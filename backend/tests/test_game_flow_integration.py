"""
主链路集成测试：创建对局 → 完整对局 → 结算 → 历史/回放可查。

运行前提（不满足则自动 skip）：
  1. 完整环境在线：uvicorn(8000) + Celery worker + outbox relay + 中间件
  2. AI 走规则引擎：AI_USE_LLM=false，AI_TASK_RATE_LIMIT 调高（见 bench/README.md）
  3. bench/users.json 存在（先运行 bench/prepare_users.py）

这是 D 组热路径重构的回归保护网：重构期间每次改动后跑一遍，确认对局语义没有被破坏。
"""
import json
import random
import time
from pathlib import Path

import pytest
import requests

BASE = "http://localhost:8000"
USERS_FILE = Path(__file__).resolve().parent.parent.parent / "bench" / "users.json"

TEAM_SIZES = [3, 4, 4, 5, 5]  # 8 人局第 1~5 轮任务人数
EVIL_CHARS = {"assassin", "morgana", "minion"}
AI_IDS = [900001, 900002, 900003, 900004, 900005, 900006, 900007]


def _env_ready() -> bool:
    if not USERS_FILE.exists():
        return False
    try:
        return requests.get(f"{BASE}/health", timeout=2).status_code == 200
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(
    not _env_ready(),
    reason="需要完整环境（uvicorn+worker+relay，AI_USE_LLM=false）与 bench/users.json",
)


def _decide(state: dict, my_id: int) -> dict | None:
    """与 bench/locust_s2.py 相同的真人驱动逻辑：轮到我则返回动作，否则 None"""
    me = next((p for p in state["players"] if p["user_id"] == my_id), None)
    if me is None:
        return None
    phase = state["phase"]

    if phase == "speech" and state.get("speaker_id") == my_id:
        return {"action_type": "speak", "payload": {"content": "过。", "is_end": True}}
    if phase == "team_proposal" and state.get("leader_id") == my_id:
        size = TEAM_SIZES[state["round"] - 1]
        others = [p["user_id"] for p in state["players"] if p["user_id"] != my_id]
        return {"action_type": "propose", "payload": {"target_ids": [my_id] + random.sample(others, size - 1)}}
    if phase == "vote" and not me["has_voted"]:
        return {"action_type": "vote", "payload": {"option": "approve"}}
    if phase == "mission" and my_id in state.get("proposed_team", []) and not me["has_acted"]:
        result = "fail" if me.get("character") in EVIL_CHARS else "success"
        return {"action_type": "mission", "payload": {"result": result}}
    if phase == "assassination" and me.get("character") == "assassin":
        target = next(p["user_id"] for p in state["players"] if p["user_id"] != my_id)
        return {"action_type": "assassinate", "payload": {"target_id": target}}
    return None


def test_full_game_flow():
    """完整对局闭环：创建 → 打完 → 结算 → 历史可见 → 回放事件流有序"""
    user = json.loads(USERS_FILE.read_text())[0]
    headers = {"Authorization": f"Bearer {user['access_token']}"}
    my_id = user["user_id"]

    # 1. 创建对局
    resp = requests.post(
        f"{BASE}/api/v1/games/",
        json={"player_ids": [my_id] + AI_IDS},
        headers=headers, timeout=30,
    )
    assert resp.status_code == 200, f"创建对局失败: {resp.text[:200]}"
    game_id = resp.json()["data"]["game_id"]

    # 2. 驱动对局直到结束（AI 由 worker 驱动；超时 180s 防卡死）
    state = None
    deadline = time.time() + 180
    while time.time() < deadline:
        resp = requests.get(f"{BASE}/api/v1/games/{game_id}", headers=headers, timeout=10)
        assert resp.status_code == 200, f"快照获取失败: {resp.status_code}"
        state = resp.json()["data"]
        if state["phase"] == "finished":
            break
        action = _decide(state, my_id)
        if action:
            requests.post(
                f"{BASE}/api/v1/games/{game_id}/action",
                json=action, headers=headers, timeout=10,
            )
        time.sleep(0.3)

    assert state and state["phase"] == "finished", "180s 内对局未完成"
    assert state["winner"] in ("good", "evil"), f"胜者异常: {state['winner']}"

    # 3. 历史列表可见该局（Write-Behind 下有 ≤200ms 刷库延迟，短轮询等待可见性）
    deadline = time.time() + 10
    while True:
        resp = requests.get(f"{BASE}/api/v1/games/history", headers=headers, timeout=10)
        assert resp.status_code == 200
        history = resp.json()["data"]
        if any(g["id"] == game_id for g in history):
            break
        assert time.time() < deadline, "历史列表 10s 内未出现本局（flusher 未工作？）"
        time.sleep(0.5)

    # 4. 回放事件流：非空且 seq 单调递增（同样等待刷库可见）
    deadline = time.time() + 10
    while True:
        resp = requests.get(f"{BASE}/api/v1/games/{game_id}/events", headers=headers, timeout=10)
        assert resp.status_code == 200
        events = resp.json()["data"]
        if events:
            break
        assert time.time() < deadline, "事件流 10s 内为空（flusher 未工作？）"
        time.sleep(0.5)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), f"事件 seq 乱序: {seqs[:20]}"
