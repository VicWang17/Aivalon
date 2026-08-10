# S3 长连接场景：梯度建立 WebSocket 连接，测单节点连接上限与广播延迟分布
#
# 运行示例：
#   # 终端 1：广播源（任意 room id，连接聚众到同一房间模拟热点）
#   python ../bench/s3_broadcast_source.py --game-id s3-hot-room --rate 5
#   # 终端 2：Locust 连接压测（1000 连接，每秒新建 50 条）
#   locust -f ../bench/locust_s3.py --headless -u 1000 -r 50 -t 60s --host ws://localhost:8000
#
# 测量口径：
# - "WS connect"：握手 + 鉴权耗时（连接建立成功率是单节点上限的直接证据）
# - "WS recv"：广播端到端延迟 = 客户端收到时刻 - 消息 payload 里的服务端发出时刻 ts
import json
import os
import random
import time
import uuid
from pathlib import Path

import websocket
from locust import User, between, task

USERS_FILE = Path(__file__).parent / "users.json"
USERS = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else []

GAME_ID = os.environ.get("S3_GAME_ID", "s3-hot-room")


class S3SpectatorUser(User):
    """一个虚拟用户 = 一条 WS 长连接（旁观热点房间）"""

    wait_time = between(0.05, 0.2)

    def on_start(self):
        if not USERS:
            raise RuntimeError("bench/users.json 不存在，先运行 prepare_users.py")
        self.token = random.choice(USERS)["access_token"]
        self.ws = None
        self._connect()

    def _connect(self):
        url = f"{self.host}/api/v1/ws/games/{GAME_ID}?token={self.token}"
        start = time.perf_counter()
        try:
            self.ws = websocket.create_connection(url, timeout=10)
            elapsed = (time.perf_counter() - start) * 1000
            self.environment.events.request.fire(
                request_type="WS", name="connect",
                response_time=elapsed, response_length=0, exception=None,
            )
        except Exception as e:
            self.environment.events.request.fire(
                request_type="WS", name="connect",
                response_time=(time.perf_counter() - start) * 1000,
                response_length=0, exception=e,
            )
            self.ws = None

    @task
    def listen(self):
        if self.ws is None:
            self._connect()
            if self.ws is None:
                return
        try:
            raw = self.ws.recv()
            now = time.time()
            msg = json.loads(raw)
            ts = (msg.get("payload") or {}).get("ts")
            if ts:
                self.environment.events.request.fire(
                    request_type="WS", name="recv (广播延迟)",
                    response_time=(now - float(ts)) * 1000,
                    response_length=len(raw), exception=None,
                )
        except Exception as e:
            self.environment.events.request.fire(
                request_type="WS", name="recv (广播延迟)",
                response_time=0, response_length=0, exception=e,
            )
            self.ws = None  # 断线，下个 task 重连

    def on_stop(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
