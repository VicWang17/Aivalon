# Todo v2｜Aivalon 高并发架构升级（简历证据链版）

> 对应文档：`document/PRD_v2_high_concurrency.md`（全景参考）、`AGENTS.md`（工作守则）
> **范围基准**：只做简历七条描述所需的事。已裁剪：Go 网关（E.3）、Design Only 文档、结构化日志全量版、并发测试用例、ADR 8→4 篇。裁剪理由见 `document/DEVLOG.md` 001。
> **数字口径**：本文件中所有性能数字为**目标参考值**，最终以 v1 基线实测与优化后实测为准回填，并同步修改简历——测不出来就改简历，不许写目标值充数。
> 纪律：A（可观测+基线）未完成前，不动任何优化；每个优化项必须有前后对比数字。

---

## 🔄 交接状态（2026-08-11 晚，换机/换 AI 续做必读）

**当前位置**：**D-2 Write-Behind 已全部闭环**。30s 超时墙闭环（DEVLOG 012）、kill 演练 + RPO 量化（013）、静默丢事件 bug 修复（014）、写压力量化 78~81%（015）。**下一步进 D-3（房间路由一致性哈希 + 节点宕机迁移）**。

**已完成**：A 组（可观测）、B 组（压测平台 + v1 基线报告）、C 组（测试安全网 24 项）、D-1（房间 Actor 去锁）、D-2 代码（Write-Behind：事件先入 Redis Stream + 快照一个事务，flusher 每 200ms 批量刷 MySQL）。

**D-2 验证状态（未闭合，接手先做）**：
- ✅ 24 项测试全绿（含全对局集成测试，走的就是新 Write-Behind 链路）
- ✅ 动作处理主体实测 ~2ms（深拷贝 0.2 + journal 1.5）；空载动作 7~13ms（基线 18~25ms）
- ✅ **kill 恢复演练已过（DEVLOG 013）**：`kill -9` API（flusher 同进程一起死）→ 重启 → 对局从 Redis 快照恢复（`phase=vote round=2` → 读回 `phase=mission round=2`）→ 续打到 `finished`；MySQL 101 事件 / distinct seq 101 / **重复行 0**。演练脚本固化为 `bench/drill_kill_recovery.py`（`play|measure|resume|watch` 四段，D-3 节点迁移可复用）
- ✅ **RPO 已量化（DEVLOG 013）**：20 房间负载下 427 次采样 @0.1s——峰值未刷 **16 条**（最坏 RPO）/ P95 10 / 均值 3.45 / 非零占比 84.5%；时间维度 ≤ ~200ms（`FLUSH_INTERVAL`）。注意必须在负载下测，空跑窗口恒为 0
  - 边界（不含糊过去）：`restore_game_state` 的"从 DB 重建 GameState"分支仍是 `# TODO` 返回 `None`，**恢复能力当前依赖 Redis 快照存在**；Redis 全丢时对局无法重建
- ✅ **顺带修掉一个静默丢事件 bug（DEVLOG 014）**：全库 372 局 `GAME_START` 事件 **0 条**——`game_events.game_id` 有外键指向 `games.id`，而 flusher 先插事件行、后补建 `games` 记录，外键失败被 `INSERT IGNORE` 降级成 warning 静默丢弃。修法：`games` 记录补建提到事件插入之前。这个 bug 是 013 演练的对账环节（`count(*)` vs `max(seq)` 差 1）挖出来的
- ✅ **MySQL 写压力已量化（DEVLOG 015）**：**写事务数下降 78~81%**（每事件 commit 1.07 → 0.20~0.24，即 4~5 个事件合并进一个事务）
  - 口径：`SHOW GLOBAL STATUS` 计数器增量 ÷ 事件数（归一化，避免"变快"混淆"变省"）；对照组用 `git worktree` 检出 a8f21ea（Actor 已有、Write-Behind 之前，只差一个变量）跑完整 v1 栈，同机同中间件同压测脚本，各两轮
  - **⚠️ 简历口径必须写"写事务数下降 ~80%"，不能写"写压力下降 80%"**——行数维度（`Innodb_rows_inserted`）下降为 **0%**（事件该落库还是要落库，只是晚 200ms），笼统说"写压力"会被理解成写入量，追问即崩
  - 测量脚本 `bench/measure_write_pressure.py`（`start/stop/diff` 三段），E/F/G 组同类量化可复用
  - 副产物：创建对局 P50 40→23ms、P75 310→97ms、max 380→180ms；v1 有 4 次 mission 500（DEVLOG 005 竞态家族），v2 零失败
