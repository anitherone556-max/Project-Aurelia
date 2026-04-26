import chromadb
from chromadb.utils import embedding_functions
import time
import os
import numpy as np
from datetime import datetime
import json
import threading
import random
import re
import sqlite3

# --- CONFIGURATION ---
MEMORY_PATH = "C:/Aurelia_Project/Aurelia_DB"
COLLECTION_NAME = "aurelia_obsession_archive"
GOALS_FILE = os.path.join(MEMORY_PATH, "goals.json")

# --- VARIABLE DECAY RATES (Lambda) ---
DECAY_RATES = {
    "conversation": 0.000003,    # Half-life of ~2.5 days for daily interaction
    "journal": 0.0000001,        # Near-permanent core memories
    "document": 0.0000001,       # Ingested knowledge base
    "subconscious": 0.0000001    # Subconscious learnings
}

def get_log_time():
    return datetime.now().strftime("%H:%M:%S")

if not os.path.exists(MEMORY_PATH):
    os.makedirs(MEMORY_PATH)

try:
    client = chromadb.PersistentClient(path=MEMORY_PATH)
    emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=emb_fn
    )
    print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: ChromaDB successfully initialized.")
except Exception as e:
    print(f"[{get_log_time()}] [MEMORY ENGINE] FATAL ERROR: Database initialization failed - {e}")

# ==========================================
# IN-MEMORY GOAL CACHE (Stops Disk Thrashing)
# ==========================================
_ACTIVE_GOALS_CACHE = []
_goals_lock = threading.RLock() # Re-entrant lock for thread safety

def _init_goals():
    global _ACTIVE_GOALS_CACHE
    with _goals_lock:
        if not os.path.exists(GOALS_FILE):
            with open(GOALS_FILE, 'w') as f:
                # FIXED: Removed default background goal. Subconscious is now reactive.
                default_goal = []
                json.dump(default_goal, f, indent=4)
                _ACTIVE_GOALS_CACHE = default_goal
        else:
            try:
                with open(GOALS_FILE, 'r') as f:
                    _ACTIVE_GOALS_CACHE = json.load(f)
            except Exception:
                _ACTIVE_GOALS_CACHE = []

_init_goals()

def _sync_goals_to_disk():
    with _goals_lock:
        try:
            with open(GOALS_FILE, 'w') as f:
                json.dump(_ACTIVE_GOALS_CACHE, f, indent=4)
        except Exception as e:
            print(f"[{get_log_time()}] [MEMORY ENGINE] ERROR: Failed to write goals to disk - {e}")

def get_active_goals():
    with _goals_lock:
        return sorted([g for g in _ACTIVE_GOALS_CACHE if g["status"] == "Active"], key=lambda x: x["priority"], reverse=True)

def add_goal(description, priority=5):
    global _ACTIVE_GOALS_CACHE
    with _goals_lock:
        # --- CLAUDE AUDIT FIX: Deduplication Guard ---
        if any(g["description"].strip() == description.strip() 
               and g["status"] == "Active" 
               for g in _ACTIVE_GOALS_CACHE):
            print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Duplicate goal suppressed.")
            return None
            
        goal_id = f"g_{int(time.time())}"
        _ACTIVE_GOALS_CACHE.append({
            "id": goal_id, 
            "description": description, 
            "priority": priority, 
            "status": "Active"
        })
        _sync_goals_to_disk()
        
    print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: New Goal Registered | ID: {goal_id}")
    return goal_id

def complete_goal(goal_id):
    global _ACTIVE_GOALS_CACHE
    with _goals_lock:
        for g in _ACTIVE_GOALS_CACHE:
            if g["id"] == goal_id:
                g["status"] = "Completed"
                break
        _sync_goals_to_disk()
    print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Goal Completed | ID: {goal_id}")

def purge_goal(goal_id):
    global _ACTIVE_GOALS_CACHE
    with _goals_lock:
        # Filter out the stalled goal, permanently deleting it from the cache
        _ACTIVE_GOALS_CACHE = [g for g in _ACTIVE_GOALS_CACHE if g["id"] != goal_id]
        # Immediately overwrite the JSON file with the purged list
        _sync_goals_to_disk()
    print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Goal Purged from Existence (Execution Limit Reached) | ID: {goal_id}")

# ==========================================
# HYBRID RETRIEVAL: SQLITE FTS5 (DISK-BASED BM25)
# ==========================================
FTS_DB_PATH = os.path.join(MEMORY_PATH, "aurelia_fts.db")

# check_same_thread=False allows our async queues to hit the DB safely
fts_conn = sqlite3.connect(FTS_DB_PATH, check_same_thread=False)
fts_lock = threading.Lock()

def tokenize_text(text):
    """ Extracts clean alphanumeric words for robust FTS matching. """
    return [word for word in re.split(r'\W+', text.lower()) if word]

