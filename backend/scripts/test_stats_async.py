import sys
import os
import time

# Ensure app is in python path
sys.path.append(os.getcwd())

from app.tasks.stats import process_game_result
from app.models.game_enums import Camp, Character

def test_stats_task():
    print("Sending stats task...")
    
    # 构造模拟数据
    game_id = "test_game_async_" + str(int(time.time()))
    winner = Camp.GOOD.value
    
    # 假设有两个玩家：user_id=1 (Good, Merlin), user_id=2 (Evil, Assassin)
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
        },
        {
            "user_id": 2,
            "username": "User2",
            "seat_id": 1,
            "is_ai": False, # 为了测试 UserStats 更新，设为真人
            "character": Character.ASSASSIN.value,
            "is_connected": True,
            "has_voted": False,
            "has_acted": True
        }
    ]
    
    # 触发任务
    result = process_game_result.delay(game_id, winner, players_data)
    print(f"Sent process_game_result to stats_queue. Task ID: {result.id}")
    
    # 等待结果（仅测试用，生产环境不需要等待）
    try:
        # process_game_result 没有返回值，成功执行返回 None
        result.get(timeout=10)
        print("Stats task completed successfully!")
    except Exception as e:
        print(f"Task failed: {e}")

if __name__ == "__main__":
    test_stats_task()
