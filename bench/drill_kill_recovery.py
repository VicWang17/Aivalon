# D-2 宕机恢复演练：kill -9 API 进程 → 重启 → 对局从 Redis 快照恢复继续打，并量化 RPO。
#
# Write-Behind 下的持久化点是 Redis（事件入 Stream + 状态快照同一个 MULTI/EXEC 事务），
# MySQL 由 flusher 每 200ms 批量补写。所以：
#   - kill API 进程：Redis 完好 → 对局状态与事件都不丢，恢复后可继续（本演练验证）
#   - RPO 窗口 = 已入 Redis 但未刷到 MySQL 的事件数（flusher 游标之后的 Stream 长度）
#
# 用法（三段，中间由调用方 kill/重启 API）：
#   python drill_kill_recovery.py play    --actions 8   # 建局并打若干动作，记录 game_id
#   python drill_kill_recovery.py measure               # 量化 RPO（Redis 未刷 vs MySQL 已落）
#   python drill_kill_recovery.py resume  --actions 8   # 重启后恢复续打，校验事件不丢不重
import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from pathlib import Path

import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core import event_journal  # noqa: E402
from app.core.redis import get_new_redis_client  # noqa: E402
from app.db.base import SessionLocal  # noqa: E402

BASE = "http://localhost:8000"
STATE_FILE = Path(__file__).parent / ".drill_state.json"
USERS = json.loads((Path(__file__).parent / "users.json").read_text())

TEAM_SIZES = [3, 4, 4, 5, 5]
EVIL_CHARS = {"assassin", "morgana", "minion"}
AI_IDS = [900001, 900002, 900003, 900004, 900005, 900006, 900007]


def headers(me: dict) -> dict:
    return {
        "Authorization": f"Bearer {me['access_token']}",
        "X-Request-ID": f"drill-{uuid.uuid4().hex[:12]}",
    }


def decide(state: dict, my_id: int) -> dict | None:
    """与 locust_s2.py 同一套判定：现在轮到我做什么"""
    me = next((p for p in state["players"] if p["user_id"] == my_id), None)
    if me is None:
        return None
    phase = state["phase"]
    if phase == "speech" and state.get("speaker_id") == my_id:
        return {"action_type": "speak", "payload": {"content": "过。", "is_end": True}}
    if phase == "team_proposal" and state.get("leader_id") == my_id:
        size = TEAM_SIZES[state["round"] - 1]
        others = [p["user_id"] for p in state["players"] if p["user_id"] != my_id]
        return {"action_type": "propose",
                "payload": {"target_ids": [my_id] + random.sample(others, size - 1)}}
    if phase == "vote" and not me["has_voted"]:
        return {"action_type": "vote", "payload": {"option": "approve"}}
    if phase == "mission" and my_id in state.get("proposed_team", []) and not me["has_acted"]:
        return {"action_type": "mission",
                "payload": {"result": "fail" if me.get("character") in EVIL_CHARS else "success"}}
    if phase == "assassination" and me.get("character") == "assassin":
        target = next(p["user_id"] for p in state["players"] if p["user_id"] != my_id)
        return {"action_type": "assassinate", "payload": {"target_id": target}}
    return None


def drive(game_id: str, me: dict, budget: int) -> dict:
    """轮询快照并在轮到自己时提交动作，最多提交 budget 个动作。返回最后一次快照。"""
    submitted = 0
    state = {}
    deadline = time.time() + 60
    while submitted < budget and time.time() < deadline:
        r = requests.get(f"{BASE}/api/v1/games/{game_id}", headers=headers(me), timeout=35)
        r.raise_for_status()
        state = r.json()["data"]
        if state["phase"] == "finished":
            break
        action = decide(state, me["user_id"])
        if action:
            resp = requests.post(f"{BASE}/api/v1/games/{game_id}/action",
                                 json=action, headers=headers(me), timeout=35)
            if resp.status_code == 200:
                submitted += 1
                print(f"  action#{submitted} {action['action_type']:12s} -> phase={state['phase']}")
            else:
                print(f"  action {action['action_type']} rejected: "
                      f"{resp.status_code} {resp.text[:120]}")
        time.sleep(0.4)
    print(f"  submitted {submitted} actions, phase={state.get('phase')}, "
          f"round={state.get('round')}")
    return state


async def measure(game_id: str | None) -> dict:
    """量化：Redis 未刷事件数（RPO 窗口）与 MySQL 已落事件数"""
    redis = get_new_redis_client()
    cursor = await redis.get(event_journal.JOURNAL_CURSOR) or "0-0"
    unflushed = await redis.xrange(event_journal.JOURNAL_STREAM, min=f"({cursor}")
    stream_len = await redis.xlen(event_journal.JOURNAL_STREAM)
    snapshot_exists = bool(await redis.get(f"game:{game_id}:state")) if game_id else False

    db = SessionLocal()
    try:
        if game_id:
            mysql_events = db.execute(
                text("SELECT COUNT(*) FROM game_events WHERE game_id = :g"), {"g": game_id}
            ).scalar()
            distinct_seq = db.execute(
                text("SELECT COUNT(DISTINCT seq) FROM game_events WHERE game_id = :g"),
                {"g": game_id}
            ).scalar()
            max_seq = db.execute(
                text("SELECT COALESCE(MAX(seq), -1) FROM game_events WHERE game_id = :g"),
                {"g": game_id}
            ).scalar()
        else:
            mysql_events = distinct_seq = max_seq = None
    finally:
        db.close()

    unflushed_mine = sum(1 for _, f in unflushed if f.get("game_id") == game_id)
    return {
        "cursor": cursor,
        "stream_len": stream_len,
        "unflushed_total": len(unflushed),
        "unflushed_this_game": unflushed_mine,
        "snapshot_exists": snapshot_exists,
        "mysql_events": mysql_events,
        "mysql_distinct_seq": distinct_seq,
        "mysql_max_seq": max_seq,
    }


