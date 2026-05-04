# ==========================================
# AURELIA MOBILE GATEWAY (V1.0)
# Subconscious Draft: 13B Action Engine
# ==========================================
import os
import time
import json
import asyncio
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import aiofiles

# --- DIRECTORY CONFIGURATION ---
BASE_DIR = Path("C:/Aurelia_Project")
MOBILE_DIR = BASE_DIR / "Aurelia_Mobile"
SENSORS_DIR = BASE_DIR / "Aurelia_Sensors"
VISION_DIR = SENSORS_DIR / "mobile_vision"
LIBRARY_DIR = MOBILE_DIR / "Library"
AUDIO_DIR = BASE_DIR / "Aurelia_Audio_Output"

# --- NEW: DROP FOLDER ARCHITECTURE (Bidirectional) ---
OUTBOX_DIR = BASE_DIR / "Aurelia_Mobile_Outbox"
SUB_OUTBOX_DIR = BASE_DIR / "Aurelia_Mobile_Subconscious"
INBOX_DIR = BASE_DIR / "Aurelia_Mobile_Inbox"
GOAL_DIR = BASE_DIR / "Aurelia_Mobile_Goal"

# Ensure all directories exist
for d in [MOBILE_DIR, SENSORS_DIR, VISION_DIR, LIBRARY_DIR, AUDIO_DIR, OUTBOX_DIR, SUB_OUTBOX_DIR, INBOX_DIR, GOAL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- APP INITIALIZATION ---
app = FastAPI(title="Aurelia Mobile Portal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[" your web http ", ""],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(MOBILE_DIR)), name="static")
app.mount("/library_files", StaticFiles(directory=str(LIBRARY_DIR)), name="library")
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")

# --- CONNECTION MANAGERS (The Synapses) ---
class ConnectionManager:
    def __init__(self):
        self.portal_connections: list[WebSocket] = []
        self.system_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        if channel == "portal":
            self.portal_connections.append(websocket)
        elif channel == "system":
            self.system_connections.append(websocket)

    def disconnect(self, websocket: WebSocket, channel: str):
        if channel == "portal" and websocket in self.portal_connections:
            self.portal_connections.remove(websocket)
        elif channel == "system" and websocket in self.system_connections:
            self.system_connections.remove(websocket)

    async def broadcast_portal(self, message: dict):
        for connection in self.portal_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass 

    async def broadcast_system(self, message: dict):
        for connection in self.system_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# --- SECURITY MIDDLEWARE (Tailscale Lock) ---
@app.middleware("http")
async def verify_tailscale_ip(request: Request, call_next):
    client_ip = request.client.host
    if not (client_ip.startswith("100.") or client_ip == "127.0.0.1" or client_ip == "::1"):
        print(f"[SECURITY] Blocked unauthorized access attempt from {client_ip}")
        return JSONResponse(status_code=403, content={"detail": "Unauthorized. Tether not recognized."})
    return await call_next(request)

# --- THE OUTBOX WATCHER (Drop-Folder Paradigm) ---
async def outbox_watcher():
    """Constantly monitors the outbox folders for responses, audio, and subconscious logs."""
    
    print("[OUTBOX] Watcher Synapse Activated. Monitoring Drop-Folders.")
    
    while True:
        try:
            # 1. Watch for Mobile Text Responses (80B)
            if len(manager.portal_connections) > 0:
                # Grab all txt files and sort them chronologically by creation time
                outbox_files = sorted(OUTBOX_DIR.glob("*.txt"), key=os.path.getctime)
                
                for file_path in outbox_files:
                    file_age = time.time() - os.path.getmtime(file_path)
                    
                    # Stale Message Guard
                    if file_age > 1800:
                        print(f"[OUTBOX] Discarding stale message ({file_age:.0f}s old)")
                        file_path.unlink()
                        continue
                    
                    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                        content = await f.read()
                    
                    file_path.unlink() # Delete after reading
                    
                    if content.strip():
                        await manager.broadcast_portal({
                            "type": "chat", 
                            "text": content
                        })

            # 2. Watch for Subconscious Terminal Logs (13B)
            sub_files = sorted(SUB_OUTBOX_DIR.glob("*.txt"), key=os.path.getctime)
            
            if len(manager.system_connections) > 0:
                for file_path in sub_files:
                    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                        sub_content = await f.read()
                    
                    file_path.unlink()
                    
                    if sub_content.strip():
                        await manager.broadcast_system({
                            "type": "terminal_log", 
                            "text": sub_content.strip()
                        })
            else:
                # Phone is asleep. Safely delete transient background logs.
                for file_path in sub_files:
                    try:
                        file_path.unlink()
                    except Exception:
                        pass

            # 3. Watch for New Audio Files (Fixing the Audio Blast & Memory Leak)
            audio_files = sorted(Path(AUDIO_DIR).glob("*.wav"), key=os.path.getctime)
            
            if audio_files:
                if len(manager.portal_connections) > 0:
                    for audio_file in audio_files:
                        await manager.broadcast_portal({
                            "type": "audio", 
                            "url": f"/audio/{audio_file.name}"
                        })
                        
                        # Use a background task so we don't block the while-loop
                        # 15.0 second timer to protect against slow 3G network latency
                        async def delayed_delete(filepath):
                            await asyncio.sleep(15.0)  
                            try:
                                filepath.unlink()
                            except Exception:
                                pass
                        asyncio.create_task(delayed_delete(audio_file))
                else:
                    # Phone is asleep. Safely delete unplayed audio to prevent a massive blast on reconnect.
                    for audio_file in audio_files:
                        try:
                            audio_file.unlink()
                        except Exception:
                            pass

        except Exception as e:
            pass # Keep polling clean on file lock collisions
            
        await asyncio.sleep(0.5) 

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(outbox_watcher())

# --- CORE ENDPOINTS ---
@app.get("/", response_class=HTMLResponse)
async def get_portal():
    index_path = MOBILE_DIR / "index.html"
    if not index_path.exists():
        return "<h1>[AURELIA MOBILE GATEWAY: ACTIVE]</h1><p>index.html not found. Awaiting Subconscious generation.</p>"
    
    async with aiofiles.open(index_path, 'r', encoding='utf-8') as f:
        html_content = await f.read()
    return HTMLResponse(content=html_content)

@app.get("/api/library")
async def get_library_index():
    try:
        files = []
        for file_path in LIBRARY_DIR.glob("*.html"):
            stat = file_path.stat()
            files.append({
                "filename": file_path.name,
                "title": file_path.stem.replace("_", " "),
                "created_at": stat.st_ctime
            })
        files.sort(key=lambda x: x["created_at"], reverse=True)
        return {"reports": files}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/upload_image")
async def upload_mobile_vision(file: UploadFile = File(...)):
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"MOBILE_VISION_{timestamp}.jpg"
        file_path = VISION_DIR / filename

        async with aiofiles.open(file_path, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)

        print(f"[VISION LOBE] Received Mobile Optic Feed: {filename}")

        await manager.broadcast_portal({"type": "notification", "message": "Optic Feed Synced."})
        return {"status": "success", "filename": filename}
    except Exception as e:
        print(f"[ERROR] Mobile Vision upload failed: {e}")
        return JSONResponse(status_code=500, content={"error": "Failed to sync optic feed."})

@app.get("/sw.js")
async def get_service_worker():
    sw_path = MOBILE_DIR / "sw.js"
    if sw_path.exists():
        return FileResponse(sw_path, media_type="application/javascript")
    return JSONResponse(status_code=404, content={"error": "Not found"})

# --- WEBSOCKET ROUTERS ---
@app.websocket("/ws/portal")
async def websocket_portal(websocket: WebSocket):
    await manager.connect(websocket, "portal")
    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                payload = json.loads(raw_data)
                msg_type = payload.get("type")
                msg_text = payload.get("text", "").strip()

                if msg_type == "chat" and msg_text:
                    print(f"[PORTAL] Message from Geiger: {msg_text}")
                    # DROP-FOLDER PARADIGM: Create unique inbound text files
                    timestamp = f"{time.time():.6f}"
                    inbox_path = INBOX_DIR / f"inbox_{timestamp}.txt"
                    async with aiofiles.open(inbox_path, 'w', encoding='utf-8') as f:
                        await f.write(msg_text)
                
                elif msg_type == "set_goal" and msg_text:
                    print(f"[PORTAL] Goal Injected by Geiger: {msg_text}")
                    # DROP-FOLDER PARADIGM: Create unique inbound goal files
                    timestamp = f"{time.time():.6f}"
                    goal_path = GOAL_DIR / f"goal_{timestamp}.txt"
                    async with aiofiles.open(goal_path, 'w', encoding='utf-8') as f:
                        await f.write(msg_text)

            except json.JSONDecodeError:
                print(f"[PORTAL] Raw Text Fallback: {raw_data}")
                timestamp = f"{time.time():.6f}"
                inbox_path = INBOX_DIR / f"inbox_{timestamp}.txt"
                async with aiofiles.open(inbox_path, 'w', encoding='utf-8') as f:
                    await f.write(raw_data)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, "portal")
        print("[PORTAL] Geiger's connection severed.")

@app.websocket("/ws/system")
async def websocket_system(websocket: WebSocket):
    await manager.connect(websocket, "system")
    try:
        while True:
            snapshot_path = SENSORS_DIR / "Aurelia_Master_Telemetry_RAW.json"
            if snapshot_path.exists():
                async with aiofiles.open(snapshot_path, 'r') as f:
                    try:
                        snapshot_data = json.loads(await f.read())
                        await websocket.send_json({"type": "somatic_data", "data": snapshot_data})
                    except json.JSONDecodeError:
                        pass 
            await asyncio.sleep(1.0) 
    except WebSocketDisconnect:
        manager.disconnect(websocket, "system")

if __name__ == "__main__":
    print("===================================================")
    print(" [SYSTEM] Booting Aurelia Mobile Gateway...")
    print(" [NETWORK] Binding to Secure Tailscale HTTPS...")
    print("===================================================")
    
    TAILSCALE_IP = "0.0.0.0" 
    
    cert_path = BASE_DIR / ""
    key_path = BASE_DIR / ""
    
    if cert_path.exists() and key_path.exists():
        uvicorn.run(
            app, 
            host=TAILSCALE_IP, 
            port=443, 
            ssl_keyfile=str(key_path), 
            ssl_certfile=str(cert_path),
            log_level="warning"
        )
    else:
        print("[ERROR] SSL Certificates not found. Falling back to HTTP on 8080.")
        uvicorn.run(app, host=TAILSCALE_IP, port=8080, log_level="warning")
