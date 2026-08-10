# S5 热点场景：单房间大量旁观 WS 连接 + 热榜集中读，两类流量叠加
#
# 运行示例：
#   # 终端 1：广播源（向热点房间持续广播）
#   python ../bench/s3_broadcast_source.py --game-id s5-hot-room --rate 5
#   # 终端 2：混合负载（按 weight 比例分配虚拟用户）
#   S3_GAME_ID=s5-hot-room locust -f ../bench/locust_s5.py --headless -u 500 -r 50 -t 60s --host http://localhost:8000
#
# 观察点：热点房间广播压力与热榜热 key 读压力叠加时，两类流量是否互相影响
import sys
from pathlib import Path

from locust import HttpUser, between, task

sys.path.insert(0, str(Path(__file__).parent))
from locust_s3 import S3SpectatorUser  # 复用 S3 的 WS 旁观连接（S3_GAME_ID 环境变量指定房间）

import json
import random
import uuid

USERS = json.loads((Path(__file__).parent / "users.json").read_text())


class HotRoomSpectator(S3SpectatorUser):
    """热点房间旁观者（WS 长连接），占 4/5 的虚拟用户"""

    weight = 4


class HotLeaderboardReader(HttpUser):
    """热榜集中读用户，占 1/5 的虚拟用户"""

    weight = 1
    wait_time = between(0.05, 0.15)

    @task
    def leaderboard(self):
        self.client.get(
            "/api/v1/users/leaderboard?type=total&limit=10",
            headers={"X-Request-ID": f"bench-s5-{uuid.uuid4().hex[:12]}"},
            name="/api/v1/users/leaderboard",
        )

    @task
    def recent_games(self):
        self.client.get(
            "/api/v1/games/recent?limit=10",
            headers={"X-Request-ID": f"bench-s5-{uuid.uuid4().hex[:12]}"},
            name="/api/v1/games/recent",
        )