async def watch(seconds: float, interval: float) -> None:
    """
    RPO 量化：高频采样"已入 Redis Stream 但未被 flusher 刷入 MySQL"的事件数。
    这个数就是 Redis 整体丢失时的丢失窗口——峰值即最坏 RPO。
    需要在有负载时跑（单独跑没有动作，窗口恒为 0）。
    """
    redis = get_new_redis_client()
    samples: list[int] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        cursor = await redis.get(event_journal.JOURNAL_CURSOR) or "0-0"
        pending = await redis.xrange(event_journal.JOURNAL_STREAM, min=f"({cursor}")
        samples.append(len(pending))
        await asyncio.sleep(interval)

    nonzero = [s for s in samples if s > 0]
    samples_sorted = sorted(samples)
    p95 = samples_sorted[int(len(samples_sorted) * 0.95)] if samples_sorted else 0
    print(f"--- RPO 窗口采样（{seconds:.0f}s / 间隔 {interval:.2f}s / {len(samples)} 次）---")
    print(f"  峰值未刷事件数 : {max(samples) if samples else 0}   <- 最坏 RPO（条）")
    print(f"  P95            : {p95}")
    print(f"  均值           : {sum(samples) / len(samples):.2f}" if samples else "  均值: n/a")
    print(f"  非零采样占比   : {len(nonzero)}/{len(samples)} "
          f"({100 * len(nonzero) / len(samples):.1f}%)" if samples else "")
    print(f"  刷库间隔       : 200ms（FLUSH_INTERVAL），即时间维度 RPO ≤ ~200ms")


def print_measure(tag: str, m: dict) -> None:
    print(f"--- {tag} ---")
    print(f"  Redis 快照存在      : {m['snapshot_exists']}")
    print(f"  Redis 未刷事件(全局): {m['unflushed_total']}  (本局 {m['unflushed_this_game']})")
    print(f"  Redis Stream 长度   : {m['stream_len']}   游标={m['cursor']}")
    print(f"  MySQL 本局事件数    : {m['mysql_events']}  "
          f"(distinct seq={m['mysql_distinct_seq']}, max seq={m['mysql_max_seq']})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["play", "measure", "resume", "watch"])
    ap.add_argument("--actions", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=60.0, help="watch: 采样时长")
    ap.add_argument("--interval", type=float, default=0.1, help="watch: 采样间隔")
    args = ap.parse_args()

    if args.phase == "watch":
        # RPO 量化：在负载下高频采样"已入 Redis 但未刷入 MySQL"的事件数，
        # 峰值即宕机（Redis 丢失）时的最坏丢失窗口。
        asyncio.run(watch(args.seconds, args.interval))
        return 0

    if args.phase == "play":
        me = random.choice(USERS)
        r = requests.post(f"{BASE}/api/v1/games/",
                          json={"player_ids": [me["user_id"]] + AI_IDS},
                          headers=headers(me), timeout=35)
        r.raise_for_status()
        game_id = r.json()["data"]["game_id"]
        print(f"created game {game_id} (driver user_id={me['user_id']})")
        state = drive(game_id, me, args.actions)
        STATE_FILE.write_text(json.dumps({
            "game_id": game_id, "user_id": me["user_id"],
            "phase_before_kill": state.get("phase"),
            "round_before_kill": state.get("round"),
            "speech_len_before_kill": len(state.get("speech_history", [])),
        }))
        print_measure("kill 前", asyncio.run(measure(game_id)))
        return 0

    saved = json.loads(STATE_FILE.read_text())
    game_id = saved["game_id"]

    if args.phase == "measure":
        print_measure("测量", asyncio.run(measure(game_id)))
        print(f"  (kill 前记录: phase={saved['phase_before_kill']}, "
              f"round={saved['round_before_kill']}, "
              f"speech={saved['speech_len_before_kill']})")
        return 0

    # resume：重启后验证状态恢复 + 继续打
    me = next(u for u in USERS if u["user_id"] == saved["user_id"])
    r = requests.get(f"{BASE}/api/v1/games/{game_id}", headers=headers(me), timeout=35)
    if r.status_code != 200:
        print(f"FAIL 恢复读取失败: {r.status_code} {r.text[:200]}")
        return 1
    state = r.json()["data"]
    print(f"恢复读取成功: phase={state['phase']} round={state['round']} "
          f"speech={len(state.get('speech_history', []))} "
          f"(kill 前: phase={saved['phase_before_kill']} "
          f"round={saved['round_before_kill']} "
          f"speech={saved['speech_len_before_kill']})")

    state = drive(game_id, me, args.actions)
    m = asyncio.run(measure(game_id))
    print_measure("续打后", m)
    dup = m["mysql_events"] - m["mysql_distinct_seq"]
    print(f"\n结论：事件重复行={dup}（应为 0）；"
          f"MySQL 事件数={m['mysql_events']}，max seq={m['mysql_max_seq']}")
    return 0 if dup == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
