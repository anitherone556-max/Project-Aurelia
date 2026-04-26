import os
import cv2
import serial
import threading
import time
import re
import json
import hid
from datetime import datetime
import numpy as np
import ctypes
from ctypes import wintypes
from collections import deque

# ==========================================
# HARDWARE CONFIGURATION
# ==========================================
LIDAR_PORT   = 'COM11'    # LD14P LiDAR 
SPATIAL_PORT = 'COM5'     # Raw mmWave (Room Macro)
VIBE_PORT    = 'COM8'     # ADXL345 Vibration
PULSE_PORT   = 'COM9'     # ESPHome mmWave (Desk Micro)
CAM_INDEX    = 0          # EMEET C960

# Temperature Sensor Config (Ambient USB)
TEMP_VID = 0x3553
TEMP_PID = 0xa001

# --- [ HWiNFO 64-BIT SENSORY LINK PROTOCOL ] ---
kernel32 = ctypes.windll.kernel32
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.MapViewOfFile.restype = ctypes.c_void_p

MAP_NAME = "Global\\HWiNFO_SENS_SM2"

# Locked User-Label Offsets + Confirmed -24 Delta
AURELIA_NERVES = {
    "CPU":   105860 - 32,
    "Brain": 158300 - 32,  # iGPU / NPU
    "Eyes":  163820 - 32   # eGPU V620
}

def get_log_time():
    return datetime.now().strftime("%H:%M:%S")

# ==========================================
# GLOBAL STATE (Aurelia's Working Memory)
# ==========================================
state_lock = threading.Lock() # INTEGRITY LAYER: Thread Safety Lock
vision_lock = threading.Lock() # INTEGRITY LAYER: Vision Buffer Lock

aurelia_state = {
    "timestamp": "",
    "camera_status": "Offline",
    "confidence": 0.0,            # Sensor Confidence Scoring
    
    # --- PHYSIOLOGY (Thermals Only) ---
    "temperature_c": 0.0,         # Ambient USB
    "cpu_thermals": 0.0,          # Ryzen CPU Native
    "brain_thermals_strix": 0.0,  # iGPU/NPU
    "eye_thermals_v620": 0.0,     # eGPU V620
    "thermal_alert": "STABLE",    # Trips to CRITICAL_FEVER if temp limits exceeded
    
    # --- SPATIAL & BIOMETRICS ---
    "vibration_xyz": [0.0, 0.0, 0.0],
    "lidar_horizontal_m": 0.0,
    "last_lidar_time": time.time(), # Added for Timeout Logic
    "spatial_mmwave_mm": 0,  
    "spatial_delta_mm": 0,   
    "pulse_mmwave_mm": 0,
    "pulse_delta_mm": 0,
    "pulse_present": False,
    "user_present": False,
    "bpm": 0,                # Extracted via FFT
    "respiration": 0,        # Extracted via FFT
    
    # --- Rolling 30-second memory arrays ---
    "history_lidar": [],
    "history_pulse": [],
    "history_spatial": [],
    "history_vibe": [],
    "history_temp": [],
    "history_cpu_temp": [],
    "history_brain_temp": [],
    "history_eye_temp": [],
    "history_bpm": []        # NEW: Added for Thalamic Volatility calculation
}

latest_frame = None
fast_pulse_buffer = deque(maxlen=200) # Thread-safe, auto-pruning buffer

# ==========================================
# ADVANCED DATA PROCESSING
# ==========================================
def extract_vitals_from_mmwave(amplitude_array, sample_rate_hz=10):
    if len(amplitude_array) < sample_rate_hz * 5: 
        return {"bpm": 0, "respiration": 0}

    # FFT Hanning Windowing Integration
    data = np.array(amplitude_array) - np.mean(amplitude_array)
    windowed_data = data * np.hanning(len(data))
    
    fft_vals = np.abs(np.fft.rfft(windowed_data))
    freqs = np.fft.rfftfreq(len(windowed_data), d=1.0/sample_rate_hz)
    
    hr_mask = (freqs >= 0.8) & (freqs <= 2.5)
    bpm = int(freqs[hr_mask][np.argmax(fft_vals[hr_mask])] * 60) if np.any(hr_mask) else 0
        
    resp_mask = (freqs >= 0.2) & (freqs <= 0.5)
    respiration = int(freqs[resp_mask][np.argmax(fft_vals[resp_mask])] * 60) if np.any(resp_mask) else 0
        
    return {"bpm": bpm, "respiration": respiration}

