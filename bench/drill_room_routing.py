# D-3 房间路由演练：双节点跨机转发 + 归属节点宕机后房间迁移。
#
# 要证明的三件事：
#   1. 转发正确——从非归属节点提交动作，状态实际在归属节点的 Actor 上演进
#      （判据：房间只驻留在归属节点的 resident_games 里，另一个节点始终为空）
#   2. 两节点视图一致——同一 game_id 在两个节点上问出同一个 owner
#   3. 宕机迁移——kill 归属节点后，剩下的节点接管该房间并能继续打
#
# 用法（需先按 README 的双节点启动方式起 node-1:8000 与 node-2:8001）：
#   python drill_room_routing.py verify              # 视图一致性 + 转发正确性
#   python drill_room_routing.py takeover            # 宕机迁移（会 kill 归属节点进程）
import argparse
import json
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

NODES = {
    "node-1": "http://localhost:8000",
    "node-2": "http://localhost:8002",
}
USERS = json.loads((Path(__file__).parent / "users.json").read_text())
AI_IDS = [900001, 900002, 900003, 900004, 900005, 900006, 900007]

TEAM_SIZES = [3, 4, 4, 5, 5]
EVIL_CHARS = {"assassin", "morgana", "minion"}


def headers(me: dict) -> dict:
    return {
        "Authorization": f"Bearer {me['access_token']}",
        "X-Request-ID": f"route-{uuid.uuid4().hex[:12]}",
    }