- ✅ **30s 超时墙已闭环（2026-08-11 16:00，DEVLOG 012）**。真凶不是原假设的 Actor future 悬挂泄漏，而是**"持有并等待"自死锁**：`get_current_user` 用 `Depends(get_db)`（连接持有到请求结束）+ 下游 `_load_user_map` 再取第二个连接 → 单请求持有 2 个连接 → 15 个鉴权连接占满池后集体等第二个连接，池永不恢复。**这个 bug 由 011 的修复引入**（把复用 db 的查询抽成独立 Session，单请求持有数 1→2）
  - 定位手段：新增连接池探针 `app/core/pool_probe.py`（checkout 时记 trace_id + 调用栈，报告持有超阈值未还的连接），`DB_POOL_PROBE=true` 开启，默认关闭
  - 修法：`get_current_user` 改手动短生命周期 Session（与 `get_ws_user` 同一招，DEVLOG 006 当时只改了 WS 路径）
  - 验证：两轮干净复测 `s2_u20_fix_r1/r2`，共 9536 请求**零失败**（修复前 25.76% 创建失败）；创建 max 30281ms → 226ms/160ms；吞吐 29.74 → 40.21 rps；探针零告警
  - 遗留（不在本步范围）：`auth.py` 的 `register` 是 `async def` 里做同步 `db.query`（011 修过的同类事件循环阻塞），但不在 S2 热路径上，未改
- ⚠️ **以下为 011 的历史记录（三处缺陷已修，但当时未根除）**：
  1. ✅ `restore_game_state` 连接泄漏（`finally` 里 `if not db` 在重新赋值后恒假，每次房间唤醒漏 1 连接）——已修（`own_session` 标志）
  2. ✅ 事件循环冻结（`get_current_user`/`create_game` 是 async def 却做同步 db.query，等池连接时冻结全服务）——已修（auth 改同步 def 走线程池；创建路径用户名查询改 `asyncio.to_thread` + 短 Session）
  3. ✅ flusher 与前台共享连接池——已隔离（独立引擎 + 单连接池）
  4. ❌ **池仍会被占满**：修复后 46 次创建 13 次 500，P66+ 卡整齐的 30000ms（池等待 30s 超时），报错点为 `get_current_user` 等连接。**关键证据**：MySQL processlist 显示所有连接 `Sleep` 空闲——连接是被**签出后闲置持有**的（僵尸请求），不是 MySQL 慢
  - **当前假设**：`get_db` Session 从鉴权查询起签出、持有到请求结束；若请求卡在永不返回的 await（如 Actor `submit` 的 future 在 Actor 任务异常退出后永久悬挂），连接永久泄漏，攒够 15 个池即死
  - **下一步**：给 SQLAlchemy 池开 checkout/checkin 日志或加 pool 状态探针，复现 20 房间场景，抓"谁签出未还"；顺带检查 `room_actor.py` 的 future 在 Actor 任务死亡路径上是否一定会被 set_exception
- ⚠️ **机器口径变更**：基线报告在 Intel i7-9750H 上测得，当前为 Apple Silicon——绝对延迟跨机不可比，I 组回填简历数字时需在新机重跑全量回归或注明口径
- 另修：`PlayerState` 补 `is_connected` 字段（结算时 stats 任务 payload 访问它抛 AttributeError，导致每局收尾动作 500、统计任务从未触发）；alembic `bd13848a4a56` 迁移加判存在（全新库可重复）；fastapi 锁 `>=0.128,<0.129`；补 `pytest-asyncio` 显式依赖
- 环境注意：celery worker / uvicorn 日志量大，后台运行必须重定向到 `logs/`（任务系统 16MiB 输出上限会杀进程）；`logs/` 已 gitignore

**接手第一步（D-2 收尾）**：先定位并修复上面的连接池占满 bug → 干净环境（单 uvicorn 实例！见 DEVLOG 008）跑 `locust -f ../bench/locust_s2.py --headless -u 20 -r 20 -t 120s` 两轮确认稳定 → kill 演练 → 量化写压力 → 提交 → 走 D-3。

