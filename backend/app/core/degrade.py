# 这个文件是 5 级降级矩阵：一个整数旋钮，越拧越狠。
#
# 为什么是一个整数，不是 5 个开关
# --------------------------
# H-1 的 `switches.py` 是"某一项功能开不开"，一项一个布尔。但事故现场需要的不是
# 5 个复选框——**5 个复选框要求操作的人在压力下自己记住依赖顺序**：
# 该先关发言还是先关热榜？关了 L4 要不要顺手关 L2？记错一步就是白降一轮。
# 降级矩阵的本质是**这些措施是有序且累积的**：越往上不仅多关一样，而且必然包含
# 下面所有级别。所以它只能是一个数字，"更严重就往上拧一格"。
# 大白话：空调不给你压缩机、风扇、除湿三个按钮，就给一个温度旋钮。
#
# 判定必须用 `>=`，不能用 `==`
# -------------------------
# 这是这类"档位"最容易写错的地方：`if level == 2: 走规则引擎` 看着没问题，
# 但**升到 3 档时 LLM 会自己开回来**——往更严重的方向拧旋钮，反而恢复了更贵的功能。
# 每个功能问的都是"我这一级到了没有"（`at_least`），不是"现在正好是我这一级吗"。
#
# 失败方向：和 H-1 开关一致，读不到就保持上次读到的值
# ------------------------------------------
# 这是**人做的决定**，所以和 `ai_queue` 那个自动降级刚好相反（那个读不到就不降）。
# 切到高档位往往正因为线上在着火，而 Redis 抖动本身就是着火的一部分——
# 这时候退回 0 档等于**旋钮在最需要它的那一刻自己弹回去了**。
# 同理这个 key 不设 TTL（降级不能自己到期恢复），但本地缓存必须有 TTL
# （AI 每回合读一次，不然就是把 Redis 挂进热路径）。
#
# 和 switches.py 是什么关系
# ----------------------
# 等级是**运维旋钮**（粗粒度、按严重程度递进），单项开关是**点名覆盖**
# （只想关这一样，不想连带关别的）。两者**都是人做的决定、生命周期相同**——
# 都要留到人来撤销——所以取"任一说停就停"是安全的合并方式。
# 真正不能和它们合并的是 `ai_queue` 那个按队列深度自动触发的降级：
# 它每回合重新判断、压力一退就自动恢复，**生命周期不同的东西不能共用一个状态位**。
import logging
import time
from typing import Any, Dict, Optional, Tuple

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.degrade")

KEY = "degrade:level"

# 本地缓存 TTL（秒）= 拧完旋钮后最久多久全局生效。同 switches.LOCAL_TTL 的口径
LOCAL_TTL = 1.0

# ---- 档位定义 ----
# 排序依据是**"这一刀砍掉多少成本"除以"玩家有多疼"**：先砍贵且不影响胜负的，
# 最后才砍"不让人进来玩"。所以 AI 发言（最贵的一次 LLM 调用、且说什么都不改变结果）
# 在最前面，拒绝新开局在最后面。
L0_NORMAL = 0
L1_NO_AI_SPEECH = 1
L2_AI_RULE_ENGINE = 2
L3_SLOW_COLD_PATH = 3
L4_QUEUE_CREATE_GAME = 4
L5_REJECT_NEW_GAME = 5

MAX_LEVEL = L5_REJECT_NEW_GAME

# 每一级砍掉什么。**这张表是给事故现场看的**：没人在半夜记得住 3 档是什么意思，
# 开关中心把它连同当前档位一起返回（见 routers/admin.py）。
MATRIX: Dict[int, str] = {
    L0_NORMAL: "正常，无降级",
    L1_NO_AI_SPEECH: "AI 发言走规则引擎（最贵的一次 LLM 调用，且不影响胜负）",
    L2_AI_RULE_ENGINE: "AI 全部决策走规则引擎（对局照常，AI 说话变套路）",
    L3_SLOW_COLD_PATH: "冷路径降频：热榜归并与回放缓存拉长间隔",
    L4_QUEUE_CREATE_GAME: "创建对局排队：建局收紧到很低的速率",
    L5_REJECT_NEW_GAME: "拒绝新开局，只服务进行中的房间",
}

# (上次读到的档位, 过期时刻)
_local: Optional[Tuple[int, float]] = None


