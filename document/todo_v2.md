# Todo v2｜Aivalon 高并发架构升级

> 对应文档：`AGENTS.md`（工作守则）、`document/DEVLOG.md`（排障与概念）、`interview_qa.md`（面试答题）
> **唯一范围基准**：简历 Aivalon 七条描述。**简历没写的不做**；简历写了但没做的就是待偿债务。
> **执行方针**：只求做出来，效率第一。不做接口抽象类重构、不做简历不提的周边功能、不做额外压测报告。
> **数字纪律**：简历已有实测出处的数字不再复测；未做的功能做完按实测改简历（第一铁律），测不出来就改简历。

---

## 🔄 当前位置（2026-08-12 下午）

**A / B / C / D / E / F / G 组已全部完成。只剩 H（韧性）,全部是"简历已写成事实但代码没有"的功能。**

已完成：可观测体系、压测平台 + v1 基线、测试安全网（179 项全绿）、房间 Actor 单写者、Write-Behind、一致性哈希路由 + 心跳租约 + 故障转移、动作 P99 口径复测、WS 网关拆分 + 跨节点扇出 + 合并帧 + 分级推送 + 背压、L1/L2 两级缓存 + 事件驱动失效 + 布隆防穿透 + singleflight 防击穿 + 逻辑过期防雪崩、热榜批量合并 ZINCRBY 写路径 + 分片 ZSET 定时归并读路径。

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
./run_tests.sh                              # 应 179 项全绿
```
多节点：`NODE_ID=node-2 NODE_ADDR=http://127.0.0.1:8002 uvicorn app.main:app --port 8002`,演练 `bench/drill_room_routing.py verify|takeover`。

**关键文件**：`room_actor.py`（Actor）/ `event_journal.py` + `event_flusher.py`（Write-Behind）/ `hash_ring.py` + `node_registry.py` + `room_router.py`（路由）/ `socket_manager.py`（广播,E 组入口）/ `bench/results/`（数字出处）

---

## 📋 简历欠账表（只列没结清的）

| 段 | 简历原话 | 欠什么 |
|---|---|---|
| ③ | 创建对局耗时…降至 **226ms** | ⚠️ 226ms 是 max,无统计稳定性（复测两轮 187ms / 411ms）。**建议改成"降至亚秒级"**——不需要重测,只需改措辞（DEVLOG 019 / C07） |
| ⑤ | 独立 WS 网关分离连接与逻辑,Redis 路由表跨节点扇出；同 Tick 事件合并帧、分级推送、慢消费者背压 | E 组全部未做。⚠️ 2,000 连接与 180ms 是**这些优化之前**测出来的,别把因果挂上去 |
| ⑥ | 热榜**分片 ZSET + 定时归并 Top N + 本地缓存** | ✅ 已结清。缓存半段（L1+L2 / 事件驱动失效 / 布隆 / singleflight / 逻辑过期）、MQ 批量 ZINCRBY 写路径、分片 ZSET + 定时归并 + L1 读路径全部到位（DEVLOG 025~032）。⚠️ 边界：**单实例 Redis 下分片本身没有可报的吞吐数字**,它买的是"横向扩得动"和"没有单点热 key",别把它说成当下的性能提升 |
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

