# Todo v2｜Aivalon 高并发架构升级

> 对应文档：`AGENTS.md`（工作守则）、`document/DEVLOG.md`（排障与概念）、`interview_qa.md`（面试答题）
> **唯一范围基准**：简历 Aivalon 七条描述。**简历没写的不做**；简历写了但没做的就是待偿债务。
> **执行方针**：只求做出来，效率第一。不做接口抽象类重构、不做简历不提的周边功能、不做额外压测报告。
> **数字纪律**：简历已有实测出处的数字不再复测；未做的功能做完按实测改简历（第一铁律），测不出来就改简历。

---

## 🔄 当前位置（2026-08-11 晚）

**A / B / C / D 组已全部完成。剩下 E → F → G → H 四组,全部是"简历已写成事实但代码没有"的功能。**

已完成：可观测体系、压测平台 + v1 基线、测试安全网（54 项全绿）、房间 Actor 单写者、Write-Behind、一致性哈希路由 + 心跳租约 + 故障转移、动作 P99 口径复测。

**排障记录索引**（细节在 `document/DEVLOG.md`）：
- 006 WS 鉴权 yield 依赖独占连接 → 100 连接 93% 失败改到 1000 连接零失败
- 011 → 012 同一个病修两次；012 的"持有并等待"自死锁是 011 的修复引入的
- 013 kill -9 恢复演练 + RPO 量化。**边界：`restore_game_state` 从 DB 重建的分支仍是 TODO,恢复依赖 Redis 快照存在**
- 014 `INSERT IGNORE` 静默吞外键失败 → 全库 372 局 `GAME_START` 事件 0 条
- 015 写事务数 -78~81%（口径是事务数不是行数,行数 0%）
- 016~018 三个多节点专属 bug（转发头白名单 / 只接写路径 / 非归属节点内存副本致对局回退）
- 019 动作 P99 两轮差三倍是分位数口径错,不是性能问题
- C05 一致性哈希与哈希盐陷阱 / C06 心跳租约判死 / C07 分位数的样本量前提

**遗留（已知不修）**：`auth.py` 的 `register` 是 `async def` 里做同步 `db.query`,不在热路径；`room_actor.py` Actor 空闲退出竞态、`actor.submit` 无超时。

**环境启动**：
```bash
docker-compose up -d
cd backend && source venv/bin/activate
export AI_USE_LLM=false AI_TASK_RATE_LIMIT=100000/m RATE_LIMIT_ACTION_TIMES=100 RATE_LIMIT_CREATE_GAME_TIMES=10000
uvicorn app.main:app --port 8000 &          # 确认 pgrep -f "uvicorn app.main" | wc -l == 1
celery -A app.core.celery_app worker --loglevel=warning &
python -m app.core.outbox_relay &
./run_tests.sh                              # 应 54 项全绿
```
多节点：`NODE_ID=node-2 NODE_ADDR=http://127.0.0.1:8002 uvicorn app.main:app --port 8002`,演练 `bench/drill_room_routing.py verify|takeover`。

**关键文件**：`room_actor.py`（Actor）/ `event_journal.py` + `event_flusher.py`（Write-Behind）/ `hash_ring.py` + `node_registry.py` + `room_router.py`（路由）/ `socket_manager.py`（广播,E 组入口）/ `bench/results/`（数字出处）

---

## 📋 简历欠账表（只列没结清的）

| 段 | 简历原话 | 欠什么 |
|---|---|---|
| ③ | 创建对局耗时…降至 **226ms** | ⚠️ 226ms 是 max,无统计稳定性（复测两轮 187ms / 411ms）。**建议改成"降至亚秒级"**——不需要重测,只需改措辞（DEVLOG 019 / C07） |
| ⑤ | 独立 WS 网关分离连接与逻辑,Redis 路由表跨节点扇出；同 Tick 事件合并帧、分级推送、慢消费者背压 | E 组全部未做。⚠️ 2,000 连接与 180ms 是**这些优化之前**测出来的,别把因果挂上去 |
| ⑥ | L1+L2 多级缓存、事件驱动失效、布隆/singleflight/逻辑过期、热榜分片 ZSET + 定时归并 + 本地缓存、MQ 批量 ZINCRBY | F / G 组全部未做 |
| ⑦ | 分层限流 + 5 级降级矩阵 + 依赖熔断,**10 倍突发流量下核心对局链路零中断** | H 组全部未做。⚠️ **"零中断"与已归档基线直接矛盾**：`v1_baseline.md` S4 记录 10 倍突发失败率 10.09%、P90 恶化到 7.8s。基线报告在 GitHub 上,面试官翻得到——这是唯一必须实测的一个数,做完 H 组跑一次 S4,拿不到就改简历 |

已结清的数字（出处在 DEVLOG / `bench/results/`,不再复测）：2,000 长连接、广播 P99 180ms、动作 P99 83.8ms、4~5 事件合并一次提交、写事务数 -80%、RPO 200ms、一致性哈希 + 160 虚拟节点、迁移量 3/4 → 1/N、节点宕机 5.6s 接管、状态无回退、100 → 2,000 连接。

