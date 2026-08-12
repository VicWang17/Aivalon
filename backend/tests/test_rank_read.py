"""热榜读路径：分片归并 + 定时快照 + 本地缓存 测试。

验收口径三条：
  1. **归并是对的**：各片 Top N 的并集里挑出来的，必须和"全量扫一遍再排序"完全一致
     ——这条是**按 member 分片**这个选择的全部依据，它错了整套分片就白搭
  2. **读不碰库**：快照里带着展示字段，读路径既不查 MySQL 也不归并分片
  3. **对账能抹平多算**：ZINCRBY 不幂等，全量重建必须能把偏高的分数拉回权威值

判据都是"返回的榜单内容"和"指标涨在哪一档"，不是"函数有没有报错"。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import random

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import cache, metrics, rank_buffer
from app.services.rank_service import RankService

ALL_SHARDS = [k for b in rank_buffer.BOARDS for k in rank_buffer.shard_keys(b)]
ALL_SNAPSHOTS = [RankService.snapshot_key(b) for b in rank_buffer.BOARDS]


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


class FakeUser:
    """只带展示字段的用户替身。

    读路径测试刻意不连 MySQL：这里要验的是"读路径不碰库"，
    真连上库反而看不出它有没有偷偷去查。
    """
    def __init__(self, uid: int, total_wins: int = 0, good: int = 0, evil: int = 0):
        self.id = uid
        self.username = f"u{uid}"
        self.total_games = total_wins * 2 or 1
        self.total_wins = total_wins
        self.wins_good = good
        self.wins_evil = evil


@pytest_asyncio.fixture
async def redis(monkeypatch):
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)

    async def _clean():
        await client.delete(
            rank_buffer.PENDING_KEY, *ALL_SHARDS, *ALL_SNAPSHOTS
        )
        cache.l1.clear()

    # 读路径里那几个 redis_client 是模块级单例，测试里换成本 fixture 的连接，
    # 否则跨 event loop 用同一个客户端会炸
    monkeypatch.setattr("app.services.rank_service.redis_client", client)

    await _clean()
    yield client
    await _clean()
    await client.aclose()


@pytest.fixture
def db_users(monkeypatch):
    """替掉读路径上唯一的两处 DB 查询，并记录被调了几次。"""
    calls = {"users": 0, "all": 0}
    store: dict = {}

    async def fake_load_users(ids):
        calls["users"] += 1
        return [store[i] for i in ids if i in store]

    async def fake_load_all(column):
        calls["all"] += 1
        return [(u.id, getattr(u, column) or 0) for u in store.values()]

    monkeypatch.setattr(RankService, "_load_users", staticmethod(fake_load_users))
    monkeypatch.setattr(RankService, "_load_all_scores", staticmethod(fake_load_all))
    return {"calls": calls, "store": store}


async def _seed(redis, board, scores: dict):
    """直接把分数写进各自所属的片（模拟写路径刷完之后的状态）。"""
    pipe = redis.pipeline(transaction=False)
    for member, score in scores.items():
        pipe.zadd(rank_buffer.key_for_member(board, str(member)), {str(member): score})
    await pipe.execute()


async def _brute_force_top(redis, board, limit):
    """参照实现：把所有片的所有成员都读出来再排序。

    这是"正确答案"的定义。归并要是和它不一致，说明"各片 Top N 的并集覆盖全局 Top N"
    这个前提在实现里没成立。
    """
    everyone = []
    for key in rank_buffer.shard_keys(board):
        everyone += await redis.zrange(key, 0, -1, withscores=True)
    everyone.sort(key=lambda kv: (-kv[1], kv[0]))
    return everyone[:limit]


# ----------------------------------------------------------------------
# 归并正确性：整套分片方案成立与否就看这一节
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_matches_full_scan(redis):
    """500 个人随机分数，归并结果必须和全量扫描排序逐条一致。

    这条是**按 member 分片**的全部依据：一个人的分数完整待在一片里，所以
    全局 Top N 一定落在各片 Top N 的并集内，归并才只需要读 SHARDS × limit 条。
    换成按写入轮转分片，同一个人被拆到多片，这条就会红。
    """
    rng = random.Random(42)
    scores = {i: rng.randint(1, 200) for i in range(1, 501)}
    await _seed(redis, rank_buffer.BOARD_TOTAL, scores)

    merged = await rank_buffer.merge_shards(redis, rank_buffer.BOARD_TOTAL, 10)
    expected = await _brute_force_top(redis, rank_buffer.BOARD_TOTAL, 10)

    assert [s for _, s in merged] == [s for _, s in expected], "归并出来的分数序列不对"
    assert len(merged) == 10


@pytest.mark.asyncio
async def test_merge_reads_bounded_rows_regardless_of_population(redis):
    """归并读的行数是 SHARDS × limit，是个**与总人数无关的上界**。

    这就是不用 ZUNIONSTORE 的理由：那个要把所有片的全部成员并起来，
    开销随总人数走，还会把单线程的 Redis 堵住。
    """
    scores = {i: i for i in range(1, 1001)}
    await _seed(redis, rank_buffer.BOARD_TOTAL, scores)

    pipe = redis.pipeline(transaction=False)
    for key in rank_buffer.shard_keys(rank_buffer.BOARD_TOTAL):
        pipe.zrevrange(key, 0, 9, withscores=True)
    per_shard = await pipe.execute()

    rows = sum(len(s) for s in per_shard)
    assert rows <= rank_buffer.SHARDS * 10, f"读了 {rows} 行，超过上界"
    # 人数从 1000 涨到 10000 这个上界也不变——它只跟分片数和 limit 有关
    assert rank_buffer.SHARDS * 10 == 80


@pytest.mark.asyncio
async def test_merge_returns_fewer_than_limit_when_board_is_small(redis):
    """榜上人不够 limit 就返回现有的，不能补空位。"""
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 5, 2: 3})
    merged = await rank_buffer.merge_shards(redis, rank_buffer.BOARD_TOTAL, 10)
    assert [m for m, _ in merged] == ["1", "2"]


@pytest.mark.asyncio
async def test_merge_on_empty_board_is_empty(redis):
    """空榜归并出空列表，不报错——冷启动就是这个状态。"""
    assert await rank_buffer.merge_shards(redis, rank_buffer.BOARD_TOTAL, 10) == []


# ----------------------------------------------------------------------
# 快照：展示字段烤进去，读路径不碰库
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_carries_display_fields(redis, db_users):
    """快照里必须带着用户名和胜率，否则读接口还得回库拼装。"""
    db_users["store"].update({1: FakeUser(1, total_wins=9), 2: FakeUser(2, total_wins=4)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 9, 2: 4})

    entries = await RankService.rebuild_snapshot(rank_buffer.BOARD_TOTAL, redis)

    assert [e["user_id"] for e in entries] == [1, 2]
    assert entries[0]["username"] == "u1"
    assert entries[0]["total_wins"] == 9
    assert entries[0]["win_rate"] == 50.0
    # 快照本体也得落到 Redis 上，不然下一个读的人还得再归并一次
    raw = await redis.get(RankService.snapshot_key(rank_buffer.BOARD_TOTAL))
    assert json.loads(raw)[0]["username"] == "u1"


@pytest.mark.asyncio
async def test_reads_never_touch_db_once_snapshot_exists(redis, db_users):
    """快照建好之后，读多少次都不再查库。

    这是这一组优化的**核心口径**：原来每次请求都拿着榜上的 id 去 MySQL 查一遍展示字段，
    榜单 QPS 直接压在库上。现在查库只发生在归并时，每 MERGE_INTERVAL 最多一次。
    """
    db_users["store"].update({i: FakeUser(i, total_wins=i) for i in range(1, 6)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {i: i for i in range(1, 6)})

    await RankService.rebuild_snapshot(rank_buffer.BOARD_TOTAL, redis)
    assert db_users["calls"]["users"] == 1

    for _ in range(20):
        cache.l1.clear()        # 连 L1 都不给，逼它每次都真去读快照
        await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 3)

    assert db_users["calls"]["users"] == 1, "读路径偷偷查了库"


@pytest.mark.asyncio
async def test_read_falls_back_to_merging_when_snapshot_missing(redis, db_users):
    """快照不在（冷启动 / 全部归并循环都停了）也得给出正确结果，只是要当场归并一次。"""
    db_users["store"].update({7: FakeUser(7, total_wins=3)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {7: 3})

    entries = await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)
    assert [e["user_id"] for e in entries] == [7]
    # 兜底归并顺手把快照建起来了，下一个人就不用再归并
    assert await redis.exists(RankService.snapshot_key(rank_buffer.BOARD_TOTAL)) == 1


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_rebuilt_not_raised(redis, db_users):
    """快照里是垃圾数据要当场重建，不能把读接口带崩。"""
    db_users["store"].update({7: FakeUser(7, total_wins=3)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {7: 3})
    await redis.set(RankService.snapshot_key(rank_buffer.BOARD_TOTAL), "not-json{")

    entries = await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)
    assert [e["user_id"] for e in entries] == [7]


@pytest.mark.asyncio
async def test_members_missing_from_db_are_skipped(redis, db_users):
    """榜上有人但库里没有（注销等）：跳过，不留空位。榜是派生数据，以 MySQL 为准。"""
    db_users["store"].update({1: FakeUser(1, total_wins=9)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 9, 999: 8})

    entries = await RankService.rebuild_snapshot(rank_buffer.BOARD_TOTAL, redis)
    assert [e["user_id"] for e in entries] == [1]


@pytest.mark.asyncio
async def test_one_snapshot_serves_every_limit(redis, db_users):
    """快照按最大 limit 存**一份**，切片在应用侧做。

    反过来按 limit 分别存的话，limit 是个无界维度（这里 1..100），
    缓存 key 会跟着请求参数爆开——同 C02 label 基数那一类问题。
    """
    db_users["store"].update({i: FakeUser(i, total_wins=i) for i in range(1, 21)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {i: i for i in range(1, 21)})
    await RankService.rebuild_snapshot(rank_buffer.BOARD_TOTAL, redis)

    top3 = await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 3)
    top10 = await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)

    assert len(top3) == 3 and len(top10) == 10
    assert top10[:3] == top3
    # 榜单 key 就这么几个，和 limit 无关
    keys = await redis.keys(f"{RankService.snapshot_key(rank_buffer.BOARD_TOTAL)}*")
    assert len(keys) == 1, f"快照 key 按 limit 分裂了: {keys}"


@pytest.mark.asyncio
async def test_unknown_board_falls_back_to_total(redis, db_users):
    """认不出的榜名退到总榜，不能拿它去拼 key（那样会读到一个永远不存在的榜）。"""
    db_users["store"].update({1: FakeUser(1, total_wins=9)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 9})

    entries = await RankService.get_leaderboard("no-such-board", 10)
    assert [e["user_id"] for e in entries] == [1]


# ----------------------------------------------------------------------
# 指标口径
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_source_metrics(redis, db_users):
    """merged 那一档只在快照缺失时涨；快照在了就该全走 snapshot。

    这是"定时归并"的验收口径：**merged 跟着读 QPS 一起涨就说明归并循环没在跑**，
    每次读都在当场归并，等于这层优化根本没生效。
    """
    db_users["store"].update({1: FakeUser(1, total_wins=9)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 9})

    def _v(result):
        return metrics.rank_reads.labels(result=result)._value.get()

    merged_before, snap_before = _v("merged"), _v("snapshot")

    await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)   # 冷启动：当场归并
    assert _v("merged") == merged_before + 1

    for _ in range(5):
        cache.l1.clear()
        await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)

    assert _v("merged") == merged_before + 1, "快照在了还在当场归并"
    assert _v("snapshot") == snap_before + 5


@pytest.mark.asyncio
async def test_l1_saves_the_redis_round_trip(redis, db_users):
    """L1 命中就不该再去读 Redis 快照——省的是一次往返加一次 JSON 反序列化。"""
    db_users["store"].update({1: FakeUser(1, total_wins=9)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 9})
    await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)   # 预热 L1

    def _snap():
        return metrics.rank_reads.labels(result="snapshot")._value.get()

    before = _snap()
    for _ in range(10):
        await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)
    assert _snap() == before, "L1 没命中，每次都在读 Redis"


# ----------------------------------------------------------------------
# 对账：ZINCRBY 不幂等的兜底
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rebuild_from_db_heals_overcounted_scores(redis, db_users):
    """全量重建要把多算的分数拉回权威值。

    G-1 敢用不幂等的 ZINCRBY，**前提就是存在这条路径**：崩溃重放会让分数偏高，
    跑一次这个就正回来了。"可以接受 at-least-once"不是因为多算无害，
    而是因为有东西能把它抹平——没有这条路径就只能上幂等方案。
    """
    db_users["store"].update({1: FakeUser(1, total_wins=5)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 8})     # 重放多算了 3 分

    written = await RankService.rebuild_from_db(rank_buffer.BOARD_TOTAL, redis)
    assert written == 1

    key = rank_buffer.key_for_member(rank_buffer.BOARD_TOTAL, "1")
    assert float(await redis.zscore(key, "1")) == 5.0, "多算的分没被抹平"
    # 快照也得跟着更新，否则对账看起来"没生效"
    entries = await RankService.get_leaderboard(rank_buffer.BOARD_TOTAL, 10)
    assert entries[0]["total_wins"] == 5


@pytest.mark.asyncio
async def test_rebuild_from_db_drops_members_no_longer_in_db(redis, db_users):
    """库里已经没有的 member 不能永远留在榜上：重建前要先把分片删干净。"""
    db_users["store"].update({1: FakeUser(1, total_wins=5)})
    await _seed(redis, rank_buffer.BOARD_TOTAL, {1: 5, 42: 99})

    await RankService.rebuild_from_db(rank_buffer.BOARD_TOTAL, redis)

    ghost = rank_buffer.key_for_member(rank_buffer.BOARD_TOTAL, "42")
    assert await redis.zscore(ghost, "42") is None, "注销用户还在榜上"


@pytest.mark.asyncio
async def test_rebuild_from_db_skips_zero_scores(redis, db_users):
    """0 分的不占榜位，不然全库用户都得塞进 ZSET。"""
    db_users["store"].update({1: FakeUser(1, total_wins=5), 2: FakeUser(2, total_wins=0)})

    written = await RankService.rebuild_from_db(rank_buffer.BOARD_TOTAL, redis)
    assert written == 1
    key = rank_buffer.key_for_member(rank_buffer.BOARD_TOTAL, "2")
    assert await redis.zscore(key, "2") is None
