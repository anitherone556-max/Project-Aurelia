import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from filelock import FileLock, Timeout

# Configure logging for the memory module
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [13B_HIPPOCAMPUS] - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class SubconsciousMemoryManager:
    """
    Manages the Tier 1 (Working Memory) and Tier 2 (State Ledger) 
    for the Qwen 13B Subconscious Agent.
    """
    
    def __init__(self, workspace_path: str | Path):
        self.workspace = Path(workspace_path)
        self.ledger_path = self.workspace / "agent_state_ledger.json"
        self.lock_path = self.workspace / "agent_state_ledger.json.lock"
        
        # Tier 1: In-RAM Scratchpad (Session specific, wipes on success)
        self.working_memory_scratchpad: List[Dict[str, str]] = []
        self.failure_count: int = 0
        
        # Initialize Tier 2 Ledger if it doesn't exist
        self._initialize_ledger()

    def _initialize_ledger(self) -> None:
        """Creates the JSON ledger if it doesn't exist, safely."""
        if not self.ledger_path.exists():
            default_state = {
                "_metadata": {
                    "description": "Aurelia 13B Agent Tool Infrastructure State",
                    "last_updated": "System Initialization"
                },
                "tools": {}
            }
            self._write_ledger(default_state)
            logger.info("Initialized fresh agent_state_ledger.json.")

    def _read_ledger(self) -> Optional[dict]:
        """Reads the ledger safely using a file lock."""
        lock = FileLock(self.lock_path, timeout=5)
        try:
            with lock:
                with open(self.ledger_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except json.JSONDecodeError:
            logger.error("Ledger JSON corrupted! Backing up and resetting.")
            backup_path = self.workspace / "agent_state_ledger_corrupted.json"
            shutil.copy(self.ledger_path, backup_path)
            self._initialize_ledger()
            return self._read_ledger()
        except Timeout:
            logger.warning("Timeout acquiring lock to read ledger. Returning None.")
            return None

    def _write_ledger(self, data: dict) -> None:
        """Writes to the ledger safely using a file lock."""
        lock = FileLock(self.lock_path, timeout=5)
        try:
            with lock:
                with open(self.ledger_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
        except Timeout:
            logger.error("Timeout acquiring lock to write ledger. State not saved!")

    # ==========================================
    # TIER 2: LEDGER METHODS (STATE MEMORY)
    # ==========================================

    def get_ledger_state_formatted(self) -> str:
        """Returns the ledger state formatted cleanly for the 13B System Prompt."""
        ledger = self._read_ledger()
        
        if ledger is None:
            return "[CURRENT SYSTEM STATE]: Ledger temporarily inaccessible due to file lock timeout."
            
        tools = ledger.get("tools", {})
        
        if not tools:
            return "[CURRENT SYSTEM STATE]: No tools currently registered in ledger."
            
        formatted_str = "[CURRENT SYSTEM STATE - REGISTERED TOOLS]:\n"
        for tool, state in tools.items():
            formatted_str += f"- {tool} | Status: {state.get('status', 'unknown')} | Known Bugs: {state.get('known_bugs', 'None')} | Last Outcome: {state.get('last_outcome', 'Pending')}\n"
        
        return formatted_str

    def update_tool_state(self, tool_name: str, status: str, known_bugs: str = "None", last_outcome: str = "Pending") -> None:
        """Allows the Orchestrator (or a subprocess script) to update a tool's status and outcome."""
        ledger = self._read_ledger()
        
        if ledger is None:
            logger.warning(f"Aborting ledger update for {tool_name} due to read timeout.")
            return
            
        ledger["tools"][tool_name] = {
            "status": status,
            "known_bugs": known_bugs,
            "last_outcome": last_outcome
        }
        
        self._write_ledger(ledger)
        logger.info(f"Ledger Updated: {tool_name} is now '{status}'. Outcome logged.")

    # ==========================================
    # TIER 1: SCRATCHPAD METHODS (WORKING MEMORY)
    # ==========================================

    def log_error(self, step: str, error_traceback: str) -> bool:
        """
        Logs a failed execution attempt.
        Returns True if failure_count > 5 (indicating a Stalled Goal).
        """
        self.failure_count += 1
        
        new_entry = {
            "step": step,
            "error": error_traceback[-1000:] # Keep only the last 1000 chars of traceback to save tokens
        }
        
        if len(self.working_memory_scratchpad) >= 5:
            # Replace the oldest entry (FIFO simulation) to prevent infinite context growth
            self.working_memory_scratchpad.pop(0)
            
        self.working_memory_scratchpad.append(new_entry)
        logger.warning(f"Error logged to scratchpad for step: {step} (Failures: {self.failure_count})")
        
        return self.failure_count > 5

    def get_scratchpad_formatted(self) -> str:
        """Formats the working memory for injection during an Error Reflection loop."""
        if not self.working_memory_scratchpad:
            return "" # No previous errors, return empty
            
        formatted_str = "\n[SESSION SCRATCHPAD - PREVIOUS FAILED ATTEMPTS]:\n"
        for i, entry in enumerate(self.working_memory_scratchpad, 1):
            formatted_str += f"Attempt {i} ({entry['step']}) Failed with Error:\n{entry['error']}\n---\n"
            
        formatted_str += "[DIRECTIVE]: Analyze the scratchpad errors above inside your <think> tag. Do NOT repeat the exact same code that caused these errors.\n"
        return formatted_str

    def clear_scratchpad(self) -> None:
        """Wipes the scratchpad upon a successful GOAL_COMPLETED."""
        self.working_memory_scratchpad.clear()
        self.failure_count = 0
        logger.info("Scratchpad cleared. Goal achieved or purged.")