---

## A / B / C / D 组 ✅ 全部完成

- [x] A 可观测：Prometheus + Grafana + 业务指标 + trace_id 透传 + 连接池探针 + `/cluster` 集群视图
- [x] B 压测：Locust 工具链 + S1~S5 五类场景 + v1 基线报告归档 + 写压力量化脚本
- [x] C 安全网：状态机单测 + 主链路集成测试 + 哈希环/路由单测,`run_tests.sh` 54 项
- [x] D 热路径：Actor 单写者去锁 + 空闲卸载；一致性哈希路由（160 虚拟节点）+ 心跳租约 + 读写双路由转发 + 一跳防环；节点宕机房间漂移 + 快照恢复；Write-Behind（Redis Stream + 200ms 批量刷库）+ RPO 量化 + kill 演练 + 写事务量化；动作 P99 口径修正

---

## E. 长连接与广播（简历⑤）

- [ ] WS 网关拆成独立服务：只做连接维持、握手鉴权、消息转发,不含业务逻辑
- [x] 连接 → 网关节点映射存 Redis（简历说的"Redis 路由表"）：房间 → 持有其连接的节点集合,节点粒度登记,TTL 兜底崩溃残留
- [x] 跨节点扇出：查路由表定向 publish 到目标节点专属频道（不广播全集群）,本节点直发不绕 Redis,收到转发只做本地投递防环。11 项测试（DEVLOG 020）
- [x] 同 Tick 事件合并帧：50ms 合并窗口内同房间多个 STATE_UPDATE 只下发最后一帧,合并率看 `ws_frames_merged / ws_frames_sent`（DEVLOG 021）
- [x] 分级推送：`unicast` 操作者单播（跨节点按 user_id 筛）/ 玩家 50ms 即时 / 旁观者 500ms 聚合成一个 BATCH 帧；角色按"是否占座"服务端判定,不信客户端声明（DEVLOG 023）
- [x] 慢消费者背压：每连接独立写缓冲 + 写协程,广播不再 await socket（消除队头阻塞）；缓冲让出一次事件循环后仍满则以 1013 断开,指标 `ws_slow_consumers_dropped`（DEVLOG 022）

## F. 缓存（简历⑥·上）

- [ ] L1 进程内 + L2 Redis 两级缓存,未命中回源 MySQL
- [ ] 对局快照事件驱动失效（状态推进即失效）+ TTL 兜底
- [ ] 穿透：布隆过滤器拦截不存在的 game_id / user_id
- [ ] 击穿：热点快照 singleflight 互斥回源（验收就是"热 key 失效瞬间只回源 1 次",单测即可断言,不用压测）
- [ ] 雪崩：逻辑过期异步重建 + TTL 随机抖动

## G. 热榜（简历⑥·下）

- [ ] 写路径：对局结束 → MQ → 批量合并 ZINCRBY（合并同玩家窗口内多次变更）
- [ ] 读路径：分片 ZSET + 定时归并 Top N + L1 本地缓存

## H. 韧性（简历⑦）

- [ ] **AI 降级开关运行时可切**（L1/L2 的前置）：现在 `AI_USE_LLM` 是启动时读配置,改成 Redis 开关热切换
- [ ] LLM 舱壁：调用加超时,超时/异常自动回落规则引擎（现在有 `except` 回落但没超时,LLM 卡住会一直挂）
- [ ] 分层限流：网关层全局令牌桶 + IP 维度；应用层用户级滑动窗口补齐遗漏场景；资源层单房间事件风暴保护 + AI 队列深度上限
- [ ] 5 级降级矩阵 + 一键触发开关中心：L1 关 AI 发言 / L2 AI 全切规则引擎 / L3 回放热榜降频 / L4 创建对局排队 / L5 拒新开局只服务进行中房间
- [ ] 依赖熔断：熔断器包住 LLM / 统计 / 邮件,熔断自动挂对应降级级别
- [ ] **S4 复测：10 倍突发下核心对局链路零中断**（唯一必测项,对照组是基线 S4 的 10.09% 失败率）

---

## 已砍清单（简历不提,一律不做）

Go 接入层 · Design Only 文档（分库分表/单元化/多活）· 结构化 JSON 日志与脱敏 · 并发用例（Actor 串行化已覆盖）· 缓存预热与就绪探针 · 热榜赛季机制 · ADR 4 篇（经验已在 DEVLOG,不再单独成文）· README 架构演进叙事 · 额外压测报告归档 · RuleEngine/LLMEngine 接口抽象（两条路径已能跑,抽类只是重构）· S2 动作 TPS 5,000 目标 · S3 冲 20,000 连接 · S3/S5 拆分后复测 · 网关节点宕机连接迁移演练 · 各类 ADR 与演练留档

## 顺序

```
E（网关 + 广播）→ F（缓存）→ G（热榜）→ H（韧性,含 AI 开关前置）→ 跑一次 S4 结清简历⑦
```
