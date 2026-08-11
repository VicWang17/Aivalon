import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess
import uuid
from collections import Counter

from app.core.hash_ring import HashRing, _hash


def _sample_keys(n: int = 10000) -> list:
    """固定种子的 key 集合：保证每次运行测的是同一批房间 id，结果可复现"""
    import random
    rnd = random.Random(42)
    return [f"game-{rnd.getrandbits(64):016x}" for _ in range(n)]


def test_empty_ring_returns_none():
    """空环不崩：没有健康节点时返回 None，由调用方决定降级策略"""
    assert HashRing().get_node("g1") is None


def test_same_key_same_node():
    """同一 key 必落同一节点——路由的基本要求，否则一个房间会被多节点认领"""
    ring = HashRing(["node-a", "node-b", "node-c"])
    key = "game-42"
    assert len({ring.get_node(key) for _ in range(100)}) == 1


def test_hash_is_stable_across_processes():
    """哈希跨进程稳定：内置 hash() 有随机盐，多节点算出的路由表会不一致。
    起一个子进程重算同一个 key 的哈希值，必须与本进程相同。"""
    key = "game-cross-process"
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from app.core.hash_ring import _hash; print(_hash(%r))"
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), key)
    )
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert int(out) == _hash(key)


def test_distribution_is_balanced():
    """虚拟节点的意义：3 个物理节点分布要接近均匀。
    物理节点直接上环时弧长极不均匀，虚拟节点用大数定律抹平。"""
    nodes = ["node-a", "node-b", "node-c"]
    ring = HashRing(nodes)
    counts = Counter(ring.get_node(k) for k in _sample_keys())

    assert set(counts) == set(nodes)  # 没有节点被饿死
    ideal = 10000 / len(nodes)
    worst = max(abs(c - ideal) / ideal for c in counts.values())
    # 160 虚拟节点下实测偏差在 5% 量级；留 15% 余量避免测试脆弱
    assert worst < 0.15, f"分布偏差 {worst:.1%} 过大: {dict(counts)}"


def test_removing_node_migrates_only_its_share():
    """一致性哈希的核心价值：摘掉 1/N 个节点，只有约 1/N 的 key 需要迁移。
    取模路由在这里会迁移约 (N-1)/N——这就是不能用取模的原因。"""
    nodes = [f"node-{i}" for i in range(4)]
    keys = _sample_keys()
    ring = HashRing(nodes)
    before = {k: ring.get_node(k) for k in keys}

    ring.remove_node("node-0")
    after = {k: ring.get_node(k) for k in keys}

    moved = [k for k in keys if before[k] != after[k]]
    # 迁移的必须全部是原本属于被摘节点的 key：其余节点的归属不受影响
    assert all(before[k] == "node-0" for k in moved)
    ratio = len(moved) / len(keys)
    assert 0.15 < ratio < 0.35, f"迁移比例 {ratio:.1%} 不在 1/4 附近"


def test_adding_node_migrates_only_its_share():
    """扩容对称成立：加一个节点，只从其他节点各匀走一小部分，约 1/(N+1)"""
    nodes = [f"node-{i}" for i in range(3)]
    keys = _sample_keys()
    ring = HashRing(nodes)
    before = {k: ring.get_node(k) for k in keys}

    ring.add_node("node-new")
    after = {k: ring.get_node(k) for k in keys}

    moved = [k for k in keys if before[k] != after[k]]
    # 新增节点只会"抢"key，不会导致存量节点之间互相搬迁
    assert all(after[k] == "node-new" for k in moved)
    ratio = len(moved) / len(keys)
    assert 0.15 < ratio < 0.35, f"迁移比例 {ratio:.1%} 不在 1/4 附近"


def test_node_add_remove_is_idempotent():
    """重复增删幂等：心跳续约/宕机判定可能重复触发同一操作，环不能被搞乱"""
    ring = HashRing(["node-a", "node-b"])
    keys = _sample_keys(1000)
    snapshot = {k: ring.get_node(k) for k in keys}

    ring.add_node("node-a")      # 重复添加
    ring.remove_node("node-zzz")  # 删除不存在的节点
    assert ring.nodes == ["node-a", "node-b"]
    assert {k: ring.get_node(k) for k in keys} == snapshot


def test_remove_then_readd_restores_routing():
    """节点摘除后重新加回，路由必须回到原样——虚拟节点由节点名派生而非随机生成。
    这条保证宕机节点恢复上线后房间会漂回去，不留永久倾斜。"""
    ring = HashRing(["node-a", "node-b", "node-c"])
    keys = _sample_keys(1000)
    before = {k: ring.get_node(k) for k in keys}

    ring.remove_node("node-b")
    ring.add_node("node-b")

    assert {k: ring.get_node(k) for k in keys} == before


def test_last_node_removal_empties_ring():
    """摘掉最后一个节点后环为空，不残留悬空虚拟节点"""
    ring = HashRing(["only"])
    ring.remove_node("only")
    assert len(ring) == 0 and ring.get_node(str(uuid.uuid4())) is None