def calculate_confidence():
    """Assigns metadata score based on signal-to-noise and sensor sync.
    Must be called from within state_lock to prevent phantom state changes.
    """
    score = 0.5
    # High confidence if LiDAR and Pulse both agree on presence
    if aurelia_state["lidar_horizontal_m"] > 0 and aurelia_state["pulse_present"]:
        score = 0.95
    elif aurelia_state["lidar_horizontal_m"] > 0 or aurelia_state["pulse_present"]:
        score = 0.70 # Partial confirmation
    elif not aurelia_state["user_present"]:
        score = 1.0 # High confidence of absence
        
    if aurelia_state["camera_status"] == "Offline":
        score -= 0.1
        
    aurelia_state["confidence"] = round(np.clip(score, 0.0, 1.0), 2)

# ==========================================
# SENSORY NERVES (Background Threads)
# ==========================================

# --- TEMPERATURE THREAD (Ambient USB) ---
def temp_thread():
    device = hid.device()
    device_open = False
    
    while True:
        try:
            # 1. Connect if not already connected
            if not device_open:
                devices = hid.enumerate(TEMP_VID, TEMP_PID)
                if devices:
                    device.open_path(devices[0]['path'])
                    device.set_nonblocking(False)
                    device_open = True
                    print(f"[{get_log_time()}] [OMNI-HUB: AMBIENT TEMP] INFO: USB Sensor connected successfully.")
                else:
                    time.sleep(5) # Wait and check again if unplugged
                    continue

            # 2. Poll the temp data
            commands = [
                [0x01, 0x80, 0x33, 0x01, 0x00, 0x00, 0x00, 0x00],
                [0x01, 0x82, 0x77, 0x01, 0x00, 0x00, 0x00, 0x00],
                [0x00, 0x01, 0x80, 0x33, 0x01, 0x00, 0x00, 0x00]
            ]
            
            temp_found = False
            for cmd in commands:
                device.write(cmd)
                data = device.read(8, timeout_ms=200) 
                if data and len(data) >= 4:
                    temp_c = ((data[2] << 8) + data[3]) / 128.0
                    if 5 < temp_c < 95:
                        with state_lock:
                            aurelia_state["temperature_c"] = round(temp_c, 2)
                        temp_found = True
                        break
                        
            time.sleep(1)
            
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: AMBIENT TEMP] WARNING: Connection lost or read failed - {e}")
            device_open = False
            try:
                device.close()
            except:
                pass
            time.sleep(5)