**环境启动**（bench 配置）：
```bash
docker-compose up -d
cd backend && source venv/bin/activate
export AI_USE_LLM=false AI_TASK_RATE_LIMIT=100000/m RATE_LIMIT_ACTION_TIMES=100 RATE_LIMIT_CREATE_GAME_TIMES=10000
uvicorn app.main:app --port 8000 &          # 确认 pgrep -f "uvicorn app.main" | wc -l == 1
celery -A app.core.celery_app worker --loglevel=warning &
python -m app.core.outbox_relay &
./run_tests.sh                              # 应 24 项全绿
```

**关键文件地图**：
- `backend/app/core/room_actor.py` — 房间 Actor（D-1）
- `backend/app/core/event_journal.py` — Write-Behind 日志（事件+快照一个 Redis 事务）
- `backend/app/core/event_flusher.py` — 批量刷库器（INSERT IGNORE 幂等 + 游标推进）
- `document/benchmark/v1_baseline.md` — 基线报告（所有对比数字的对照组）
- `document/DEVLOG.md` — 排障记录 001~008 + 概念速查 C01~C04
- `bench/README.md` — 压测场景运行方法

**之后顺序**：D-3（路由表+故障转移，最小真实版）→ D-4（AI 双引擎+降级开关）→ D-5（验证报告+ADR-01/02）→ E 组（网关广播）→ F/G（缓存热榜）→ H（韧性演练）→ I（叙事资产收尾+简历数字回填核对）。

---

## A. 可观测性（一切优化的前置）

### A.1 指标（Prometheus + Grafana）
- [x] 接入 prometheus-client，暴露 /metrics 端点
- [ ] API 指标：QPS、延迟分位数（P50/P95/P99）、按路由分组
- [ ] WS 指标：当前连接数、消息收发速率、广播延迟
- [ ] 对局指标：活跃房间数、事件写入速率、action 处理耗时
- [ ] AI 指标：队列积压深度、LLM 调用耗时/失败率/重试率
- [ ] 基础设施指标：Redis / MySQL 操作耗时、连接池水位
- [x] docker-compose 增加 Prometheus + Grafana 服务
- [x] Grafana 面板：压测总览仪表盘（QPS/延迟/房间/队列一屏看完）

### A.2 日志（最小集）
- [x] 全链路 trace_id 透传（压测排障刚需）
- ~~结构化 JSON 日志 / 敏感信息脱敏检查~~（简历未提及，砍）

## B. 压测平台（基线 + 回归复用）

### B.1 压测工具链
- [x] 选型落地（Locust 或 k6，需支持 WS 长连接）
- [x] 压测数据准备脚本（批量造用户、造对局）
- [x] 压测流量标记（Header 透传，便于隔离与清理）

### B.2 标准压测场景（做成可重复执行的脚本）
- [x] S1 纯 API 场景：登录/历史/热榜读接口梯度加压
- [x] S2 对局主链路：N 并发房间全自动对局（规则 AI），测 action TPS 与延迟
- [x] S3 长连接场景：梯度建立 WS 连接，测单节点上限与广播延迟分布
- [x] S4 突发流量：1 分钟内 10 倍流量冲击
- [x] S5 热点场景：单房间大量旁观连接 + 热榜集中读

### B.3 v1 基线报告（最关键产出）
- [x] 跑完 S1~S5，记录当前各项指标数值
- [x] 定位每个场景的第一瓶颈（CPU / IO / DB / 锁）
- [x] 归档 `document/benchmark/v1_baseline.md`
- [x] **用基线数字回写简历占位值**（吞吐 X00、连接数、各 P99），确认目标量级可达，达不到就当场改简历

## C. 对局正确性保障（重构安全网，最小集）

- [x] 对局状态机单元测试：覆盖提名/投票/执行/刺杀/结算关键规则与边界
- [x] 主链路集成测试：创建对局 → 完整对局 → 结算 → 历史/回放可查
- [x] 一键运行脚本，D 组重构期间常驻
- ~~并发用例（重复提交/乱序/断线重连）~~（由 H 组故障演练覆盖，砍）

## D. 房间热路径（简历第 3 条证据链）