- [x] WS 网关拆成独立服务：`app/gateway.py` 独立入口,复用 `ws.router` 只挂 WS 一个路由,不进一致性哈希环（进环会把 1/N 房间分给没有业务逻辑的进程）；`ws.py` 去掉 `GameService` 依赖,推送等级改由 `core/ws_tier.py` 只读 Redis 快照判定；职责边界用 AST 断言钉住（DEVLOG 024）
- [x] 连接 → 网关节点映射存 Redis（简历说的"Redis 路由表"）：房间 → 持有其连接的节点集合,节点粒度登记,TTL 兜底崩溃残留
- [x] 跨节点扇出：查路由表定向 publish 到目标节点专属频道（不广播全集群）,本节点直发不绕 Redis,收到转发只做本地投递防环。11 项测试（DEVLOG 020）
- [x] 同 Tick 事件合并帧：50ms 合并窗口内同房间多个 STATE_UPDATE 只下发最后一帧,合并率看 `ws_frames_merged / ws_frames_sent`（DEVLOG 021）
- [x] 分级推送：`unicast` 操作者单播（跨节点按 user_id 筛）/ 玩家 50ms 即时 / 旁观者 500ms 聚合成一个 BATCH 帧；角色按"是否占座"服务端判定,不信客户端声明（DEVLOG 023）
- [x] 慢消费者背压：每连接独立写缓冲 + 写协程,广播不再 await socket（消除队头阻塞）；缓冲让出一次事件循环后仍满则以 1013 断开,指标 `ws_slow_consumers_dropped`（DEVLOG 022）

## F. 缓存（简历⑥·上）

- [x] L1 进程内 + L2 Redis 两级缓存,未命中回源 MySQL：`core/cache.py`,加在**回放事件流**读路径（全站唯一真回源 MySQL 的接口,且事件只增不改）。**刻意不加在对局状态路径**——那里的 `games` 是 Actor 权威状态,非归属节点留副本会重现 DEVLOG 018 对局回退。L1 有条数上限 + TTL 即一致性上限,指标 `cache_reads{level}`（DEVLOG 025）
- [x] 事件驱动失效 + TTL 兜底：Pub/Sub 失效通道补上 L1 跨进程失效不了的洞（L2 共享删一次即可,L1 在各进程堆里得喊一声）。**失效点挂在 flusher 刷库成功之后**,不在动作发生时——事件走 Write-Behind,提前失效会让读回源到还没刷入的 MySQL、把旧结果重新缓存 300s。失效失败不上抛,L1 短 TTL 才是保证（DEVLOG 026）
- [x] 穿透：布隆过滤器拦截不存在的 game_id：`core/bloom.py`，md5 双哈希（内置 `hash()` 受 `PYTHONHASHSEED` 影响，跨进程算出不同位 → 共享位图失效，同 C05）。**能当门卫全靠误判单向**：只会误判"存在"，绝不误判"不存在"。三条工程约定维持"无假阴性"：空位图放行 / key 不设 TTL / 启动时位图不存在则灌一遍库里已有 id——少一条就会把真实房间 404 掉。拦截点放在**跨节点转发之前**（省掉一次往返），刻意不拦 `load_game`（AI 任务与快照恢复走它）。Redis 故障一律放行，指标 `bloom_rejects`（DEVLOG 027）
- [x] 击穿：singleflight 互斥回源：`cache.py` 的 `_load_once`，`_inflight` 存 `key → future`。**用 future 不用锁**：锁只保证不同时查，后来者拿到锁还会各自再查一次；future 直接把第一次的结果交给所有等待者，N 个并发 → 1 次查询。两个真坑：① `asyncio.shield` 挡取消传播——不加的话一个等待者被取消会连带打断回源方和其他等待者，**收敛故障的机制反而放大故障**；回源方被取消则必须放掉等待者，所以捕获 `BaseException` 而非 `Exception`（`CancelledError` 不是 `Exception` 子类）。② 占位前必须二次检查 L1——L2 读那次 `await` 期间别的任务足够跑完一次回源并回填。互斥按 key 不按全局。边界：**进程内互斥不是集群级**，M 个进程最坏 M 次回源；不上分布式锁是因为它要在每次未命中路径上多付一次往返，而把 N 压到 1 是数量级改善、把 M 压到 1 只是常数级。10 项单测，`gather` 50 并发断言只回源 1 次，不用压测（DEVLOG 029）
- [x] 雪崩：逻辑过期异步重建 + TTL 抖动：L2 存的是信封 `{"v": 值, "exp": 逻辑过期时刻}`，物理 TTL = 逻辑过期 + 60s 宽限窗口 + ±10% 抖动。读到逻辑过期的值**先返回旧值、重建放后台**，调用方一次也不等（singleflight 把 N 次回源压成 1 次，但那 1 次还是有人在等）。抖动治的是**雪崩会自我强化**——第一轮集体到期后幸存的 key 全被对齐到同一时刻，下一轮更整齐。两个坑：① F-4 那个二次检查 L1 会让重建空转（旧值刚被填进 L1），所以重建走 `skip_l1` 但照旧查 `_inflight`；② `_decode` 返回三元组而非二元组——**None 是合法缓存值**，拿它当解析失败会把 F-3 的空结果缓存废掉。`KEY_PREFIX` 升 v2（值结构变了必须换前缀，这是 F-1 留它的用处）。13 项单测，核心那条判耗时不判返回值（DEVLOG 030）

