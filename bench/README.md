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

## 设计要点

- **登录接口不进压测场景**：`/auth/login` 有 5次/分/IP 限流，压它测量的是限流器而非系统容量；压测 token 由 `prepare_users.py` 直接签发
- **压测流量标记**：每个请求带 `X-Request-ID: bench-s1-*` 前缀，可在访问日志中精确筛出压测流量（A.2 trace_id 的实战用途）
- **注册验证码旁路**：准备脚本直接写 Redis 验证码，跳过邮件发送；业务注册逻辑不变
- **邮箱域名注意**：email-validator 拒绝 `.local`/`.test` 等 RFC 特殊用途 TLD，压测邮箱用 `@aivalon-bench.com`

## 基线报告

见 `document/benchmark/`（v1 基线报告归档处）。
