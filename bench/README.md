# Aivalon 压测平台

> 工具链：Locust（Python，与项目同栈，可复用协议结构）。
> 纪律（见 AGENTS.md）：先基线后优化；所有数字必须可复现——报告需注明环境、参数、持续时间。

## 目录

- `prepare_users.py` — 压测数据准备：批量创建 `bench_user_*` 用户并签发 token，输出 `users.json`
- `locustfile.py` — S1 纯 API 场景（热榜/最近对局/对局历史）
- `users.json` — 生成的压测凭证（已 gitignore，勿提交）

## 快速开始

```bash
# 0. 前提：docker-compose up -d（中间件）+ uvicorn 运行在 8000
cd backend && source venv/bin/activate

# 1. 准备压测用户（幂等，可重复运行）
python ../bench/prepare_users.py --count 50

# 2. 运行 S1 场景（headless 示例：50 并发用户，每秒增加 10 个，持续 60s）
locust -f ../bench/locustfile.py --headless -u 50 -r 10 -t 60s --host http://localhost:8000

# 3. Web UI 模式（调试用）：去掉 --headless，浏览器打开 http://localhost:8089
```

## S2 对局主链路场景

S2 每个虚拟用户驱动一个完整对局（真人动作由驱动器轮询快照提交，AI 由 Celery worker 驱动）。

```bash
# 0. 额外前提：Celery worker 与 outbox relay 在线；且必须用压测配置启动后端：
cd backend && source venv/bin/activate
export AI_USE_LLM=false                    # AI 走规则引擎，避免 LLM 延迟/成本污染数据
export RATE_LIMIT_ACTION_TIMES=100         # 调高按用户限流阈值（默认 1次/秒 是真人手速语义）
export RATE_LIMIT_CREATE_GAME_TIMES=10000  # 同上（默认 10局/小时）
uvicorn app.main:app --port 8000 &
celery -A app.core.celery_app worker --loglevel=warning &
python -m app.core.outbox_relay &

# 1. 运行 S2（10 个并发房间，持续 120s）
locust -f ../bench/locust_s2.py --headless -u 10 -r 2 -t 120s --host http://localhost:8000
```

## S3 长连接场景

S3 每个虚拟用户 = 一条 WS 长连接，聚众到同一房间（热点场景），广播源独立控制发送速率。

```bash
# 终端 1：广播源（每秒 5 条广播，走真实 manager.broadcast 链路）
python ../bench/s3_broadcast_source.py --game-id s3-hot-room --rate 5

# 终端 2：连接压测（1000 连接，每秒新建 100 条，持续 40s）
locust -f ../bench/locust_s3.py --headless -u 1000 -r 100 -t 40s --host ws://localhost:8000
```

测量口径：`WS connect` = 握手+鉴权耗时（连接上限证据）；`WS recv` = 广播端到端延迟（服务端 ts → 客户端收到）。

> 注意：WS 鉴权曾按连接独占 DB 连接池（修复见 DEVLOG 006），梯度压测前先确认后端是最新代码。

## 设计要点

- **登录接口不进压测场景**：`/auth/login` 有 5次/分/IP 限流，压它测量的是限流器而非系统容量；压测 token 由 `prepare_users.py` 直接签发
- **压测流量标记**：每个请求带 `X-Request-ID: bench-s1-*` 前缀，可在访问日志中精确筛出压测流量（A.2 trace_id 的实战用途）
- **注册验证码旁路**：准备脚本直接写 Redis 验证码，跳过邮件发送；业务注册逻辑不变
- **邮箱域名注意**：email-validator 拒绝 `.local`/`.test` 等 RFC 特殊用途 TLD，压测邮箱用 `@aivalon-bench.com`

## 基线报告

见 `document/benchmark/`（v1 基线报告归档处）。
