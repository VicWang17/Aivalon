# S2 对局主链路场景：每个虚拟用户驱动一个完整对局（创建房间 → 与 7 个 AI 打完全场 → 结算）
#
# 运行示例（需要 uvicorn + Celery worker + outbox relay 全部在线）：
#   cd backend && source venv/bin/activate
#   locust -f ../bench/locust_s2.py --headless -u 10 -r 2 -t 120s --host http://localhost:8000
#
# 说明：
# - 真人动作由本驱动器轮询快照后提交；AI 动作由 Celery worker 消费队列后回写
# - 压测要求 AI_USE_LLM=False（规则引擎）与调高限流阈值（RATE_LIMIT_*），见 README
# - 8 人局任务人数配置固定为 3-4-4-5-5（与 game_rules 一致）
import json
import random
import uuid
from pathlib import Path

from locust import HttpUser, between, task

USERS_FILE = Path(__file__).parent / "users.json"
USERS = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else []

TEAM_SIZES = [3, 4, 4, 5, 5]  # 8 人局第 1~5 轮任务人数
EVIL_CHARS = {"assassin", "morgana", "minion"}
# AI 占位的假用户 ID（库中不存在即被识别为 AI，路由层会随机命名）
AI_IDS = [900001, 900002, 900003, 900004, 900005, 900006, 900007]


class S2GameUser(HttpUser):
    """一个虚拟用户 = 一个进行中的对局房间的真人驱动器"""

    wait_time = between(0.3, 0.8)

    def on_start(self):
        if not USERS:
            raise RuntimeError("bench/users.json 不存在，先运行 prepare_users.py")
        self.me = random.choice(USERS)
        self.game_id = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.me['access_token']}",
            "X-Request-ID": f"bench-s2-{uuid.uuid4().hex[:12]}",
        }

    @task
    def play(self):
        if self.game_id is None:
            self._create_game()
        else:
            self._step()

    def _create_game(self):
        payload = {"player_ids": [self.me["user_id"]] + AI_IDS}
        with self.client.post(
            "/api/v1/games/", json=payload, headers=self._headers(),
            name="POST /games (创建对局)", catch_response=True,
        ) as resp:
            if resp.status_code == 200 and resp.json().get("data", {}).get("game_id"):
                self.game_id = resp.json()["data"]["game_id"]
                resp.success()
            else:
                resp.failure(f"创建对局失败: {resp.status_code} {resp.text[:200]}")

    def _step(self):
        with self.client.get(
            f"/api/v1/games/{self.game_id}", headers=self._headers(),
            name="GET /games/{id} (轮询快照)", catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"快照获取失败: {resp.status_code}")
                self.game_id = None
                return
            state = resp.json()["data"]

        if state["phase"] == "finished":
            self.game_id = None  # 本局结束，下次 task 开新局
            return

        action = self._decide(state)
        if action:
            self.client.post(
                f"/api/v1/games/{self.game_id}/action",
                json=action, headers=self._headers(),
                name=f"POST /action ({action['action_type']})",
            )

    def _decide(self, state: dict) -> dict | None:
        """根据快照判断"现在是否轮到我行动"，返回动作或 None"""
        my_id = self.me["user_id"]
        me = next((p for p in state["players"] if p["user_id"] == my_id), None)
        if me is None:
            return None
        phase = state["phase"]

        if phase == "speech" and state.get("speaker_id") == my_id:
            return {"action_type": "speak", "payload": {"content": "过。", "is_end": True}}

        if phase == "team_proposal" and state.get("leader_id") == my_id:
            size = TEAM_SIZES[state["round"] - 1]
            others = [p["user_id"] for p in state["players"] if p["user_id"] != my_id]
            team = [my_id] + random.sample(others, size - 1)
            return {"action_type": "propose", "payload": {"target_ids": team}}

        if phase == "vote" and not me["has_voted"]:
            return {"action_type": "vote", "payload": {"option": "approve"}}

        if phase == "mission" and my_id in state.get("proposed_team", []) and not me["has_acted"]:
            result = "fail" if me.get("character") in EVIL_CHARS else "success"
            return {"action_type": "mission", "payload": {"result": result}}

        if phase == "assassination" and me.get("character") == "assassin":
            target = next(p["user_id"] for p in state["players"] if p["user_id"] != my_id)
            return {"action_type": "assassinate", "payload": {"target_id": target}}

        return None