### D.1 房间 Actor 状态机
- [x] Actor 框架：每房间一个执行单元，动作串行化，移除 Redis 分布式锁
- [x] 房间休眠/唤醒：无活动超时卸载，新动作触发状态重建（Actor 空闲 300s 自动退出，状态留内存）
- [ ] 房间路由：一致性哈希（虚拟节点），路由表存 Redis（D-3 做）
- [ ] 节点宕机迁移：房间漂移到健康节点，状态可恢复（D-3 做）
- [ ] ~~房间休眠/唤醒完整版~~：从 Redis/DB 重建状态的唤醒链路（并入 D-2 恢复机制）
- [ ] 房间路由：一致性哈希（虚拟节点），路由表存 Redis
- [ ] 房间休眠/唤醒：无活动超时卸载，新动作触发状态重建
- [ ] 节点宕机迁移：房间漂移到健康节点，状态可恢复

### D.2 写路径异步化
- [x] 事件双写：内存 + Redis Stream（持久化兜底）
- [x] 批量刷盘器：时间窗口 / 批量阈值触发，按 seq 顺序刷 MySQL
- [x] 宕机恢复：Redis Stream 未刷盘事件 + 定期 snapshot 重建（**边界**：DB 重建分支仍是 TODO，恢复依赖 Redis 快照存在，见 DEVLOG 013）
- [x] Outbox 角色调整：从主链路降级为持久化与分发兜底（flusher 同事务写 outbox）
- [x] RPO 量化：峰值 16 条 / P95 10 / 均值 3.45 @20 房间；时间维度 ≤200ms（DEVLOG 013）

### D.3 AI 链路分层（简历第 7 条证据链）
- [ ] AI 决策接口抽象：RuleEngine / LLMEngine 可插拔双引擎
- [ ] 规则引擎补全：作为默认路径，全流程可独立跑通对局
- [ ] LLM 舱壁隔离：独立连接池 + 超时回落规则引擎
- [ ] AI 发言挂载降级开关

### D.4 验证（数字待实测回填）
- [x] C 组测试全部通过（重构后回归，24 项全绿）
- [ ] S2 压测对比报告：动作 TPS 较基线提升 N 倍（目标参考：≥ 5,000 TPS，P99 ≤ 50ms）——**单机 Python 栈不可能到 5000 TPS，D-5 出报告时按实测改简历**
- [x] kill 演练：**进程**宕机恢复、事件不丢不重（DEVLOG 013）；~~房间跨**节点**迁移~~ 待 D-3
- [x] MySQL 写入下降比例：**写事务数 -78~81%**（DEVLOG 015）。注意口径是事务数不是行数，行数为 0%
- [ ] ADR-01（单写者 vs 分布式锁）、ADR-02（Write-Behind RPO）

## E. 长连接与广播（简历第 4 条证据链）

### E.1 WS 网关独立服务
- [ ] 网关服务拆分：连接维持、握手鉴权、消息转发，不含业务逻辑
- [ ] 连接映射：连接 → 网关节点关系存 Redis
- [ ] 跨节点扇出：逻辑层广播经 Redis Pub/Sub 或 MQ topic 分发到目标网关
- [ ] 网关水平扩容验证：1 → 3 节点，广播延迟不劣化

### E.2 广播优化
- [ ] 事件合并：同房间同 Tick 多事件合并为一帧
- [ ] 分级推送：操作者单播 / 房间即时广播 / 旁观者聚合批量
- [ ] 慢消费者背压：写缓冲超阈值主动断开，保护节点
- [ ] 单房间推送限频，防事件风暴
- [ ] 断线重连增强：增量事件按 seq 幂等去重

~~### E.3 Go 接入层~~（简历未提及，砍；Python/Go 网关选型差异改为面试口述素材）

### E.3 验证（数字待实测回填）
- [ ] S3 压测达标：单节点连接数（目标参考：≥ 20,000）、广播 P99（目标参考：≤ 200ms）
- [ ] 慢消费者隔离演练通过
- [ ] ADR-03（广播合并与分级推送）

## F. 缓存体系（简历第 5 条证据链·上）

### F.1 多级缓存
- [ ] L1 进程内缓存：对局快照、热榜 Top N、persona 配置
- [ ] L2 Redis + L3 MySQL 分层与回源逻辑
- [ ] 对局快照事件驱动失效（状态推进即失效）+ TTL 兜底

