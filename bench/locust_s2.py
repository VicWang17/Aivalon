# S2 对局主链路场景：每个虚拟用户驱动一个完整对局（创建房间 → 与 7 个 AI 打完全场 → 结算）
#
# 运行示例（需要 uvicorn + Celery worker + outbox relay 全部在线）：
#   cd backend && source venv/bin/activate
#   locust -f ../bench/locust_s2.py --headless -u 10 -r 2 -t 120s --host http://localhost:8000
#
# 说明：
# - 真人动作由本驱动器轮询快照后提交；AI 动作由 Celery worker 消费队列后回写
# - 压测要求 AI_USE_LLM=False（规则引擎）与调高限流阈值（RATE_LIMIT_*），见 README
# - 8 人局任务人数配置固定为 3-4-4-5-5（与 game_rules 一致）
import json
import math
import random
import time
import uuid
from pathlib import Path

from locust import HttpUser, between, events, task

USERS_FILE = Path(__file__).parent / "users.json"
USERS = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else []

RESULTS_DIR = Path(__file__).parent / "results"

# ---------------------------------------------------------------------------
# 原始延迟采集：写进简历的分位数不能用 locust CSV 里的那份
#
# 两个问题，都会让"动作 P99"这个数字站不住：
# 1. locust 的分位数是**分桶估算**——它把响应时间归整后计数（<100ms 归到 1ms、
#    <1000ms 归到 10ms、更大归到 100ms），再从桶里插值。同 DEVLOG C03 讲 Histogram
#    的那套，够看趋势，但要写简历就该用原始值算。
# 2. **分动作看 P99 等于看噪声**。一轮 120s 里 speak 约 200 个请求、propose 只有
#    20 来个，P99 落在第 2~3 慢的那几个样本上，换一轮就能差一倍（这正是
#    s2_u20_fix_r1 的 47ms 和 r2 的 130ms 差那么远的原因）。
#    所以额外把所有动作类请求汇成一个池子，样本量上千，分位数才有意义。
# ---------------------------------------------------------------------------
_SAMPLES: dict[str, list[float]] = {}


@events.request.add_listener
def _collect_sample(name, response_time, exception, **kwargs):
    """只收成功请求：失败请求的耗时是超时值，混进延迟分位数里没有意义"""
    if exception is not None:
        return
    _SAMPLES.setdefault(name, []).append(response_time)


def _pct(sorted_vals: list[float], p: float) -> float:
    """最近秩法（nearest-rank）：第 ceil(p*n) 个样本，不做插值。
    口径写明是因为不同工具的分位数定义不同，换算法数字会变。"""
    if not sorted_vals:
        return float("nan")
    idx = max(0, math.ceil(p * len(sorted_vals)) - 1)
    return sorted_vals[idx]


def _report(rows: list[tuple[str, list[float]]]) -> str:
    lines = [f"{'接口':<34}{'样本':>7}{'P50':>9}{'P90':>9}{'P95':>9}{'P99':>9}{'max':>9}"]
    for name, vals in rows:
        s = sorted(vals)
        lines.append(
            f"{name:<34}{len(s):>7}"
            f"{_pct(s, .50):>8.1f}{_pct(s, .90):>9.1f}{_pct(s, .95):>9.1f}"
            f"{_pct(s, .99):>9.1f}{s[-1]:>9.1f}"
        )
    return "\n".join(lines)


@events.quitting.add_listener
def _dump_samples(environment, **kwargs):
    """落原始值 + 打印精确分位数。原始值必须留档，否则事后换口径要重跑压测。"""
    if not _SAMPLES:
        return
    prefix = getattr(environment.parsed_options, "csv_prefix", None) or "s2_raw"
    out = RESULTS_DIR / f"{Path(prefix).name}_latency_raw.json"

    actions = [v for name, vals in _SAMPLES.items()
               if name.startswith("POST /action") for v in vals]
    rows = sorted(_SAMPLES.items())
    if actions:
        rows.append(("动作类合计（POST /action *）", actions))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"note": "单位 ms，仅成功请求；分位数口径为最近秩法",
         "samples": {k: v for k, v in _SAMPLES.items()},
         "action_pool": actions},
        ensure_ascii=False))
    print(f"\n=== 精确分位数（原始值，最近秩法，单位 ms）===\n{_report(rows)}")
    print(f"原始样本已存 {out}")

# ---------------------------------------------------------------------------
# 被拒之后做什么：照 `Retry-After` 退避，且**不把这一局丢掉**
#
# 原来 `_step` 里快照非 200 就 `self.game_id = None`，等于**把 429 理解成"这局没了"**：
# 下一个 tick 去建新局，建局也被拒、`game_id` 仍是 None，再下一个 tick 又建一次。
# S4 run B 里 500 个用户 120 秒发出 28,729 次建局尝试，真正的对局动作只跑了 221 次
# ——**驱动器自己把限流放大成了流量**（DEVLOG 044）。
#
# 这正是 H-3a「429 必须带 Retry-After」那条规则反过来长在客户端身上：服务端两条
# 429 路径都给了这个头（准入中间件的 JSONResponse、`sliding_window.py:147` 的
# HTTPException），**这次是客户端没听**。
#
# 两个决定：
# 1. **"这次没成功"和"这局结束了"必须分开判**。同一个状态位承载两种含义，压力下
#    必然选错那种。现在只有三种情况才清掉 `game_id`：phase 到 `finished`（正常结算）、
#    404（这局真没了）、以及建局本身失败（压根没有局）。429 和 5xx 都只是**这次没成功**,
#    局还在，下个 tick 接着轮询。
# 2. **退避必须有上界**。`Retry-After` 可能来自建局配额（10 局/小时 → 可能报出几千秒),
#    照着睡就等于这个用户在本轮压测里彻底消失、还消失得无声无息。封顶之后超过上界的
#    等待会被截断——**测出来的是"它想让我等多久"，而不是"我愿意等多久"**。
#    `time.sleep` 在 locust 下是 gevent 打过补丁的协作式让出，不会堵住别的用户。
MAX_BACKOFF = 30.0
DEFAULT_BACKOFF = 1.0  # 没给 Retry-After 时的兜底，不要变成 0（那就是不退避）