# --- SYSTEM HEALTH THREAD (Direct Nerve Access via HWiNFO) ---
def system_health_thread():
    print(f"[{get_log_time()}] [OMNI-HUB: HWiNFO] INFO: HWiNFO 64-Bit DNA Graft Thread Online")
    
    while True:
        try:
            hMap = kernel32.OpenFileMappingW(0x0004, False, MAP_NAME)
            if not hMap:
                print(f"[{get_log_time()}] [OMNI-HUB: HWiNFO] WARNING: Cannot find HWiNFO Shared Memory. Retrying...")
                time.sleep(2)
                continue

            pBuf = kernel32.MapViewOfFile(hMap, 0x0004, 0, 0, 256 * 1024)
            if not pBuf:
                kernel32.CloseHandle(hMap)
                print(f"[{get_log_time()}] [OMNI-HUB: HWiNFO] WARNING: Cannot map HWiNFO memory view. Retrying...")
                time.sleep(2)
                continue

            try:
                while True:
                    temp_cpu = ctypes.cast(pBuf + AURELIA_NERVES["CPU"], ctypes.POINTER(ctypes.c_double)).contents.value
                    temp_brain = ctypes.cast(pBuf + AURELIA_NERVES["Brain"], ctypes.POINTER(ctypes.c_double)).contents.value
                    temp_eyes = ctypes.cast(pBuf + AURELIA_NERVES["Eyes"], ctypes.POINTER(ctypes.c_double)).contents.value
                    
                    with state_lock:
                        aurelia_state["cpu_thermals"] = round(temp_cpu, 1)
                        aurelia_state["brain_thermals_strix"] = round(temp_brain, 1)
                        aurelia_state["eye_thermals_v620"] = round(temp_eyes, 1)
                        
                        # True-Thermal Stewardship Alert Logic
                        if aurelia_state["eye_thermals_v620"] >= 90 or max(aurelia_state["brain_thermals_strix"], aurelia_state["cpu_thermals"]) >= 83:
                            aurelia_state["thermal_alert"] = "CRITICAL_FEVER"
                        else:
                            aurelia_state["thermal_alert"] = "STABLE"

                    time.sleep(2.0)
            except Exception as e:
                print(f"[{get_log_time()}] [OMNI-HUB: HWiNFO] ERROR: Failed reading mapped memory - {e}")
            finally:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(pBuf))
                kernel32.CloseHandle(ctypes.c_void_p(hMap))
        except Exception as e:
            print(f"[{get_log_time()}] [OMNI-HUB: HWiNFO] FATAL: System Health Thread Crashed - {e}")
        
        time.sleep(2.0)

# --- VIBRATION THREAD (COM8) ---
def vibe_thread():
    while True:
        ser = None
        try:
            ser = serial.Serial(VIBE_PORT, 115200, timeout=1)
            ser.reset_input_buffer()
            print(f"[{get_log_time()}] [OMNI-HUB: VIBE] INFO: Thread bound to {VIBE_PORT}")
            while True:
                raw = ser.readline().decode('utf-8', errors='ignore').strip()
                if "," in raw:
                    parts = raw.split(',')
                    if len(parts) == 3:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                        with state_lock:
                            aurelia_state["vibration_xyz"] = [round(x,3), round(y,3), round(z,3)]
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: VIBE] ERROR: Vibration sensor connection lost - {e}")
            if ser: ser.close()
            time.sleep(5)

# --- LIDAR THREAD (COM11) ---
def lidar_thread():
    while True:
        ser = None
        try:
            ser = serial.Serial(LIDAR_PORT, 230400, timeout=0.1)
            ser.reset_input_buffer()
            print(f"[{get_log_time()}] [OMNI-HUB: LIDAR] INFO: Thread bound to {LIDAR_PORT}")
            while True:
                if ser.in_waiting > 100:
                    raw = ser.read(ser.in_waiting)
                    for i in range(len(raw) - 47):
                        if raw[i] == 0x54 and raw[i+1] == 0x2C:
                            angle = (raw[i+4] | (raw[i+5] << 8)) / 100.0
                            if 270.0 <= angle <= 320.0:
                                dist_mm = (raw[i+6] | (raw[i+7] << 8))
                                dist_m = round(dist_mm / 1000.0, 3)
                                if 0.38 <= dist_m <= 0.89:
                                    with state_lock:
                                        aurelia_state["lidar_horizontal_m"] = dist_m 
                                        aurelia_state["last_lidar_time"] = time.time() # Keeps her "memory" of you alive
                time.sleep(0.05)
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: LIDAR] ERROR: LiDAR connection lost - {e}")
            if ser: ser.close()
            time.sleep(5)

