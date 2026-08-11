# 这个文件是房间路由的一致性哈希环：把 game_id 映射到某个节点，节点增删时只迁移少量房间。
#
# 为什么不用取模（hash(game_id) % N）：
#   取模的分母是节点数，一旦 N 变化，几乎所有 key 的归属都会变——3 个节点扩到 4 个，
#   理论迁移量 3/4。房间迁移意味着状态搬迁 + Actor 重建，代价很高。
#   一致性哈希把节点和 key 映射到同一个环上，key 归属"顺时针遇到的第一个节点"，
#   删掉一个节点只影响它自己那段弧，期望迁移量 1/N。
#
# 为什么需要虚拟节点：
#   物理节点直接上环时，3 个点把环切成 3 段，段长随机且极不均匀（实测标准差可达 50%+）。
#   每个物理节点放 VNODES 个虚拟节点，等于用大数定律把弧长抹平——这是工业界标配做法。
#
# 哈希函数为什么不用内置 hash()：
#   CPython 对 str 的 hash 加了每进程随机盐（PYTHONHASHSEED），同一个 game_id 在两个
#   进程里算出的值不同 → 路由表在节点间不一致，房间会被两个节点同时认领。
#   必须用密码学哈希这类跨进程稳定的函数，这里用 md5（只取分布性，不涉及安全性）。
import bisect
import hashlib
from typing import Dict, List, Optional

# 每个物理节点的虚拟节点数：越大分布越均匀，但环上点数 = 节点数 × VNODES，查找是 O(log) 影响很小。
# 160 是 libketama 的经典取值，实测 3~10 节点下分布标准差可压到 5% 以内。
VNODES = 160


def _hash(key: str) -> int:
    """稳定哈希：跨进程、跨重启结果一致（内置 hash() 有随机盐，不能用）"""
    return int(hashlib.md5(key.encode()).hexdigest(), 16)


class HashRing:
    """一致性哈希环：节点增删只影响相邻弧段上的 key"""

    def __init__(self, nodes: Optional[List[str]] = None, vnodes: int = VNODES):
        self._vnodes = vnodes
        # 有序环：_keys 升序存哈希值（供 bisect 二分），_ring 映射哈希值 -> 节点名
        self._keys: List[int] = []
        self._ring: Dict[int, str] = {}
        self._nodes: set = set()
        for node in nodes or []:
            self.add_node(node)

    def add_node(self, node: str) -> None:
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self._vnodes):
            h = _hash(f"{node}#{i}")
            # 哈希碰撞概率极低，但撞上就跳过，避免覆盖别的节点的虚拟节点
            if h in self._ring:
                continue
            self._ring[h] = node
            bisect.insort(self._keys, h)

    def remove_node(self, node: str) -> None:
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        for i in range(self._vnodes):
            h = _hash(f"{node}#{i}")
            if self._ring.get(h) == node:
                del self._ring[h]
                idx = bisect.bisect_left(self._keys, h)
                if idx < len(self._keys) and self._keys[idx] == h:
                    self._keys.pop(idx)

    def get_node(self, key: str) -> Optional[str]:
        """key 归属节点：环上顺时针第一个虚拟节点所属的物理节点"""
        if not self._keys:
            return None
        h = _hash(key)
        idx = bisect.bisect_right(self._keys, h)
        # 越过环尾则回绕到环首（环是闭合的）
        if idx == len(self._keys):
            idx = 0
        return self._ring[self._keys[idx]]

    @property
    def nodes(self) -> List[str]:
        return sorted(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)