TEAM_SIZES = [3, 4, 4, 5, 5]  # 8 人局第 1~5 轮任务人数
EVIL_CHARS = {"assassin", "morgana", "minion"}
# AI 占位的假用户 ID（库中不存在即被识别为 AI，路由层会随机命名）
AI_IDS = [900001, 900002, 900003, 900004, 900005, 900006, 900007]


class S2GameUser(HttpUser):
    """一个虚拟用户 = 一个进行中的对局房间的真人驱动器"""

    wait_time = between(0.3, 0.8)

    def on_start(self):
        if not USERS:
            raise RuntimeError("bench/users.json 不存在，先运行 prepare_users.py")
        self.me = random.choice(USERS)
        self.game_id = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.me['access_token']}",
            "X-Request-ID": f"bench-s2-{uuid.uuid4().hex[:12]}",
        }

    def _backoff(self, resp) -> None:
        """被拒了就照服务端说的等，等待有上界。见 MAX_BACKOFF 上方的说明。

        **服务端已经把答案写在响应里了**，客户端自己拍一个间隔纯属多余——
        `Retry-After` 是按"还差多少令牌 / 最老那次请求何时滑出窗口"算出来的，
        比任何猜测都准。头缺失或不是数字时才退回 DEFAULT_BACKOFF。
        """
        try:
            wait = float(resp.headers.get("Retry-After", DEFAULT_BACKOFF))
        except (TypeError, ValueError):
            wait = DEFAULT_BACKOFF
        time.sleep(min(max(wait, 0.0), MAX_BACKOFF))

    @task
    def play(self):
        if self.game_id is None:
            self._create_game()
        else:
            self._step()

    def _create_game(self):
        payload = {"player_ids": [self.me["user_id"]] + AI_IDS}
        with self.client.post(
            "/api/v1/games/", json=payload, headers=self._headers(),
            name="POST /games (创建对局)", catch_response=True,
        ) as resp:
            if resp.status_code == 200 and resp.json().get("data", {}).get("game_id"):
                self.game_id = resp.json()["data"]["game_id"]
                resp.success()
            else:
                resp.failure(f"创建对局失败: {resp.status_code} {resp.text[:200]}")
                # 建局失败时 game_id 保持 None 是对的（压根没有局），但**不能立刻再试**：
                # 那正是 run B 里 28,729 次建局尝试的另一半原因。
                if resp.status_code in (429, 503):
                    self._backoff(resp)

    def _step(self):
        with self.client.get(
            f"/api/v1/games/{self.game_id}", headers=self._headers(),
            name="GET /games/{id} (轮询快照)", catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"快照获取失败: {resp.status_code}")
                # 只有 404 才是"这局真没了"。429/5xx 都只是**这次没成功**，
                # 局还在——清掉 game_id 就会去建新局，把限流放大成流量（见文件中部说明）。
                if resp.status_code == 404:
                    self.game_id = None
                elif resp.status_code in (429, 503):
                    # 503 和 429 是同一类信号（"稍后再来"），Redis 池那个 handler
                    # 也带 Retry-After。**刻意不给 500 退避**：那是"我们坏了"，
                    # 不是"现在别来"——它该在曲线上又快又响地暴露出来。
                    self._backoff(resp)
                return
            state = resp.json()["data"]

        if state["phase"] == "finished":
            self.game_id = None  # 本局结束，下次 task 开新局
            return

        action = self._decide(state)
        if action:
            with self.client.post(
                f"/api/v1/games/{self.game_id}/action",
                json=action, headers=self._headers(),
                name=f"POST /action ({action['action_type']})",
                catch_response=True,
            ) as resp:
                # 动作被拒同样只是这次没成功：该我行动这件事没变，退避后下个 tick
                # 会从快照里重新读到轮次还在我身上，然后再交一次。
                if resp.status_code == 429:
                    self._backoff(resp)

    def _decide(self, state: dict) -> dict | None:
        """根据快照判断"现在是否轮到我行动"，返回动作或 None"""
        my_id = self.me["user_id"]
        me = next((p for p in state["players"] if p["user_id"] == my_id), None)
        if me is None:
            return None
        phase = state["phase"]

        if phase == "speech" and state.get("speaker_id") == my_id:
            return {"action_type": "speak", "payload": {"content": "过。", "is_end": True}}

        if phase == "team_proposal" and state.get("leader_id") == my_id:
            size = TEAM_SIZES[state["round"] - 1]
            others = [p["user_id"] for p in state["players"] if p["user_id"] != my_id]
            team = [my_id] + random.sample(others, size - 1)
            return {"action_type": "propose", "payload": {"target_ids": team}}

        if phase == "vote" and not me["has_voted"]:
            return {"action_type": "vote", "payload": {"option": "approve"}}

        if phase == "mission" and my_id in state.get("proposed_team", []) and not me["has_acted"]:
            result = "fail" if me.get("character") in EVIL_CHARS else "success"
            return {"action_type": "mission", "payload": {"result": result}}

        if phase == "assassination" and me.get("character") == "assassin":
            target = next(p["user_id"] for p in state["players"] if p["user_id"] != my_id)
            return {"action_type": "assassinate", "payload": {"target_id": target}}

        return None
