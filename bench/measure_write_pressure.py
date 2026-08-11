# 量化 MySQL 写压力：取 SHOW GLOBAL STATUS 计数器在一轮压测前后的增量。
#
# 口径说明（重要，别混淆两个不同的"写压力"）：
#   - 行数维度（Innodb_rows_inserted）：Write-Behind **不会**减少落库行数——事件该持久化的
#     还是要持久化，只是晚 200ms。指望这个数下降是理解错了。
#   - 事务/请求维度（Com_commit、Com_insert 语句数）：这才是 Write-Behind 省掉的东西。
#     v1 是"一个动作一个事务"，v2 是"一批事件一个事务"，每次 commit 都要 fsync + 占一个
#     连接一个往返，这是 MySQL 写路径真正的瓶颈所在。
#
# 所以headline 指标是 **每事件的 commit 次数**，而不是行数。
#
# 用法：
#   python measure_write_pressure.py start --tag v2_u10     # 压测前打点
#   （跑 locust）
#   python measure_write_pressure.py stop  --tag v2_u10     # 压测后打点并输出增量
#   python measure_write_pressure.py diff  --a v1_u10 --b v2_u10   # 两轮对比
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402

SNAP_DIR = Path(__file__).parent / ".write_pressure"

# 关注的计数器。Com_* 是语句数，Innodb_rows_* 是行数，Handler_* 是存储引擎层调用数。
COUNTERS = [
    "Com_insert",
    "Com_update",
    "Com_select",
    "Com_commit",
    "Com_begin",
    "Queries",
    "Innodb_rows_inserted",
    "Innodb_rows_updated",
    "Handler_write",
    "Handler_update",
    "Handler_commit",
]


def snapshot() -> dict:
    db = SessionLocal()
    try:
        vals = {}
        for k in COUNTERS:
            row = db.execute(text("SHOW GLOBAL STATUS LIKE :k"), {"k": k}).fetchone()
            vals[k] = int(row[1]) if row else 0
        vals["_game_events"] = db.execute(
            text("SELECT COUNT(*) FROM game_events")
        ).scalar()
        vals["_games"] = db.execute(text("SELECT COUNT(*) FROM games")).scalar()
        vals["_outbox"] = db.execute(text("SELECT COUNT(*) FROM outbox_events")).scalar()
        vals["_t"] = time.time()
        return vals
    finally:
        db.close()


def report(tag: str, before: dict, after: dict) -> dict:
    events = after["_game_events"] - before["_game_events"]
    elapsed = after["_t"] - before["_t"]
    delta = {k: after[k] - before[k] for k in COUNTERS}
    delta["_events"] = events
    delta["_games"] = after["_games"] - before["_games"]
    delta["_outbox"] = after["_outbox"] - before["_outbox"]
    delta["_elapsed"] = elapsed

    print(f"=== 写压力测量 [{tag}] ===")
    print(f"  窗口时长      : {elapsed:.1f}s")
    print(f"  新增对局      : {delta['_games']}")
    print(f"  新增事件行    : {events}")
    print(f"  新增 outbox 行: {delta['_outbox']}")
    print("  --- MySQL 计数器增量 ---")
    for k in COUNTERS:
        rate = delta[k] / elapsed if elapsed else 0
        per_ev = delta[k] / events if events else 0
        print(f"  {k:24s} {delta[k]:>8d}   ({rate:7.1f}/s, {per_ev:6.3f}/事件)")

    if events:
        print("  --- 归一化（headline）---")
        print(f"  每事件 commit 次数     : {delta['Com_commit'] / events:.4f}")
        print(f"  每事件 insert 语句数   : {delta['Com_insert'] / events:.4f}")
        print(f"  每事件落库行数         : {delta['Innodb_rows_inserted'] / events:.4f}")
    return delta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["start", "stop", "diff"])
    ap.add_argument("--tag", default="run")
    ap.add_argument("--a", help="diff: 基线 tag")
    ap.add_argument("--b", help="diff: 对比 tag")
    args = ap.parse_args()
    SNAP_DIR.mkdir(exist_ok=True)

    if args.phase == "start":
        snap = snapshot()
        (SNAP_DIR / f"{args.tag}.before.json").write_text(json.dumps(snap))
        print(f"已打点 [{args.tag}] before: events={snap['_game_events']}")
        return 0

    if args.phase == "stop":
        before = json.loads((SNAP_DIR / f"{args.tag}.before.json").read_text())
        after = snapshot()
        (SNAP_DIR / f"{args.tag}.after.json").write_text(json.dumps(after))
        delta = report(args.tag, before, after)
        (SNAP_DIR / f"{args.tag}.delta.json").write_text(json.dumps(delta))
        return 0

    # diff：两轮对比，算下降比例
    da = json.loads((SNAP_DIR / f"{args.a}.delta.json").read_text())
    db_ = json.loads((SNAP_DIR / f"{args.b}.delta.json").read_text())
    print(f"=== 对比：{args.a}(基线) → {args.b} ===")
    print(f"  事件行数 {da['_events']} → {db_['_events']}")
    print(f"  {'指标':<26} {'基线/事件':>12} {'新/事件':>12} {'下降':>10}")
    for k in ["Com_commit", "Com_insert", "Com_update", "Queries",
              "Innodb_rows_inserted", "Handler_commit"]:
        a_per = da[k] / da["_events"] if da["_events"] else 0
        b_per = db_[k] / db_["_events"] if db_["_events"] else 0
        drop = (1 - b_per / a_per) * 100 if a_per else 0
        print(f"  {k:<26} {a_per:>12.4f} {b_per:>12.4f} {drop:>9.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
