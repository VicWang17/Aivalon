# S3 广播源：以固定速率向指定房间触发真实广播（走 /ai_thinking 内部接口 → manager.broadcast）
#
# 用法：
#   python s3_broadcast_source.py --game-id <room_id> --rate 5   # 每秒 5 条广播
#
# 说明：广播链路不要求房间真实存在（manager 按 game_id 做字典 key），
# 因此 S3 可以用任意 room id 聚众连接，广播源独立控制发送速率——发送与接收解耦，便于梯度实验。
import argparse
import time

import requests

BASE_URL = "http://localhost:8000"
# 内部接口鉴权（与 app.routers.game 的 x_internal_secret 校验一致）
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.core.config import settings


def main():
    parser = argparse.ArgumentParser(description="S3 广播源")
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--rate", type=float, default=5.0, help="每秒广播条数")
    args = parser.parse_args()

    interval = 1.0 / args.rate
    url = f"{BASE_URL}/api/v1/games/{args.game_id}/ai_thinking"
    headers = {"X-Internal-Secret": settings.SECRET_KEY}
    count = 0
    print(f"广播源启动：room={args.game_id} rate={args.rate}/s")
    try:
        while True:
            resp = requests.post(url, json={"player_id": 900001}, headers=headers, timeout=5)
            count += 1
            if resp.status_code != 200:
                print(f"[{count}] 广播失败: {resp.status_code} {resp.text[:100]}")
            elif count % 50 == 0:
                print(f"[{count}] 已发送")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"广播源停止，共发送 {count} 条")


if __name__ == "__main__":
    main()