### F.2 缓存三兄弟实战
- [ ] 穿透：布隆过滤器拦截不存在的 game_id / user_id + 空值短 TTL
- [ ] 击穿：热点对局快照 singleflight 互斥回源
- [ ] 雪崩：TTL 随机抖动 + 热点数据逻辑过期（异步重建）

### F.3 缓存预热
- [ ] 启动预热：persona、规则配置、活跃房间快照、热榜 Top N
- [ ] 就绪探针：预热完成才接流量

### F.4 验证（三项演练，各留记录）
- [ ] 演练 a：刷不存在的 game_id，DB 零查询
- [ ] 演练 b：热 key 失效瞬间回源请求 = 1
- [ ] 演练 c：Redis 全量重启，系统无雪崩
- [ ] ADR-04（缓存三兄弟落点与逻辑过期取舍）

## G. 排行榜（简历第 5 条证据链·下）

- [ ] 写路径：对局结束 → MQ → 批量合并 ZINCRBY（合并同玩家窗口内多次变更）
- [ ] 读路径：分片 ZSET + 定时归并 Top N 热 key + L1 本地缓存
- [ ] 赛季机制：新赛季 key 预建预热、旧赛季归档快照、原子切换无空窗
- [ ] S5 压测达标（目标参考：读 P95 ≤ 10ms @ 10,000 QPS，实测回填）

## H. 韧性工程（简历第 6 条证据链）

### H.1 分层限流（支撑"10 倍突发零中断"）
- [ ] 网关层：全局令牌桶 + IP 维度限流
- [ ] 应用层：用户级滑动窗口（登录/创建对局/动作提交，补齐遗漏场景）
- [ ] 资源层：单房间事件风暴保护、AI 队列深度上限 + 拒绝策略

### H.2 降级矩阵（5 级，可一键触发）
- [ ] L1：关闭 AI 发言
- [ ] L2：AI 全切规则引擎
- [ ] L3：回放/历史延迟容忍，热榜降频
- [ ] L4：创建对局排队 + 异步通知
- [ ] L5：拒新开局，只服务进行中房间
- [ ] 降级开关中心：配置化、可灰度、可一键触发

### H.3 熔断与可靠性补强
- [ ] 依赖调用熔断器（LLM、统计、邮件等），熔断自动挂降级
- [ ] 修复 Outbox relay：失败事件指数退避重试（≤5 次）→ DLQ → 告警

### H.4 故障演练（6 项，各留记录——简历点名数字）
- [ ] 演练 1：kill Celery worker → 对局继续（规则兜底），恢复后无重复结算
- [ ] 演练 2：停 RabbitMQ → 主链路表现符合预期
- [ ] 演练 3：Redis 宕机 → 核心对局自动降级不雪崩
- [ ] 演练 4：MySQL 注入 2s 慢查询 → 读路径熔断/降级生效
- [ ] 演练 5：网关节点宕机 → 连接自动迁移，对局无感
- [ ] 演练 6：AI 队列灌入 10 倍任务 → 背压与降级触发
- [ ] S4 突发流量复测：降级矩阵逐级触发，核心对局链路零中断
- [ ] ADR 可选项：降级矩阵设计（或用 DEVLOG 条目替代）

## I. 叙事资产（随做随写，最小集）

- [ ] ADR 4 篇归档至 `document/adr/`（编号见 D/E/F 验证项；允许用 DEVLOG 条目替代）
- [ ] README 架构演进叙事：v1 → v2 演进图 + 各阶段瓶颈与实测数字对比
- [ ] 压测报告归档 `document/benchmark/`（基线 + 各主线对比 + 最终全量回归，一次出齐）
- [ ] **简历数字回填检查**：七条描述中每个数字都有对应的实测报告出处，逐条核对
- ~~Design Only 文档（分库分表/单元化/多活）~~（简历未提及，砍；改口头演进叙事）

## 执行顺序速查

```
A（观测）→ B（压测基线 → 回填简历占位数字）→ C（测试安全网）→ D（热路径）
                                                            ↘ E（网关广播）
                                                            ↘ F（缓存）→ G（热榜）
                                          H（韧性演练，依赖 D~G 就绪）
                                          I（叙事资产，贯穿全程；收尾做数字回填核对）
```