## G. 热榜（简历⑥·下）

- [x] 写路径：对局结束 → MQ（已有 Celery `stats_queue`）→ 批量合并 ZINCRBY：`core/rank_buffer.py`，增量先 `HINCRBY` 攒进 HASH（field = `榜|玩家`），后台循环每秒 `RENAME` 换出 + pipeline 批量 `ZINCRBY`。**`ZADD` → `ZINCRBY` 省掉的不是一条命令而是那次查库**：`ZADD` 写绝对值就必须先知道现在是多少（那次 MySQL 查询的来源），`ZINCRBY` 写增量而增量在对局结束那刻已经知道。代价是不幂等，接受 at-least-once——榜是可以从 MySQL 全量重建的派生数据。收益分两笔别混着吹：**批量化是主要收益**（N 次往返变 1 次），同 member 合并在本项目里不大（一个人没法同时结束两局），比值看 `rank_updates_buffered / rank_updates_applied`。三个坑：① 换出必须 `RENAME` 不能"`HGETALL` 后 `DEL`"，后者会把两步之间进来的写连带抹掉且无从发现；② `RENAME` 的原子性顺带做掉多节点互斥，不用再套分布式锁；③ 删除放在 `ZINCRBY` 之后（宕机则重放一次，可自愈），反过来是永久少算。顺手修了既有 bug：`stats.py` 拿大写名字的集合比小写枚举值，`is_evil` 永远 False——坏人赢了不加分、好人赢了全场都加分，**不报错只是全算错**；判据收敛到 `game_enums.camp_of()`。13 项单测（DEVLOG 031）
- [x] 读路径：分片 ZSET + 定时归并 Top N + L1 本地缓存：每榜拆 8 片（`rank_buffer.shard_of`，md5 不用内置 `hash()`——写入进程和归并进程不是同一个，同 C05 盐坑），归并循环每 5s 取各片 Top N 排序、**连展示字段一起烤进快照 key**，读接口只读快照。**按 member 哈希分片而不是按写入轮转**：一个人的分数完整待在一片里，所以全局 Top N ⊆ 各片 Top N 的并集，归并只读 `SHARDS × limit`（8×10=80）行、**上界与总人数无关**；轮转分片会把同一个人拆散在多片，必须全表扫才能排序——**分片键约束的是查询形态**。不用 `ZUNIONSTORE`（要并全部成员，开销随人数走且堵住单线程 Redis），改在应用侧排序。原来"查 Redis 排序 → 拿 id 回 MySQL 查用户名场次"等于**榜单 QPS 直接压在库上**，现在读路径一次都不查库，查库降为每 5s 最多一次（口径 `rank_reads{result}`：`merged` 跟着读 QPS 涨就说明归并循环没跑）。快照**按最大 limit 存一份**、切片在应用侧（`limit` 是无界维度，按它分别存就是让 key 跟请求参数爆开，同 C02）。`get_or_load` 传 `redis=None` 只复用 L1 + singleflight（快照本身就是 L2）。归并循环**刻意不互斥**（纯读+覆盖写，重复只费 CPU），和写路径 `RENAME` 必须互斥恰好相反——**看重复执行会不会改变结果，不看是不是"后台任务"**。16 项单测（DEVLOG 032）

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