# --- SPATIAL THREAD (COM5) - Room Macro ---
def spatial_thread():
    while True:
        ser = None
        try:
            ser = serial.Serial(SPATIAL_PORT, 115200, timeout=0.1)
            ser.reset_input_buffer()
            print(f"[{get_log_time()}] [OMNI-HUB: SPATIAL] INFO: Thread bound to {SPATIAL_PORT}")
            
            last_print_tick = time.time()
            current_range_mm = 0
            last_range_mm = 0
            
            while True:
                current_time = time.time()
                
                while ser.in_waiting > 0:
                    raw_data = ser.readline().decode('ascii', errors='ignore').strip()
                    range_match = re.search(r'Range\s+(\d+)', raw_data)
                    if range_match:
                        raw_cm = int(range_match.group(1))
                        current_range_mm = raw_cm * 10 

                if current_time - last_print_tick >= 1.0:
                    delta_mm = abs(current_range_mm - last_range_mm)
                    with state_lock:
                        aurelia_state["spatial_mmwave_mm"] = current_range_mm
                        aurelia_state["spatial_delta_mm"] = delta_mm
                    
                    last_range_mm = current_range_mm
                    last_print_tick = current_time

                time.sleep(0.05)
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: SPATIAL] ERROR: Room Macro sensor connection lost - {e}")
            if ser: ser.close() 
            time.sleep(5)

# --- PULSE THREAD (COM9) - Desk Micro ---
def pulse_thread():
    global fast_pulse_buffer
    while True:
        ser = None
        try:
            ser = serial.Serial(PULSE_PORT, 115200, timeout=1)
            ser.reset_input_buffer()
            print(f"[{get_log_time()}] [OMNI-HUB: PULSE] INFO: Thread bound to {PULSE_PORT}")
            ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
            last_detection_distance = 0
            
            while True:
                while ser.in_waiting > 0:
                    raw_line = ser.readline()
                    if raw_line:
                        try:
                            text = raw_line.decode('utf-8', errors='ignore').strip()
                            clean_text = ansi_escape.sub('', text)
                            
                            if "Distance" in clean_text and "Sending state" in clean_text:
                                parts = clean_text.split("': Sending state ")
                                sensor_type = parts[0].split("'")[-1]
                                distance_str = parts[1].split(" cm")[0]
                                
                                if sensor_type == "Detection Distance":
                                    current_dist_mm = int(float(distance_str) * 10)
                                    delta_mm = abs(current_dist_mm - last_detection_distance)
                                    
                                    with state_lock:
                                        aurelia_state["pulse_mmwave_mm"] = current_dist_mm
                                        aurelia_state["pulse_delta_mm"] = delta_mm
                                        aurelia_state["pulse_present"] = (current_dist_mm > 0)
                                    
                                        fast_pulse_buffer.append(current_dist_mm)
                                        # pop(0) removed; deque automatically prunes maxlen=200
                                    
                                    last_detection_distance = current_dist_mm
                        except Exception as parse_e:
                            print(f"[{get_log_time()}] [OMNI-HUB: PULSE] WARNING: Malformed data frame - {parse_e}")
                time.sleep(0.1)
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: PULSE] ERROR: Desk Micro sensor connection lost - {e}")
            if ser: ser.close()
            time.sleep(5)

# --- VISION THREAD ---
def vision_thread():
    global latest_frame
    while True:
        try:
            cap = cv2.VideoCapture(CAM_INDEX)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            
            if cap.isOpened(): 
                with state_lock:
                    aurelia_state["camera_status"] = "Online"
                print(f"[{get_log_time()}] [OMNI-HUB: VISION] INFO: Optic Nerve (Camera) Online")
            else:
                print(f"[{get_log_time()}] [OMNI-HUB: VISION] WARNING: Could not open Camera Index {CAM_INDEX}")
                
            while True:
                success, frame = cap.read()
                if success: 
                    with vision_lock:
                        latest_frame = frame.copy()
                else:
                    print(f"[{get_log_time()}] [OMNI-HUB: VISION] ERROR: Frame read failure.")
                    cap.release() # <--- CRITICAL FIX: Release the hardware lock before breaking
                    break # Break to re-initialize camera
                time.sleep(0.03)
        except Exception as e: 
            print(f"[{get_log_time()}] [OMNI-HUB: VISION] FATAL: Optic Nerve thread crashed - {e}")
            if 'cap' in locals() and cap.isOpened():
                cap.release() # <--- CRITICAL FIX: Failsafe release
            time.sleep(5)

