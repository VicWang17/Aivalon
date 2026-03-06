import sys
import os
import time
import redis
from app.core.config import settings

# Ensure app is in python path
sys.path.append(os.getcwd())

try:
    from app.tasks.stats import process_game_result
    from app.models.game_enums import Camp, Character
except ImportError as e:
    print(f"Error importing app modules: {e}")
    sys.exit(1)

def test_idempotency():
    print("=== Testing Idempotency ===")
    
    # 1. Prepare data
    game_id = f"test_idempotency_{int(time.time())}"
    winner = Camp.GOOD.value
    players_data = [
        {
            "user_id": 1,
            "username": "User1",
            "seat_id": 0,
            "is_ai": False,
            "character": Character.MERLIN.value,
            "is_connected": True,
            "has_voted": False,
            "has_acted": True
        }
    ]
    
    # 2. First execution
    print(f"\n[1] First Execution for {game_id}...")
    try:
        # Using .apply() to execute synchronously in current process
        process_game_result.apply(args=[game_id, winner, players_data])
    except Exception as e:
        print(f"Execution failed: {e}")

    # 3. Second execution (Should be skipped)
    print(f"\n[2] Second Execution for {game_id}...")
    try:
        process_game_result.apply(args=[game_id, winner, players_data])
    except Exception as e:
        print(f"Execution failed: {e}")

    # 4. Check Redis Key directly
    print(f"\n[3] Verifying Redis Key...")
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD,
        decode_responses=True
    )
    key = f"celery:idempotency:game_result:{game_id}"
    val = r.get(key)
    print(f"Key {key}: {val}")
    
    if val in ["PROCESSING", "DONE"]:
        print("SUCCESS: Key exists.")
    else:
        print("FAILURE: Key does not exist.")
        
    r.close()

if __name__ == "__main__":
    test_idempotency()
