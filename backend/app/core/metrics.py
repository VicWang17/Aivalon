# 这个文件是 Prometheus 业务指标的定义中心，集中声明所有自定义指标，供各模块 import 使用。
# 注意：label 只允许低基数维度（路由、事件类型、队列名），严禁放 game_id / user_id（高基数会打爆 Prometheus）。
from prometheus_client import Counter, Gauge, Histogram

# ---- WebSocket 层 ----
# 当前 WS 连接数（网关注册/注销时增减）
ws_connections = Gauge(
    "aivalon_ws_connections",
    "Current WebSocket connections",
)

# 广播延迟：从事件产生到写入 WS 连接的耗时（秒）
ws_broadcast_latency = Histogram(
    "aivalon_ws_broadcast_latency_seconds",
    "Latency from event created to pushed to WS clients",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5),
)

# 实际下发的帧数 / 被合并掉的帧数。
# 两者相比即合并率，是"同 Tick 合并帧"这个优化唯一的验收口径。
ws_frames_sent = Counter(
    "aivalon_ws_frames_sent_total",
    "Frames actually written to WS connections",
)
ws_frames_merged = Counter(
    "aivalon_ws_frames_merged_total",
    "State-update frames coalesced into a later frame (never sent)",
)

# 因写缓冲积压被主动断开的连接数。
# 这个值持续上涨说明下游跟不上推送速率，是背压生效的信号，不是 bug。
ws_slow_consumers_dropped = Counter(
    "aivalon_ws_slow_consumers_dropped_total",
    "Connections dropped because their send buffer overflowed",
)

# ---- 对局层 ----
# 当前活跃房间数
active_rooms = Gauge(
    "aivalon_active_rooms",
    "Current active game rooms",
)

# 事件写入速率（按事件类型分组）
game_events_total = Counter(
    "aivalon_game_events_total",
    "Game events appended",
    ["event_type"],
)

# action 处理耗时：从收到动作请求到状态推进完成（秒）
game_action_latency = Histogram(
    "aivalon_game_action_latency_seconds",
    "Action processing latency (submit -> state advanced)",
    ["action_type"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5),
)

# ---- 缓存 ----
# 各级缓存的命中/未命中。level 取 l1 / l2 / db / singleflight，都是低基数维度。
# 这是多级缓存唯一的验收口径：L1 命中率说明进程内缓存值不值得，
# db 那一档的增速就是真实回源 QPS。
# singleflight 那一档是"被合并掉的回源"——它和 db 档的比值就是防击穿的收益，
# 热 key 失效瞬间来了 N 个并发，应该看到 db +1 而 singleflight +(N-1)。
cache_reads = Counter(
    "aivalon_cache_reads_total",
    "Cache reads by level and result",
    ["level", "result"],
)

# 被布隆过滤器拦掉的请求数（= 省下的无效回源次数）。
# 这是防穿透唯一的验收口径：这个数在涨，说明确实有不存在的 id 在打进来且被挡住了。
bloom_rejects = Counter(
    "aivalon_bloom_rejects_total",
    "Requests rejected by bloom filter (nonexistent ids, never reached DB)",
)

# ---- 热榜 ----
# 入缓冲的变更条数 / 真正打到 ZSET 上的 ZINCRBY 条数。
# 两者相比即合并率，是"批量合并写"唯一的验收口径：buffered 远大于 applied
# 才说明合并真的在发生。注意这个比值在本项目里不会很夸张——
# 一个人没法同时结束两局，所以同 member 合并有限，主要收益在**批量化**
# （一个窗口的变更攒成一次 pipeline，N 次往返变 1 次）。
rank_updates_buffered = Counter(
    "aivalon_rank_updates_buffered_total",
    "Leaderboard score changes written into the merge buffer",
)

rank_updates_applied = Counter(
    "aivalon_rank_updates_applied_total",
    "ZINCRBY commands actually issued after merging",
)