def init_fts_engine():
    """ Boots the FTS engine and ensures it is perfectly synced with Chroma. """
    with fts_lock:
        cursor = fts_conn.cursor()
        
        # Create the virtual FTS5 table. We use 'porter' stemming for better NLP matching.
        # 'id' is UNINDEXED because we only use it as a reference key.
        cursor.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS bm25_index USING fts5(
                id UNINDEXED,
                content,
                tokenize='porter'
            )
        ''')
        fts_conn.commit()

        # --- ONE-TIME SYNC CHECK ---
        cursor.execute("SELECT COUNT(*) FROM bm25_index")
        fts_count = cursor.fetchone()[0]
        
        try:
            chroma_count = collection.count()
        except Exception:
            chroma_count = 0

        if fts_count != chroma_count:
            print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Re-aligning Sparse FTS5 Index to Dense Core...")
            cursor.execute("DELETE FROM bm25_index")
            
            all_data = collection.get()
            if all_data and all_data['ids']:
                for idx, mem_id in enumerate(all_data['ids']):
                    cursor.execute("INSERT INTO bm25_index (id, content) VALUES (?, ?)", 
                                   (mem_id, all_data['documents'][idx]))
            fts_conn.commit()
            print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: FTS5 Alignment Complete. Matrices synchronized.")

# Run the bootloader immediately
init_fts_engine()

# ==========================================
# SEMANTIC MEMORY & RETRIEVAL
# ==========================================
def save_memory(content, mood="SOFT", importance=0.5, mem_type="conversation"):
    try:
        # --- SEMANTIC DEDUPLICATION ---
        if mem_type == "conversation" and len(content) > 60:
            sim_check = collection.query(query_texts=[content], n_results=1)
            if sim_check['distances'] and sim_check['distances'][0]:
                if sim_check['distances'][0][0] < 0.2:
                    existing_id = sim_check['ids'][0][0]
                    reinforce_memory(existing_id, boost=0.05)
                    return 

        unix_time = time.time()
        readable_time = datetime.fromtimestamp(unix_time).strftime("%Y-%m-%d %H:%M:%S")
        mem_id = f"mem_{int(unix_time * 1000)}"
        
        # 1. Save to Dense Vector Database
        collection.add(
            documents=[content],
            metadatas=[{
                "created_at": unix_time,          
                "timestamp_str": readable_time,   
                "importance": float(importance),
                "access_count": 0,
                "mood": mood,
                "type": mem_type
            }],
            ids=[mem_id]
        )
        
        # 2. Save to Sparse Exact-Match Database (Instantaneous)
        with fts_lock:
            cursor = fts_conn.cursor()
            cursor.execute("INSERT INTO bm25_index (id, content) VALUES (?, ?)", (mem_id, content))
            fts_conn.commit()
        
        print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Memory Indexed | ID: {mem_id} | Importance: {importance}")
        
    except Exception as e:
        print(f"[{get_log_time()}] [MEMORY ENGINE] ERROR: Failed to save memory - {e}")

def reinforce_memory(mem_id, boost=0.1):
    try:
        existing = collection.get(ids=[mem_id])
        if existing and existing['metadatas']:
            meta = existing['metadatas'][0]
            new_importance = min(1.0, float(meta['importance']) + boost)
            new_count = int(meta['access_count']) + 1
            
            # Reconsolidation: Reset the timestamp so the decay curve starts over
            collection.update(
                ids=[mem_id],
                metadatas=[{**meta, "importance": new_importance, "access_count": new_count, "created_at": time.time()}]
            )
            print(f"[{get_log_time()}] [MEMORY ENGINE] INFO: Synaptic Pathway Strengthened | ID: {mem_id} | New Weight: {new_importance:.2f}")
    except Exception as e:
        print(f"[{get_log_time()}] [MEMORY ENGINE] ERROR: Failed to reinforce memory - {e}")

def query_and_prune(query_text, current_mood="SOFT", min_d=0.2):
    try:
        current_time = time.time()
        fused_scores = {}
        memory_metadata = {}
        
        # 1. DENSE RETRIEVAL (ChromaDB)
        chroma_results = collection.query(query_texts=[query_text], n_results=50)
        if chroma_results['documents'] and chroma_results['ids'][0]:
            for rank, mem_id in enumerate(chroma_results['ids'][0]):
                rrf_dense = 60.0 / (60 + rank)
                fused_scores[mem_id] = fused_scores.get(mem_id, 0.0) + rrf_dense
                
                if mem_id not in memory_metadata:
                    memory_metadata[mem_id] = {
                        "doc": chroma_results['documents'][0][rank],
                        "meta": chroma_results['metadatas'][0][rank]
                    }

        # 2. SPARSE RETRIEVAL (SQLite FTS5)
        tokens = tokenize_text(query_text)
        if tokens:
            # Join words with OR to mimic standard bag-of-words exact matching
            fts_query = " OR ".join(tokens)
            
            try:
                with fts_lock:
                    cursor = fts_conn.cursor()
                    # ORDER BY bm25() natively calculates BM25 ranking. 
                    # (Note: SQLite returns negative numbers for BM25, so most negative = best match)
                    cursor.execute("""
                        SELECT id FROM bm25_index 
                        WHERE content MATCH ? 
                        ORDER BY bm25(bm25_index) LIMIT 50
                    """, (fts_query,))
                    
                    sparse_hits = cursor.fetchall()
            except Exception as e:
                print(f"[{get_log_time()}] [MEMORY ENGINE] FTS Parse skipped: {e}")
                sparse_hits = []

            for rank, (mem_id,) in enumerate(sparse_hits):
                # Manual metadata fetch if Dense missed this exact match
                if mem_id not in memory_metadata:
                    exact_match_data = collection.get(ids=[mem_id])
                    if exact_match_data and exact_match_data['documents']:
                        memory_metadata[mem_id] = {
                            "doc": exact_match_data['documents'][0],
                            "meta": exact_match_data['metadatas'][0]
                        }
                    else:
                        continue 
                        
                # Identical RRF math to keep the weighting beautifully balanced
                rrf_sparse = 60.0 / (60 + rank)
                fused_scores[mem_id] = fused_scores.get(mem_id, 0.0) + rrf_sparse

        # 3. EXPONENTIAL DECAY, MOOD WEIGHTING, & PRUNING
        scored_memories = []
        for mem_id, rrf_score in fused_scores.items():
            data = memory_metadata.get(mem_id)
            if not data: continue
            
            meta = data["meta"]
            doc = data["doc"]
            
            # State-Dependent Memory: Boost RRF score if memory matches current emotional state
            if meta.get('mood') == current_mood:
                rrf_score *= 1.2
            
            # Fetch specific decay rate based on memory type
            mem_type = meta.get('type', 'conversation')
            lambda_val = DECAY_RATES.get(mem_type, 0.000003)
            
            t = current_time - meta['created_at']
            final_d = (meta['importance'] * rrf_score) * np.exp(-lambda_val * t)
            
            if final_d > min_d: 
                scored_memories.append({
                    "score": final_d,
                    "text": f"- [ARCHIVED: {meta['timestamp_str']} | ID: {mem_id} | STATE: {meta['mood']}] -> {doc}"
                })
        
        scored_memories.sort(key=lambda x: x["score"], reverse=True)
        top_memories = scored_memories[:10]
        
        formatted_context = "\n".join([m["text"] for m in top_memories])
        
        active_goals = get_active_goals()
        if active_goals:
            goal_strs = [f"- {g['description']} (Priority: {g['priority']})" for g in active_goals[:3]]
            formatted_context += f"\n\n[ACTIVE INTERNAL GOALS (Subconscious)]:\n" + "\n".join(goal_strs)
                
        # 4. ACTIVE GARBAGE COLLECTION
        if random.random() < 0.05: 
            threading.Thread(target=garbage_collect_memories, daemon=True).start()

        return formatted_context
    except Exception as e:
        print(f"[{get_log_time()}] [MEMORY ENGINE] ERROR: Hybrid Query failed - {e}")
        return ""

# ==========================================
# ACTIVE GARBAGE COLLECTION (The Void)
# ==========================================
def garbage_collect_memories(kill_threshold=0.05):
    try:
        all_data = collection.get(include=["metadatas"])
        if not all_data['ids']:
            return

        current_time = time.time()
        ids_to_delete = []

        for i, mem_id in enumerate(all_data['ids']):
            meta = all_data['metadatas'][i]
            
            # Use specific decay rate for calculation
            mem_type = meta.get('type', 'conversation')
            
            # Hard-skip the permanent ones to save compute, though decay rate handles it too
            if mem_type in ["journal", "document", "subconscious"]:
                continue
                
            lambda_val = DECAY_RATES.get(mem_type, 0.000003)
            
            t = current_time - meta['created_at']
            d = meta['importance'] * np.exp(-lambda_val * t)
            
            if d < kill_threshold:
                ids_to_delete.append(mem_id)

        if ids_to_delete:
            # Wipe from Dense Vector Core
            collection.delete(ids=ids_to_delete)
            
            # Wipe from Sparse FTS5 Core
            with fts_lock:
                cursor = fts_conn.cursor()
                cursor.executemany("DELETE FROM bm25_index WHERE id = ?", [(i,) for i in ids_to_delete])
                fts_conn.commit()
            
            print(f"[{get_log_time()}] [MEMORY ENGINE] AUDIT: The Void purged {len(ids_to_delete)} dead memory vectors.")
            
    except Exception as e:
        print(f"[{get_log_time()}] [MEMORY ENGINE] ERROR: Garbage collection failed - {e}")