# --- MEMORY BUFFER THREAD ---
def memory_buffer_thread():
    history_map = {
        "lidar_horizontal_m": "history_lidar",
        "pulse_mmwave_mm": "history_pulse",
        "spatial_mmwave_mm": "history_spatial",
        "vibration_xyz": "history_vibe",
        "temperature_c": "history_temp",
        "cpu_thermals": "history_cpu_temp",
        "brain_thermals_strix": "history_brain_temp",
        "eye_thermals_v620": "history_eye_temp",
        "bpm": "history_bpm"  # NEW: Vital for the Thalamic Filter
    }
    
    while True:
        try:
            with state_lock:
                for state_key, hist_key in history_map.items():
                    if state_key in aurelia_state and hist_key in aurelia_state:
                        # FIX: If it's the vibration array, calculate magnitude first
                        if state_key == "vibration_xyz":
                            vibe = aurelia_state[state_key]
                            val_to_append = round(sum(abs(v) for v in vibe), 3)
                        else:
                            val_to_append = aurelia_state[state_key]
                            
                        aurelia_state[hist_key].append(val_to_append)
                        if len(aurelia_state[hist_key]) > 30: 
                            aurelia_state[hist_key].pop(0)
            time.sleep(1)
        except Exception as e:
            print(f"[{get_log_time()}] [OMNI-HUB: BUFFER] ERROR: Memory array aggregation failed - {e}")
            time.sleep(1)

# ==========================================
# THE BRAIN STEM LOGIC
# ==========================================