# 榜单读的来源：snapshot（读到归并快照）/ merged（快照没有，当场归并兜底）。
# 这是"定时归并"的验收口径：merged 那一档应该几乎不涨——它只在冷启动和
# 快照过期时出现。如果它跟着读 QPS 一起涨，说明归并循环没在跑，
# 每次读都在当场归并，等于这层优化根本没生效。
rank_reads = Counter(
    "aivalon_rank_reads_total",
    "Leaderboard reads by source",
    ["result"],
)

# ---- 准入控制 ----
# 被网关层准入拒掉的请求数。layer 分 global / ip，两档刻意分开，
# 因为看到它们之后要做的事完全不同：global 是**系统到顶了**（该扩容或降级），
# ip 是**某个来源在打我**（该封禁或该排查）。合成一个 429 计数就分不出来了。
# 这也是准入层唯一的验收口径：正常流量下它应该一直是 0，一涨就说明到了容量边界。
admission_rejects = Counter(
    "aivalon_admission_rejects_total",
    "Requests rejected by gateway admission control",
    ["layer"],
)

# 被应用层滑动窗口拒掉的请求数。scope 是接口标识（低基数，接口数量固定），
# 绝不能放 user_id（C02 基数爆炸）。和上面 admission 分开记：admission 说明
# **系统**到顶了，这个说明**某个用户**在某个接口上超了业务配额——
# 前者要扩容或降级，后者是正常的规则生效，多数时候不需要任何动作。
rate_limit_rejects = Counter(
    "aivalon_rate_limit_rejects_total",
    "Requests rejected by per-user sliding window",
    ["scope"],
)

# 被资源层拦掉的房间动作数。reason 分 queue_full / timeout，两档的含义不同：
# queue_full 是**这个房间被打爆了**（动作压根没入队，一定没生效，可以安全重试）；
# timeout 是**处理跟不上**（动作可能已经在跑，服务端不能断言它没生效）。
# 和前两层再分一次：admission = 系统到顶，rate_limit = 某用户超配额，
# 这个 = **某个房间这一个资源被打爆**，而前两层都统计不到它——
# 八个人对着一局猛点，每个人都没超自己的配额。
# 刻意不放 game_id 进 label（房间数无上限，C02 基数爆炸）；要定位到具体房间看日志。
room_overload = Counter(
    "aivalon_room_overload_total",
    "Room actions rejected by per-room resource limits",
    ["reason"],
)

# ---- 降级开关 ----
# 每个开关当前是否处于降级态（1 = 已降级）。name 是开关名，低基数。
# 降级动作本身必须可观测：不上报的话，复盘时分不清"AI 没说话"是因为降级了
# 还是因为坏了——那就是一次没人知道的静默变更。
degrade_switch = Gauge(
    "aivalon_degrade_switch",
    "Degradation switch state (1 = degraded)",
    ["name"],
)

# ---- AI 链路 ----
# AI 队列积压深度（周期性从 broker 读取）
ai_queue_depth = Gauge(
    "aivalon_ai_queue_depth",
    "Pending tasks in AI queue",
)

# LLM 调用结果。四档刻意分开，因为它们要采取的行动不同：
#   success —— 正常
#   timeout —— 依赖**慢了**：该考虑降级或扩容
#   invalid —— 依赖**坏了**（通了但返回的不是合法 JSON）：该去改 prompt
#   error   —— 其他失败（鉴权、配额、网络）
# 混成一个 "fallback" 档的话，事故里分不清该扩容还是该改 prompt。
llm_calls_total = Counter(
    "aivalon_llm_calls_total",
    "LLM invocations by result",
    ["result"],
)

# LLM 调用耗时（秒）。**失败和超时的耗时也要记进来**——只记成功的话，
# LLM 全在超时的时候这条曲线反而会变好看（慢的都没被统计），
# 于是最需要告警的时刻指标一片绿。
llm_latency = Histogram(
    "aivalon_llm_latency_seconds",
    "LLM call latency",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
