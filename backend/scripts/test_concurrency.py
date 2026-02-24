import sys
import os
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session

# Add backend path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.game_service import GameService
from app.models.game_enums import ActionType
from app.db.base import SessionLocal
from app.models.user import User
from app.models.game import GameEvent

# Mock manager
from unittest.mock import AsyncMock
from app.core.socket_manager import manager
manager.broadcast = AsyncMock()

def run_worker_sync(game_id, user_id, count):
    """Synchronous worker simulating concurrent requests"""
    db = SessionLocal()
    try:
        for i in range(count):
            try:
                # 模拟 process_action 的持久化部分
                # 注意：这里我们直接调用 _append_event 并 commit
                # 实际上 process_action 会做更多事，但核心冲突点在这里
                
                # 为了增加冲突概率，我们可以在这里 sleep 一下？
                # 不，_append_event 内部很快，外部 sleep 没用。
                # 真正的冲突在于 "Read max seq" 和 "Insert" 之间的时间窗口。
                
                GameService._append_event(
                    db, 
                    game_id, 
                    ActionType.SPEAK, 
                    user_id, 
                    {"content": f"msg {i} from {user_id}"}
                )
                db.commit()
                # print(f"User {user_id} appended {i}")
            except Exception as e:
                print(f"Error in worker {user_id} iter {i}: {e}")
                db.rollback()
    finally:
        db.close()

async def main():
    print("Setting up test...")
    
    # 1. Get Users
    db = SessionLocal()
    users = db.query(User).limit(8).all()
    if len(users) < 8:
        print("Need 8 users. Run create_test_users.py first.")
        return
    p_ids = [u.id for u in users]
    u_map = {u.id: u.username for u in users}
    db.close()
    
    # 2. Create Game
    print("Creating game...")
    game = await GameService.create_game(p_ids, u_map, p_ids[0])
    game_id = game.game_id
    print(f"Game created: {game_id}")
    
    # 3. Run Concurrent Workers
    num_workers = 5
    msgs_per_worker = 20
    total_msgs = num_workers * msgs_per_worker
    
    print(f"Starting {num_workers} threads, {msgs_per_worker} msgs each...")
    start_time = time.time()
    
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        for i in range(num_workers):
            # Pass distinct user_ids to simulate different players
            futures.append(
                loop.run_in_executor(
                    executor, 
                    run_worker_sync, 
                    game_id, 
                    p_ids[i], 
                    msgs_per_worker
                )
            )
        await asyncio.gather(*futures)
        
    duration = time.time() - start_time
    print(f"Finished in {duration:.2f}s")
    
    # 4. Verify
    db = SessionLocal()
    events = db.query(GameEvent).filter(GameEvent.game_id == game_id).order_by(GameEvent.seq).all()
    print(f"Total events in DB: {len(events)}")
    
    expected = 1 + total_msgs # 1 for GAME_START
    
    # Check sequences
    seqs = [e.seq for e in events]
    is_contiguous = seqs == list(range(1, len(events) + 1))
    
    if len(events) == expected and is_contiguous:
        print("SUCCESS: All events persisted with contiguous sequences!")
    else:
        print(f"FAILURE: Expected {expected}, got {len(events)}")
        print(f"Contiguous: {is_contiguous}")
        # print(f"Seqs: {seqs}")
        
        # Check for duplicates
        if len(seqs) != len(set(seqs)):
            print("ERROR: Duplicate sequences found!")
        
        # Check for missing
        if not is_contiguous:
            print(f"Max seq: {max(seqs) if seqs else 0}")

    db.close()

if __name__ == "__main__":
    asyncio.run(main())
