# DEVLOG · Aivalon 开发经验与踩坑记录

> 规则（见 AGENTS.md §6）：凡开发过程中遇到的非平凡问题——报错、环境坑、压测结果与预期不符、选型纠结——都记录在此。
> 每条按「现象 → 排查 → 根因 → 解决 → 经验」五段式，注明日期。这是技术复盘长文和面试问答的一手素材库。

## 概念速查（面试向 · 随时补充）

> 非踩坑类：开发中遇到"不懂但必须懂"的概念，用问答式笔记沉淀在此，每条标注关联代码/配置位置。

### C01 Prometheus 是什么（2026-08-10 · A 组）

- **一句话**：专门存"监控数字"的时序数据库 + 采集器，大厂监控体系事实标准
- **核心模式是 pull**：它不是等你上报，而是每隔 15s 主动访问 `/metrics` 把当前所有数字抓走存起来（对比：MySQL 是你往里写）
- **数据结构叫时间序列**：指标名 + 一组 label = 一条随时间变化的曲线。例：`http_requests_total{handler="/health", method="GET", status="2xx"}` 就是"该接口 GET 2xx 的累计请求数"这条曲线。有了曲线就能算增速 = QPS
- **分工**：Prometheus 存数据和告警，Grafana 负责画图（Grafana 只读不存）
- **关联代码**：`backend/app/main.py` 的 `Instrumentator().expose(app)` 就是被拉的 `/metrics`

### C02 label 基数爆炸（2026-08-10 · A 组）

- **label**：指标上的维度标签，如 `{handler="/health", method="GET"}`
- **基数 = 取值个数**；**每种 label 取值组合 = 一条独立时间序列**，都要占内存
- **爆炸机制**：label 放低基数维度（路由十几个 × 方法几种 ≈ 几十条序列）没问题；放 `game_id`/`user_id` 这种高基数值，一万个房间 = 每个指标爆炸成上万条序列，Prometheus 内存打爆、查询卡死。这是 Prometheus 最经典的生产事故
- **正确分工（面试标准答案）**：metrics 看聚合趋势，日志查个案（带 trace_id/game_id），trace 看调用链——"metrics 看趋势、日志查个案、trace 看链路"
- **关联代码**：`backend/app/core/metrics.py` 头部注释；label 只用了 event_type / action_type 这类低基数维度

### C03 Histogram、桶与"P99 是估算"（2026-08-10 · A 组）

- **问题**：想算 P99 延迟，但 Prometheus 每 15s 才拉一次，拿不到每个请求的原始值，无法排序
- **Histogram 的解法**：不存原始值，只存"落在每个区间的累计个数"。区间边界就是桶（buckets）。一个延迟 37ms 的观测，会给 ≤50ms、≤100ms、≤1s……所有不小于它的桶各 +1
- **P99 怎么算出来**：总 1000 个请求，第 990 名落在哪个桶？查各桶计数找到它落在 50ms~100ms 桶，然后**假设桶内均匀分布做线性插值**，估出约 98ms——所以 P99 是估算值不是精确值
- **推论（面试追问点）**：估算精度取决于桶边界设计。桶要按"目标延迟"加密设置——我们要验证 P99 < 100ms，就在 5ms~200ms 区间密集设桶（见 `metrics.py` 的 buckets 参数）；桶设糙了 P99 就没意义
- **关联代码**：`backend/app/core/metrics.py` 的 `ws_broadcast_latency` / `game_action_latency`

## 2026-08-10 · A 组观测体系

### 002 prometheus-fastapi-instrumentator 8.x 与 fastapi 的 starlette 版本冲突

- **现象**：接入指标库后 uvicorn 启动即崩，`TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`，报错栈在 `app/routers/auth.py` 的 `APIRouter()`——看上去像是自己代码的问题
- **排查**：`pip check` 一把出真相：`fastapi 0.128.0 has requirement starlette<0.51.0,>=0.40.0, but you have starlette 1.6.0`；查 instrumentator 的依赖约束发现 8.x 强制 `starlette>=1.0.0,<2.0.0`——pip 装它时把 starlette 从 0.40.x 升到了 1.6.0，fastapi 被拖垮
- **根因**：两个依赖对 starlette 大版本的要求互斥（fastapi 0.128 锁 <0.51，instrumentator 8.x 锁 >=1.0），pip 按后装的包解析，破坏了先装的
- **解决**：instrumentator 降到 `7.1.0`（兼容 starlette 0.x），starlette 回落 0.50.0，`pip check` 转绿；requirements.txt 加 `<8.0.0` 上限并注释原因
- **经验**：① **装新依赖后先 `pip check` 再启动**，比看报错栈快得多——报错栈指到的往往是受害者不是肇事者；② 依赖冲突在排障中的优先级要前置：功能没动过却起不来，先怀疑环境/版本，再怀疑代码；③ requirements 里发现过的冲突要留上限约束 + 注释，否则下次有人 `pip install -U` 又踩一遍



### 001 范围决策：以简历证据链为唯一范围基准（2026-08-09 · v2 开工前）

- **背景**：v2 PRD 覆盖了从单机优化到分库分表/单元化/多活的完整演进叙事，但项目根本目的是面试，简历只承载七条描述
- **决策**：以简历七条为范围基准重裁剪 todo（见 `document/todo_v2.md` 各组标注）：砍掉 Go 网关（设计文档与 demo）、分库分表/单元化/多活设计文档；结构化日志、并发测试、ADR 数量降级为最小必需集
- **经验**：① 简历项目的范围应围绕"还差什么证据"来砍——简历没写的做了是自我感动，简历写了的没证据是定时炸弹；② 砍范围时做全文档一致性扫描（PRD/todo/AGENTS 三处同步），留一处矛盾就是面试被追问的坑；③ "Design Only"类内容不删除而是标注归档，未来真有需要时不用重新调研