def _observe(value: int) -> int:
    """把当前档位同步到指标上。

    **自动降级必须可观测**这条在 ai_queue 讲过；人手拧的旋钮同样要上报，
    因为"降级了"和"某个功能坏了"在现象上无法区分——没有这条曲线，
    复盘时说不清"那段时间 AI 说套话"是降级生效还是 LLM 挂了。
    """
    metrics.degrade_level.set(value)
    return value


def _parse(raw: Any) -> int:
    """脏值一律按 0 档处理：宁可不降，也不要因为一个坏值把全站拒了。

    注意这和上面"读不到保持上次的值"不冲突——**读不到**是通路故障（保持现状），
    **读到了但是垃圾**是数据错误（这个值不可信，按最轻的来）。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("降级档位值非法，按 0 档处理: %r", raw)
        return L0_NORMAL
    if not (L0_NORMAL <= value <= MAX_LEVEL):
        logger.warning("降级档位越界，按 0 档处理: %r", raw)
        return L0_NORMAL
    return value


async def level(redis=None) -> int:
    """读当前档位。永不抛异常：旋钮读不到不该让业务路径跟着挂。"""
    global _local
    now = time.monotonic()
    if _local is not None and now < _local[1]:
        return _observe(_local[0])

    client = redis or _client()
    if client is None:
        return _observe(L0_NORMAL)
    try:
        raw = await client.get(KEY)
    except Exception as e:
        logger.warning("读降级档位失败: %s", e)
        if _local is not None:
            # 过期的旧值也比"当作没降级"可信：见文件头"失败方向"
            return _observe(_local[0])
        return _observe(L0_NORMAL)

    value = L0_NORMAL if raw is None else _parse(raw)
    _local = (value, now + LOCAL_TTL)
    return _observe(value)


async def effective_level(redis=None) -> int:
    """**实际生效的档位** = max(人手拧的, 熔断器推断的)。

    为什么熔断器不直接去写 `degrade:level`
    --------------------------------
    跳闸后 `set_level(2)` 只有一行，很诱人，但它是错的：那个 key 是**人的决定**，
    刻意不设 TTL、只能由人撤销（见文件头）。熔断器往里写就出现两个后果——
      1. **机器覆盖人的决定**：人本来拧在 3 档，熔断器写个 2 就把 L3 撤销了；
      2. **撤不回去**：依赖恢复后谁来把它写回 0？写回去又可能抹掉人半夜拧的那一档，
         而"到底是谁拧的"这个信息在一个整数里根本存不下。
    所以两个来源**各自保存、读的时候取最大值**。这和 H-3c·下 那条是同一句话：
    **生命周期不同的东西不能共用一个状态位**——自动触发的必须能自动恢复，
    人手触发的必须留到人来撤销。

    取 max 是安全的合并方式：两个来源的意图都是"这一刀砍下去"，
    **谁都不该被别人的"不用降"覆盖掉**（同 AI 侧四条通路取"或"）。
    """
    manual = await level(redis)
    # 纯本地内存判断，不含 await，不会给热路径添往返
    from app.core import breaker
    value = max(manual, breaker.implied_level())
    metrics.degrade_level_effective.set(value)
    return value


async def at_least(target: int, redis=None) -> bool:
    """这一级的措施现在是否生效。

    功能侧一律用这个，**不要自己拿 `level()` 去比 `==`**：
    比 `==` 的话往更严重的档位拧反而会把这个功能放开（见文件头）。
    走 `effective_level` 而不是 `level`，所以熔断器推断出来的降级也算生效。
    """
    return await effective_level(redis) >= target


async def set_level(value: int, redis=None) -> int:
    """拧旋钮。刻意不设 TTL：降级是人做的决定，不能自己到期弹回来（同 H-1）。"""
    if not (L0_NORMAL <= value <= MAX_LEVEL):
        # **越界不夹逼要报错**：把手滑输入的 50 夹成 5，就是静默地把全站新开局拒了
        raise ValueError(f"降级档位必须在 {L0_NORMAL}~{MAX_LEVEL} 之间，收到 {value}")

    global _local
    before = await level(redis)
    client = redis or _client()
    await client.set(KEY, str(value))
    # 本进程立刻生效，别让拧旋钮的人自己还要等 LOCAL_TTL 才看到变化
    _local = (value, time.monotonic() + LOCAL_TTL)

    if value < before:
        # **一次下调多级要显式提示**：等于把攒着的流量一次性全放回来，
        # 有把刚缓过来的系统重新打回去的风险。刻意不在代码里强制"必须逐级恢复"——
        # 事故里最需要灵活性的时刻，那种约束只会挡路。
        logger.warning("降级档位下调 %d -> %d（一次放开 %d 级，注意回弹）",
                       before, value, before - value)
    else:
        logger.warning("降级档位上调 %d -> %d：%s", before, value, MATRIX[value])
    return _observe(value)


async def reset(redis=None) -> None:
    """回到 0 档（删掉运行时档位）。"""
    global _local
    client = redis or _client()
    await client.delete(KEY)
    _local = None
    logger.warning("降级档位已复位到 0 档")
    _observe(L0_NORMAL)


async def snapshot(redis=None) -> Dict[str, Any]:
    """当前档位 + 整张矩阵 + 每一级此刻是否生效，供开关中心展示。

    **开关中心的价值主要在"看得清"而不是"切得动"**：切一个数字任何人都会写，
    难的是事故里第二分钟能一眼确认"现在几档、这一档到底关了什么、我以为关掉的
    那样东西是不是真的关了"。所以这里把矩阵和生效状态一起返回。
    """
    current = await level(redis)
    return {
        "level": current,
        "description": MATRIX[current],
        "max_level": MAX_LEVEL,
        "matrix": [
            {"level": lv, "description": desc, "active": current >= lv and lv > L0_NORMAL}
            for lv, desc in sorted(MATRIX.items())
        ],
    }


async def cold_path_interval(base: float, redis=None) -> float:
    """L3：冷路径的间隔，到档就乘上倍数。

    **降频不是关掉**：榜单晚十几秒更新没人投诉，查不到榜单会被当成故障——
    冷路径省下的是"每秒都在归并/回源"那部分成本，把它整个关掉省不了更多，
    却把一个正常功能变成了报错。同理这里返回的是间隔而不是布尔，
    调用方照常跑循环、只是跑得慢些。
    """
    if await at_least(L3_SLOW_COLD_PATH, redis):
        return base * settings.DEGRADE_COLD_PATH_FACTOR
    return base


async def guard_new_game(redis=None) -> None:
    """L4/L5：建局的闸。到 L5 直接拒，到 L4 收一个**全局**配额。

    为什么这个配额是全局的：按用户的那个（`rate_limit.create_game_rate_limit`，
    10 局/小时）本来就一直在生效，而它保护不了系统容量——**一万个用户每人只建
    1 局完全合规，机器照样倒**（同 H-3a 网关层那条）。L4 要压的是"全站每秒能开
    出几局"，所以键只有一个、所有人共享。

    L4 和 L5 的差别不只是数字大小，**是给客户端的语义不同**：
      - L4 是 429 + `Retry-After`：等一会儿能建，客户端该排队重试
      - L5 是 503 + `Retry-After`：这个功能现在整个关了，别拿重试来试
    两个都必须带 `Retry-After`——不说等多久，客户端就立刻重试，
    而重试本身会变成新的峰值（同 H-3a）。
    """
    from fastapi import HTTPException

    level_now = await level(redis)

    if level_now >= L5_REJECT_NEW_GAME:
        metrics.degrade_rejects.labels(reason="level_l5").inc()
        raise HTTPException(
            status_code=503,
            detail="系统繁忙，暂停开新对局，进行中的对局不受影响",
            headers={"Retry-After": "60"},
        )

    if level_now >= L4_QUEUE_CREATE_GAME:
        from app.core import sliding_window

        remaining, retry_ms = await sliding_window.check(
            "degrade:create_game",
            settings.DEGRADE_CREATE_GAME_SECONDS,
            settings.DEGRADE_CREATE_GAME_TIMES,
        )
        if remaining < 0:
            metrics.degrade_rejects.labels(reason="level_l4").inc()
            raise HTTPException(
                status_code=429,
                detail="创建对局排队中，请稍后重试",
                headers={"Retry-After": str(max(1, -(-retry_ms // 1000)))},
            )


def clear_local_cache() -> None:
    """丢掉本进程的档位缓存（测试用，也可用于强制立刻重读）。"""
    global _local
    _local = None


def _client():
    # 延迟导入：core.redis 在导入时就会建连接池，让这个模块能被单独 import
    from app.core.redis import redis_client
    return redis_client