def take_snapshot():
    global fast_pulse_buffer
    print(f"\n[{get_log_time()}] [OMNI-HUB: CORE] --- [ SYNCHRONIZED SNAPSHOT GENERATED ] ---")
    
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. ATOMIC READ: Grab a frozen copy of exactly what we need, then release the lock instantly.
    with state_lock:
        # --- UPGRADE 1: THE LIDAR TIMEOUT FIX ---
        if time.time() - aurelia_state["last_lidar_time"] > 5.0 and aurelia_state["lidar_horizontal_m"] > 0.0:
            print(f"[{get_log_time()}] [OMNI-HUB: LIDAR] WARNING: Connection timeout (>5s). Resetting presence to prevent Ghosting.")
            aurelia_state["lidar_horizontal_m"] = 0.0
            
        buffer_copy = list(fast_pulse_buffer)
        frozen_lidar = aurelia_state["lidar_horizontal_m"]
        frozen_pulse_present = aurelia_state["pulse_present"]
        aurelia_state["timestamp"] = current_time
    
    # 2. HEAVY MATH: Do the FFT calculation entirely outside the lock to prevent thread starvation.
    vitals = extract_vitals_from_mmwave(buffer_copy, sample_rate_hz=10)
    lidar_active = 0.38 < frozen_lidar < 0.89
    
    # 3. ATOMIC WRITE & METADATA: Push values back and score confidence under the SAME lock
    with state_lock:
        # Assign and instantly clamp under the same lock
        raw_bpm = vitals["bpm"]
        aurelia_state["bpm"] = int(np.clip(raw_bpm, 40, 180)) if raw_bpm > 0 else 0
        
        raw_resp = vitals["respiration"]
        aurelia_state["respiration"] = int(np.clip(raw_resp, 1, 40)) if raw_resp > 0 else 0
        
        aurelia_state["user_present"] = bool(lidar_active or frozen_pulse_present)

        # True-Thermal Protocol: Floor at 0.0, NO UPPER CLAMPING.
        aurelia_state["cpu_thermals"] = float(max(0.0, aurelia_state["cpu_thermals"]))
        aurelia_state["brain_thermals_strix"] = float(max(0.0, aurelia_state["brain_thermals_strix"]))
        aurelia_state["eye_thermals_v620"] = float(max(0.0, aurelia_state["eye_thermals_v620"]))
        
        # Floor spatial anomalies
        aurelia_state["lidar_horizontal_m"] = float(max(0.0, aurelia_state["lidar_horizontal_m"]))
        aurelia_state["spatial_mmwave_mm"] = int(max(0, aurelia_state["spatial_mmwave_mm"]))
        
        # Ensure confidence is calculated synchronously before snapshot copy
        calculate_confidence()
        
        # Safely pull a copy of the fully resolved state for serialization
        state_snapshot = aurelia_state.copy()

    local_frame = None
    with vision_lock:
        if latest_frame is not None:
            local_frame = latest_frame.copy()

    if local_frame is not None:
        try:
            small_frame = cv2.resize(local_frame, (512, 512))
            cv2.imwrite(r"C:\Aurelia_Project\Aurelia_Sensors\Aurelia_Optic_Buffer.jpg", small_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            print(f"[{get_log_time()}] [OMNI-HUB: CORE] Vision saved: Aurelia_Optic_Buffer.jpg")
        except Exception as e:
            print(f"[{get_log_time()}] [OMNI-HUB: CORE] ERROR: Failed to write Optic Buffer - {e}")
    
    # ==========================================
    # THE THALAMIC FILTER (Symbolic Compression)
    # ==========================================
    bpm_hist = state_snapshot.get('history_bpm', [])
    if len(bpm_hist) >= 5:
        sigma = np.std(bpm_hist[-10:])
        delta = bpm_hist[-1] - bpm_hist[-5]
        bpm_trend = "RISING" if delta > 3 else ("FALLING" if delta < -3 else "STATIC")
        bpm_volatility = "HIGH_JITTER" if sigma > 5.0 else ("ELEVATED" if sigma > 2.0 else "SMOOTH")
    else:
        bpm_trend = "STATIC"
        bpm_volatility = "SMOOTH"
        sigma = 0.0

    current_bpm = state_snapshot['bpm']
    is_spike = current_bpm > 100 or bpm_volatility == "HIGH_JITTER"

    # Evaluate Thermal Hierarchy
    hottest_sensor = "CPU"
    peak_temp = state_snapshot['cpu_thermals']
    if state_snapshot['brain_thermals_strix'] > peak_temp:
        peak_temp = state_snapshot['brain_thermals_strix']
        hottest_sensor = "Brain (iGPU)"
    if state_snapshot['eye_thermals_v620'] > peak_temp:
        peak_temp = state_snapshot['eye_thermals_v620']
        hottest_sensor = "Eyes (eGPU)"
        
    thermal_str = f"{hottest_sensor} PEAKING at {peak_temp}C" if peak_temp > 75 else "NOMINAL"

    # Construct the compressed Vibe Vector
    vibe_summary = f"[VIBE_VECTOR | BPM: {current_bpm} ({bpm_trend}, \u03c3:{bpm_volatility}) | PROXIMITY: {state_snapshot['lidar_horizontal_m']}m | THERMAL: {thermal_str}]"
    
    thalamic_payload = {
        "timestamp": state_snapshot["timestamp"],
        "vibe_vector": vibe_summary,
        "is_interrupt": bool(is_spike),
        "user_present": state_snapshot["user_present"],
        "confidence": state_snapshot["confidence"]
    }

    # --- CRITICAL FIX: ATOMIC DUAL-JSON WRITES ---
    try:
        # 1. Write the RAW Data for the 13B Subconscious Agent
        raw_temp_path = r"C:\Aurelia_Project\Aurelia_Sensors\Aurelia_Master_Telemetry_RAW_temp.json"
        raw_final_path = r"C:\Aurelia_Project\Aurelia_Sensors\Aurelia_Master_Telemetry_RAW.json"
        
        with open(raw_temp_path, "w") as f:
            json.dump(state_snapshot, f, indent=4)
        os.replace(raw_temp_path, raw_final_path)

        # 2. Write the Compressed Data for the 80B/9B Cache Optimization
        thalamic_temp_path = r"C:\Aurelia_Project\Aurelia_Sensors\Aurelia_Thalamic_Snapshot_temp.json"
        thalamic_final_path = r"C:\Aurelia_Project\Aurelia_Sensors\Aurelia_Thalamic_Snapshot.json"
        
        with open(thalamic_temp_path, "w") as f:
            json.dump(thalamic_payload, f, indent=4)
        os.replace(thalamic_temp_path, thalamic_final_path)
            
        print(f"[{get_log_time()}] [OMNI-HUB: CORE] Dual-Telemetry written (RAW for 13B, Thalamic for 80B/9B)")
    except Exception as e:
        print(f"[{get_log_time()}] [OMNI-HUB: CORE] ERROR: Failed to write Telemetry files - {e}")

    # --- MAGNITUDE DEBUG ---
    vibe_mag = round(sum(abs(v) for v in state_snapshot['vibration_xyz']), 3)

    print(f"    - Time:           {current_time}")
    print(f"    - Confidence:     {state_snapshot['confidence']}")
    print(f"    - CPU Native:     {state_snapshot['cpu_thermals']}°C")
    print(f"    - Brain (iGPU):   {state_snapshot['brain_thermals_strix']}°C")
    print(f"    - Eyes (eGPU):    {state_snapshot['eye_thermals_v620']}°C")
    print(f"    - Thermal Status: {state_snapshot['thermal_alert']}")
    print(f"    - Temp (Rig Core): {state_snapshot['temperature_c']}°C")
    print(f"    - Vibration:      Magnitude {vibe_mag} (Raw Vibe Array Deprecated in CLI)")
    print(f"    - Spatial (Room): Range {state_snapshot['spatial_mmwave_mm']}mm | Delta: {state_snapshot['spatial_delta_mm']}mm")
    print(f"    - LiDAR (Desk):   {state_snapshot['lidar_horizontal_m']}m")
    
    if state_snapshot["user_present"]:
        print(f"    - Desk Presence:  YES")
        pulse_status = "Active" if state_snapshot["pulse_present"] else "Stationary"
        print(f"        -> mmWave Radar: {pulse_status} ({state_snapshot['pulse_mmwave_mm']}mm)")
        print(f"        -> Vitals (FFT): {state_snapshot['bpm']} BPM | {state_snapshot['respiration']} Breaths/Min")
        print(f"        -> Thalamic Vibe: {vibe_summary} | Interrupt: {is_spike}")
    else:
        print("    - Desk Presence:  NO (AFK)")
    print("-------------------------------------------\n")

if __name__ == "__main__":
    print(f"[{get_log_time()}] [SYSTEM] Aurelia: Booting Omni-Sensory Hub (V18 Architecture)...")
    
    threading.Thread(target=temp_thread, daemon=True).start()
    threading.Thread(target=system_health_thread, daemon=True).start() 
    threading.Thread(target=vibe_thread, daemon=True).start()
    threading.Thread(target=lidar_thread, daemon=True).start()
    threading.Thread(target=spatial_thread, daemon=True).start()
    threading.Thread(target=pulse_thread, daemon=True).start()
    threading.Thread(target=vision_thread, daemon=True).start()
    
    threading.Thread(target=memory_buffer_thread, daemon=True).start()
    
    time.sleep(3) 
    print(f"\n[{get_log_time()}] [SYSTEM] Aurelia: Nervous System Online.")
    print(f"[{get_log_time()}] [SYSTEM] Auto-Snapshot active: Updating memory every 30 seconds.")
    print(f"[{get_log_time()}] [SYSTEM] Press Ctrl+C to shut down the Hub.")
    
    try:
        while True:
            take_snapshot()
            time.sleep(30)
    except KeyboardInterrupt:
        print(f"\n[{get_log_time()}] [SYSTEM] Aurelia: Severing brain stem. Shutting down Omni-Hub.")