def cluster(base: str, game_id: str | None = None) -> dict:
    params = {"game_id": game_id} if game_id else None
    r = requests.get(f"{base}/cluster", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def decide(state: dict, my_id: int) -> dict | None:
    """与 drill_kill_recovery.py 同一套判定：现在轮到我做什么"""
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


def drive(base: str, game_id: str, me: dict, budget: int, tag: str = "") -> tuple[dict, int]:
    """从指定节点提交动作。base 故意可以是非归属节点——那才是在测转发。"""
    submitted = 0
    state = {}
    deadline = time.time() + 60
    while submitted < budget and time.time() < deadline:
        r = requests.get(f"{base}/api/v1/games/{game_id}", headers=headers(me), timeout=35)
        r.raise_for_status()
        state = r.json()["data"]
        if state["phase"] == "finished":
            break
        action = decide(state, me["user_id"])
        if action:
            resp = requests.post(f"{base}/api/v1/games/{game_id}/action",
                                 json=action, headers=headers(me), timeout=35)
            if resp.status_code == 200:
                submitted += 1
                print(f"    {tag}action#{submitted} {action['action_type']:12s} "
                      f"phase={state['phase']} round={state['round']}")
            else:
                print(f"    {tag}action {action['action_type']} rejected: "
                      f"{resp.status_code} {resp.text[:120]}")
        time.sleep(0.4)
    return state, submitted


def create_game(base: str, me: dict) -> str:
    r = requests.post(f"{base}/api/v1/games/",
                      json={"player_ids": [me["user_id"]] + AI_IDS},
                      headers=headers(me), timeout=35)
    r.raise_for_status()
    return r.json()["data"]["game_id"]


def find_game_owned_by(base: str, me: dict, want_owner: str, tries: int = 8) -> tuple[str, str]:
    """建局直到落在指定节点名下。房间归属由哈希决定，不能指定，只能重试筛选。"""
    for _ in range(tries):
        game_id = create_game(base, me)
        owner = cluster(base, game_id)["query"]["owner"]
        if owner == want_owner:
            return game_id, owner
        print(f"  建局 {game_id[:8]} 归 {owner}，不是想要的 {want_owner}，重试")
    raise SystemExit(f"重试 {tries} 次仍未建到归属 {want_owner} 的房间")


def check_views_agree() -> list[str]:
    """两节点必须看到同一个存活集合，否则路由表不一致，房间会被两个节点同时认领"""
    views = {name: cluster(base) for name, base in NODES.items()}
    for name, v in views.items():
        print(f"  {name}: node_id={v.get('node_id')} live={v.get('live_nodes')} "
              f"actors={v.get('local_actors')}")
    live_sets = [tuple(v["live_nodes"]) for v in views.values()]
    if len(set(live_sets)) != 1:
        raise SystemExit(f"FAIL 两节点存活视图不一致: {live_sets}")
    print(f"  OK 存活视图一致: {live_sets[0]}")
    return list(live_sets[0])


def check_ownership_agrees(samples: int = 200) -> None:
    """同一 game_id 在两个节点上必须问出同一个 owner"""
    mismatch = 0
    for i in range(samples):
        gid = f"probe-game-{i}"
        owners = {name: cluster(base, gid)["query"]["owner"] for name, base in NODES.items()}
        if len(set(owners.values())) != 1:
            mismatch += 1
            if mismatch <= 3:
                print(f"  MISMATCH {gid}: {owners}")
    if mismatch:
        raise SystemExit(f"FAIL {mismatch}/{samples} 个房间的归属判定在两节点间不一致")
    print(f"  OK {samples} 个房间的归属判定两节点完全一致")


def cmd_verify(actions: int) -> int:
    me = random.choice(USERS)

    print("=== 1. 两节点存活视图 ===")
    live = check_views_agree()
    if len(live) < 2:
        raise SystemExit(f"FAIL 存活节点不足 2 个（{live}），请先起第二个节点")

    print("\n=== 2. 房间归属判定一致性 ===")
    check_ownership_agrees()

    print("\n=== 3. 转发正确性：从非归属节点提交动作 ===")
    # 建一个归 node-2 的房间，然后**全程从 node-1 提交**——每个动作都要被转发
    game_id, owner = find_game_owned_by(NODES["node-1"], me, "node-2")
    ingress = "node-1"
    print(f"  房间 {game_id} 归属={owner}，全程从 {ingress} 提交（每次都需转发）")

    state, submitted = drive(NODES[ingress], game_id, me, actions, tag="")
    if submitted == 0:
        raise SystemExit("FAIL 一个动作都没提交成功")

    print("\n=== 4. 判据：房间只驻留在归属节点 ===")
    resident = {name: cluster(base)["resident_games"] for name, base in NODES.items()}
    for name, games in resident.items():
        mark = "<- 房间在这里" if game_id in games else ""
        print(f"  {name}: {len(games)} 个驻留房间 {mark}")

    if game_id not in resident[owner]:
        raise SystemExit(f"FAIL 房间未驻留在归属节点 {owner}，转发没生效")
    others = [n for n in NODES if n != owner]
    for other in others:
        if game_id in resident[other]:
            raise SystemExit(
                f"FAIL 房间同时驻留在 {other}——状态出现两份副本，单写者模型已失效")

    print(f"\n结论：{submitted} 个动作从 {ingress} 提交，状态全部在 {owner} 的 Actor 上演进；"
          f"入口节点未驻留该房间。phase={state.get('phase')} round={state.get('round')}")
    return 0


def pid_of_node(node: str) -> int:
    """按端口找 uvicorn 进程。演练要 kill 的是归属节点，不能盲杀。"""
    port = NODES[node].rsplit(":", 1)[1]
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        raise SystemExit(f"FAIL 端口 {port} 上没有监听进程")
    return int(out.splitlines()[0])


def cmd_takeover(actions: int) -> int:
    me = random.choice(USERS)

    print("=== 1. 前置检查 ===")
    live = check_views_agree()
    if len(live) < 2:
        raise SystemExit(f"FAIL 存活节点不足 2 个（{live}）")

    print("\n=== 2. 建一个归 node-2 的房间并打几手 ===")
    game_id, owner = find_game_owned_by(NODES["node-1"], me, "node-2")
    survivor = next(n for n in NODES if n != owner)
    print(f"  房间 {game_id} 归属={owner}，幸存节点={survivor}")
    state, submitted = drive(NODES[survivor], game_id, me, actions, tag="kill前 ")
    before = {"phase": state.get("phase"), "round": state.get("round"),
              "speech": len(state.get("speech_history", []))}
    print(f"  kill 前: {before}，已提交 {submitted} 个动作")

    print(f"\n=== 3. kill -9 归属节点 {owner} ===")
    pid = pid_of_node(owner)
    subprocess.run(["kill", "-9", str(pid)], check=True)
    print(f"  已 kill pid={pid}（{owner}）")

    print("\n=== 4. 等待幸存节点判死并接管 ===")
    # 判死阈值 NODE_TTL = 3 × 2s = 6s，留足余量再断言
    deadline = time.time() + 20
    took_over = False
    t0 = time.time()
    while time.time() < deadline:
        v = cluster(NODES[survivor], game_id)
        if owner not in v["live_nodes"] and v["query"]["is_mine"]:
            took_over = True
            print(f"  {time.time() - t0:5.1f}s 接管完成: live={v['live_nodes']} "
                  f"owner={v['query']['owner']}")
            break
        time.sleep(0.5)
    if not took_over:
        raise SystemExit(f"FAIL 20s 内 {survivor} 未接管该房间")
    detect_secs = time.time() - t0

    print("\n=== 5. 迁移后继续打 ===")
    state, submitted2 = drive(NODES[survivor], game_id, me, actions, tag="迁移后 ")
    after = {"phase": state.get("phase"), "round": state.get("round"),
             "speech": len(state.get("speech_history", []))}
    print(f"  迁移后: {after}，续打 {submitted2} 个动作")

    if submitted2 == 0:
        raise SystemExit("FAIL 迁移后一个动作都没打成功")
    if after["speech"] < before["speech"]:
        raise SystemExit(f"FAIL 迁移后状态回退: speech {before['speech']} -> {after['speech']}")

    resident = cluster(NODES[survivor])["resident_games"]
    if game_id not in resident:
        raise SystemExit(f"FAIL 房间未驻留在接管节点 {survivor}")

    print(f"\n结论：归属节点 kill -9 后 {detect_secs:.1f}s 完成判死与接管"
          f"（NODE_TTL=6s），房间迁至 {survivor} 并续打 {submitted2} 个动作，状态未回退。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["verify", "takeover"])
    ap.add_argument("--actions", type=int, default=6)
    args = ap.parse_args()
    return cmd_verify(args.actions) if args.phase == "verify" else cmd_takeover(args.actions)


if __name__ == "__main__":
    sys.exit(main())
