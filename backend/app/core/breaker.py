# 这个文件是依赖熔断器：某个外部依赖持续不行了，就**先别调它**，直接走兜底。
#
# 已经有 H-2 的舱壁了，为什么还要熔断
# ------------------------------
# 舱壁（`wait_for` + 回落规则引擎）保证**单次调用有上界**，但它每次都要
# **付满那个上界**才知道这次也不行。LLM 挂掉的时候，每个 AI 回合照旧等 45 秒
# 再回落——对局能推进，但每一步慢 45 秒，玩家体验上和卡死没区别。
# **舱壁约束单次的最坏值，熔断约束"总共白等多久"**：学到依赖已经不行之后，
# 后面的调用一次都不等，立刻走兜底。
# 大白话：舱壁是每次敲门最多等 45 秒，熔断是发现家里没人就先别敲了。
#
# 状态放进程内，不放 Redis
# ---------------------
# 熔断器存在的意义就是**少打一次外部调用**。把状态放 Redis，等于为了省一次
# LLM 调用先付一次 Redis 往返——**保护机制自己成了新的延迟来源**
# （同 H-1 本地缓存必须有 TTL、同 ai_queue 刻意不问 broker 要 message_count）。
# 代价是每个进程各自学：M 个进程最坏各付 `min_samples` 次失败才都跳闸。
# 这和 F-4 singleflight "进程内互斥不是集群级" 是同一笔取舍——
# **误差有上界，而省下的往返落在每一次调用上**。
# 也不用加锁：这些方法内部没有一个 `await`，单线程事件循环里跑不到一半被打断。
#
# 判定按窗口内的失败比例，不按"连续 N 次"
# --------------------------------
# "连续 5 次失败就跳闸"在依赖半死不活时永远不跳：成功和失败交替出现，
# 连续计数一直被清零，而实际上一半的请求都在付满超时。
# 但比例判定有个前提——**必须要求最小样本数**：1 次调用里失败 1 次是 100% 失败率，
# 少了这条，第一次网络抖动就能把依赖整个摘掉（同 C07：比例和分位数都有样本量前提）。
#
# 哪些失败该算进去
# --------------
# **不是所有错误都说明依赖不行。** 判据是这次失败**有没有白花时间**：
#   - 超时、连不上、鉴权配额错 → 算。要么白等，要么马上重试也不会成功。
#   - LLM 返回的不是合法 JSON → **不算，而且要记成成功**（不是笔误）。依赖是活的、
#     答得也快，只是答得不对，多半是 prompt 的问题。算成失败的话，一个 prompt bug
#     会把熔断器跳开，而半开探测同样会拿回非法 JSON，于是**它永远合不回来**。
#   - `CancelledError` → 两边都不记。取消是我们自己的决定，不是依赖的问题（同 DEVLOG 029）。
# 一句话：**熔断该拦的是"白等"，不是"答得不合我意"。**
import logging
import time
from collections import deque
from typing import Deque, Dict, Tuple

from app.core import metrics

logger = logging.getLogger("aivalon.breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

_STATE_CODE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}


