# S1 纯 API 场景：热榜 / 最近对局 / 对局历史 读接口梯度加压
#
# 运行示例（headless）：
#   cd backend && source venv/bin/activate
#   locust -f ../bench/locustfile.py --headless -u 50 -r 10 -t 60s --host http://localhost:8000
#
# 说明：
# - 登录接口不在本场景内：它有 5次/分/IP 限流，压测登录只会触发 429，测量的是限流器而不是系统容量
# - 每个请求带 X-Request-ID: bench-s1-* 前缀，压测流量可在日志中精确筛出（trace_id 透传的实战用途）
import json
import random
import uuid
from pathlib import Path

from locust import HttpUser, between, task

USERS_FILE = Path(__file__).parent / "users.json"
USERS = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else []


def bench_headers(token: str | None = None) -> dict:
    h = {"X-Request-ID": f"bench-s1-{uuid.uuid4().hex[:12]}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class S1ApiUser(HttpUser):
    """只读 API 用户：模拟浏览热榜/最近对局/个人历史的真实读流量"""

    wait_time = between(0.05, 0.2)

    def on_start(self):
        if not USERS:
            raise RuntimeError("bench/users.json 不存在，先运行 prepare_users.py")
        self.token = random.choice(USERS)["access_token"]

    @task(5)
    def leaderboard(self):
        self.client.get(
            "/api/v1/users/leaderboard?type=total&limit=10",
            headers=bench_headers(),
            name="/api/v1/users/leaderboard",
        )

    @task(3)
    def recent_games(self):
        self.client.get(
            "/api/v1/games/recent?limit=10",
            headers=bench_headers(),
            name="/api/v1/games/recent",
        )

    @task(2)
    def my_history(self):
        self.client.get(
            "/api/v1/games/history?skip=0&limit=20",
            headers=bench_headers(self.token),
            name="/api/v1/games/history",
        )
