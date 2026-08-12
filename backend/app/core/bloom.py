# 这个文件是布隆过滤器，用来在回源之前拦掉"根本不存在的 game_id"（缓存穿透）。
#
# 缓存穿透是什么
# --------------
# 缓存只能挡住"存在的数据"。查一个不存在的 id：L1 没有、L2 没有、回源 MySQL 也没有——
# 空结果按 F-1 的设计会被缓存下来，所以同一个不存在的 id 只穿一次。
# 但攻击者每次换一个随机 id，每个 id 都是全新的、都要穿到 MySQL，缓存等于不存在。
# 这就是穿透：**不是缓存失效，而是被查的东西压根没进过缓存。**
#
# 为什么用布隆过滤器：它的误判是单向的
# ------------------------------------
# 布隆过滤器**可能误判"存在"，绝不可能误判"不存在"**。
# 这个不对称正是它能当门卫用的全部理由：
#   - 它说"不存在" → 一定不存在 → 可以直接 404，不用碰 MySQL
#   - 它说"存在"   → 可能是误判 → 照常往下走缓存和数据源，由它们给出真实答案
# 如果方向反过来（可能漏报存在），它就一点用都没有——会把真实数据判成不存在。
# 代价是几百 KB 内存换掉全部无效回源，而误判"存在"的后果只是"这次没拦住"，完全无害。
#
# 关键前提：无假阴性是"登记齐全"的性质，不是算法的性质
# ----------------------------------------------------
# 算法只保证"加进去的一定查得到"。**没加进去的，它一样答"不存在"**——
# 而这时的"不存在"是错的。所以两种情况必须当心：
#   1. 过滤器部署之前就已存在的老房间，压根没登记过
#   2. Redis 被清过 / key 丢了，整个位图归零
# 这两种情况下拒绝请求就是把真实房间判死。所以位图为空时一律放行（见 `might_contain`），
# 且这个 key **不设 TTL**——过期归零和被清空是同一种事故。
# 这和 E-1 那个"不按存活视图过滤广播目标"是同一条推理：**判错的两个方向谁更疼。**
# 少拦一个不存在的 id 只是多一次查询；错拦一个真实房间是功能直接坏掉。
import asyncio
import hashlib
import logging

from app.core import metrics

logger = logging.getLogger("aivalon.bloom")

# 位图 key。带版本号：改了 m / k / 哈希方式，旧位图的每一位含义都变了，必须换 key 重建。
# 刻意不设 TTL——过期归零会让所有真实房间被判成不存在。
GAMES_KEY = "aivalon:bloom:games:v1"

# 位数 m = 2^20 ≈ 105 万位 = 128 KB。
# 按 n = 10 万个房间算，m/n ≈ 10.5 位/个，配 k=7 的误判率约 1%——
# 即 100 个不存在的 id 里有 1 个没拦住，多查一次库，无害。
# 取 2 的幂是为了用位与取模（& BIT_MASK），比 % 快且分布不受偏移影响。
BITS = 1 << 20
BIT_MASK = BITS - 1

# 哈希个数。k 太小误判率高，k 太大每次读写都要 k 次 GETBIT/SETBIT 且位图更快饱和。
# 7 是 m/n ≈ 10 时的近似最优值。
HASHES = 7


def _offsets(value: str) -> list:
    """算出这个值该点亮/该检查哪 7 个位。

    用 md5 而不是内置 hash()：同 C05 那个坑——`PYTHONHASHSEED` 随机化让内置 hash()
    的结果每个进程都不一样，位图是跨进程共享的，各进程算出不同的位就完全失效了。
    md5 这里只取它的分布性，不涉及安全性。

    双哈希法：从一个 16 字节摘要里切出两个 64 位整数 h1、h2，
    第 i 个位置取 h1 + i*h2。这样只做一次 md5 就能得到 k 个独立性够用的位置，
    比算 7 次 md5 便宜得多。
    """
    digest = hashlib.md5(value.encode()).digest()
    h1 = int.from_bytes(digest[:8], "big")
    h2 = int.from_bytes(digest[8:], "big") | 1   # 保证奇数，避免 h2 与 BITS 有公因子导致位置重复
    return [(h1 + i * h2) & BIT_MASK for i in range(HASHES)]


async def add(redis, value: str, key: str = GAMES_KEY) -> None:
    """登记一个值。失败只记日志：登记不上的后果是这个 id 以后会被误拦，
    所以调用方必须把它当"尽力而为"看，不能让业务因此失败——真正的兜底是下面的放行策略。"""
    if redis is None:
        return
    try:
        pipe = redis.pipeline(transaction=False)
        for offset in _offsets(value):
            pipe.setbit(key, offset, 1)
        await pipe.execute()
    except Exception as e:
        logger.warning("布隆登记失败: value=%s %s", value, e)


async def warm(redis, load_values, key: str = GAMES_KEY) -> int:
    """位图不存在时，把库里已有的值灌进去。返回登记条数（已存在则返回 0 且不查库）。

    这一步不是性能优化，是**正确性的前提**。"无假阴性"是"登记齐全"的性质而不是
    算法的性质：过滤器上线之前建的房间从没登记过，而位图一旦非空，
    文件头那条"位图为空就放行"的兜底就不再保护它们了——它们会被判成不存在。
    所以位图是空的时候必须先把库里已有的 id 灌进去，再开始拦。
    （这和已砍的"缓存预热"不是一件事：那个是让缓存提前热起来，少了只是慢；
      这个少了功能直接是错的。）

    `load_values` 传的是**函数不是列表**：位图通常已经在 Redis 里躺着（它不设 TTL），
    绝大多数次启动都不该为此扫一遍 games 表。先看 key 在不在，不在才去加载。
    多个节点同时启动会各灌一遍，SETBIT 是幂等的，重复灌无害，不用加锁。
    """
    if redis is None:
        return 0
    try:
        if await redis.exists(key):
            return 0
    except Exception as e:
        logger.warning("布隆预热检查失败，跳过预热: %s", e)
        return 0

    count = 0
    try:
        values = load_values()
        if asyncio.iscoroutine(values):
            values = await values
        pipe = redis.pipeline(transaction=False)
        for value in values:
            for offset in _offsets(value):
                pipe.setbit(key, offset, 1)
            count += 1
        if count:
            await pipe.execute()
    except Exception as e:
        # 灌不进去就别开始拦：让位图保持空，`might_contain` 会一路放行
        logger.warning("布隆预热失败，过滤器将放行全部请求: %s", e)
        return 0
    logger.info("布隆过滤器预热完成: 登记 %d 个已有房间", count)
    return count


async def might_contain(redis, value: str, key: str = GAMES_KEY) -> bool:
    """返回 False 表示**一定不存在**，可以直接拒。返回 True 表示可能存在，继续往下查。

    三种情况一律返回 True（放行）：
      - 没接 Redis（单机/测试）
      - 读位图失败：过滤器是优化，不能让它成为可用性依赖
      - 位图为空：说明还没建立或被清空过，此时的"不存在"全是假的（见文件头）
    **放行的代价是多查一次库，错拦的代价是真实房间 404，两者完全不对等。**
    """
    if redis is None:
        return True
    try:
        exists = await redis.exists(key)
        if not exists:
            # 位图还没建立：不能信它的任何"不存在"结论
            return True
        pipe = redis.pipeline(transaction=False)
        for offset in _offsets(value):
            pipe.getbit(key, offset)
        bits = await pipe.execute()
    except Exception as e:
        logger.warning("布隆查询失败，放行: value=%s %s", value, e)
        return True

    if all(bits):
        return True
    metrics.bloom_rejects.inc()
    return False