class Breaker:
    """一个依赖一个熔断器。

    `implies_level`：跳闸时**顺带认为降级矩阵该到哪一级**（见 core/degrade.py）。
    注意它只是"这个熔断器认为现在该到几档"，**绝不去写人手拧的那个档位**，
    原因见 `degrade.effective_level`。
    """

    def __init__(self, name: str, *, window: float, min_samples: int,
                 failure_ratio: float, open_for: float, implies_level: int = 0):
        self.name = name
        self.window = window
        self.min_samples = min_samples
        self.failure_ratio = failure_ratio
        self.open_for = open_for
        self.implies_level = implies_level

        self._events: Deque[Tuple[float, bool]] = deque()
        self._opened_at = 0.0
        self._state = CLOSED
        # 半开时**只放一个探针进去**，这两个字段就是那把独占锁
        self._probing = False
        self._probe_deadline = 0.0
        self._observe()

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------

    def _advance(self) -> str:
        """冷却期满就把 open 推进到半开。**读状态的所有入口都要先走这一步。**

        为什么不只在 `allow()` 里推进：跳闸会顺带把降级档位顶上去
        （`implies_level`），而那个档位可能让调用方**压根走不到 `allow()`**
        ——比如 AI 路径读到"已降到 L2"就直接回落规则引擎、一次都不调 LLM。
        于是冷却期过了也没人来推进状态，**熔断器把自己锁死在 open 上**：
        它推断出的降级，反过来挡住了它自己唯一的恢复途径。
        把推进放在"任何人读状态"这一步，`implied_level()` 那次读就顺手解了这个死锁。
        """
        if self._state == OPEN and time.monotonic() - self._opened_at >= self.open_for:
            self._state = HALF_OPEN
            self._probing = False
            logger.warning("熔断器 %s 冷却结束，进入半开", self.name)
            self._observe()
        return self._state

    def allow(self) -> bool:
        """能不能真去调这个依赖。`False` = 熔断中，调用方直接走兜底。"""
        state = self._advance()
        now = time.monotonic()

        if state == OPEN:
            metrics.breaker_rejects.labels(name=self.name).inc()
            return False

        if state == HALF_OPEN:
            # **半开只放一个请求过去。** 全放过去的话，依赖刚有点起色就被积压的
            # 流量一次打回原形——**恢复探测本身把依赖又打挂了**。
            # 其余请求走 `False` 这条路立刻回落，不等待，所以它们不吃亏。
            if self._probing and now < self._probe_deadline:
                metrics.breaker_rejects.labels(name=self.name).inc()
                return False
            if self._probing:
                # 探针过了自己的期限还没回报（调用方漏了 `record`、进程被打断、
                # 任务被取消）。**必须允许下一个探针**，否则这个熔断器就永久停在
                # 半开、再也不探测了。同 ai_queue 那个租约——
                # **凡是"登记出去等对方回报"的东西，都必须假设对方不会来**（同 C06）。
                logger.warning("熔断器 %s 的探针没有回报，放行下一个", self.name)
            self._probing = True
            self._probe_deadline = now + self.open_for
            return True

        return True

    def record(self, ok: bool) -> None:
        """记一次调用结果。**只把"白等"记成失败**，见文件头。"""
        now = time.monotonic()

        if self._state == HALF_OPEN and self._probing:
            self._probing = False
            if ok:
                # 探针成功：**必须把窗口清空**。留着跳闸前那批失败记录的话，
                # 下一次失败会立刻把比例重新算到阈值上，等于刚合上就又跳开。
                self._events.clear()
                self._state = CLOSED
                logger.warning("熔断器 %s 探测成功，已闭合", self.name)
            else:
                # **探针失败立刻重新跳开**，不等窗口攒够——半开状态下总共只有
                # 这一个样本，等"够不够 min_samples"就是永远不够。
                self._trip(now, probe=True)
            self._observe()
            return

        self._events.append((now, ok))
        self._trim(now)

        if self._state == CLOSED and self._should_trip():
            self._trip(now)
            self._observe()

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _should_trip(self) -> bool:
        total = len(self._events)
        if total < self.min_samples:
            return False
        failures = sum(1 for _, ok in self._events if not ok)
        return failures / total >= self.failure_ratio

    def _trip(self, now: float, probe: bool = False) -> None:
        self._state = OPEN
        self._opened_at = now
        metrics.breaker_trips.labels(name=self.name).inc()
        logger.error("熔断器 %s 跳闸（%s），%.0fs 内不再调用该依赖",
                     self.name, "探测失败" if probe else "窗口内失败率超阈值",
                     self.open_for)

    # ------------------------------------------------------------------
    # 观测与内省
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._advance()

    def _observe(self) -> None:
        metrics.breaker_state.labels(name=self.name).set(_STATE_CODE[self._state])

    def snapshot(self) -> Dict[str, object]:
        state = self._advance()
        now = time.monotonic()
        self._trim(now)
        return {
            "name": self.name,
            "state": state,
            "samples": len(self._events),
            "failures": sum(1 for _, ok in self._events if not ok),
            "min_samples": self.min_samples,
            "failure_ratio": self.failure_ratio,
            "implies_level": self.implies_level,
            "cooldown_left": (round(max(0.0, self.open_for - (now - self._opened_at)), 1)
                              if state == OPEN else 0.0),
        }

    def reset(self) -> None:
        """强制闭合。测试用，也可用于"人已经确认依赖恢复了，别再等冷却"。"""
        self._events.clear()
        self._state = CLOSED
        self._probing = False
        self._observe()


# ---- 登记表 ----
# 熔断器**挂在依赖边界上，不挂在某个调用方里**：挂在调用方的话，
# 下一个调用这个依赖的人不会知道要先问一句，这道防线就只护住了一条路径。
_registry: Dict[str, Breaker] = {}


def register(breaker: Breaker) -> Breaker:
    _registry[breaker.name] = breaker
    return breaker


def get(name: str) -> Breaker:
    return _registry[name]


def snapshot() -> Dict[str, Dict[str, object]]:
    return {name: b.snapshot() for name, b in _registry.items()}


def reset_all() -> None:
    for b in _registry.values():
        b.reset()


def implied_level() -> int:
    """已跳闸的熔断器里，最高的那个 `implies_level`。

    **只算 `open`**：半开意味着正在试探恢复，这时候必须把推断出来的降级
    暂时撤掉，否则调用方读到"还在降级"就不会去调依赖，探针也就永远发不出去
    （见 `_advance` 那段）。闭合后自然回 0——**自动触发的降级必须能自动恢复**
    （同 H-3c·下），而人手拧的那个档位反过来，必须留到人来撤销。
    """
    return max((b.implies_level for b in _registry.values() if b.state == OPEN),
               default=0)
