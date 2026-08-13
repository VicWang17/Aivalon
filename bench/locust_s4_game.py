# S4 突发流量场景（对局链路版）：阶梯形态负载打在**建局 + 提交动作**上，不是读接口。
#
# 为什么要有这个文件——`locust_s4.py` 证不到简历⑦
# ------------------------------------------------
# `locust_s4.py` 复用的是 `S1ApiUser`：榜单 / 最近对局 / 个人历史，**全是读接口**。
# 而简历⑦ 写的是「10 倍突发流量下**核心对局链路**零中断」——建局和提交动作压根不在
# 那个场景里。**就算它全绿，能证明的也只是"读接口在突发下不中断"**，那不是要证的事。
# 所以这里复用 `S2GameUser`（真人驱动器：建房 → 轮询快照 → 该我时提交动作 → 结算）
# 配上 S4 的阶梯 shape。
#
# 三个测量口径的决定，写在这里是因为**每一条错了都会让结论变成废数字**
# ------------------------------------------------------------------
# 1. **一个虚拟用户 = 一个独立账号，刻意不随机复用**。
#    `S2GameUser.on_start` 是 `random.choice(USERS)`，50 个账号跑 500 个虚拟用户时，
#    每个账号身上平均压着 10 个驱动器。而对局链路的限流键是 `user:{sub}`
#    （见 `rate_limit.py:user_or_ip_identifier`），于是测到的是**同一个账号被 10 倍手速刷**,
#    动作配额 1 次/秒当场就拒——**那是限流器在正确工作，不是系统的容量边界**。
#    **10 倍突发的正确形状是"人变多"，不是"每个人变快"**：真实的十倍流量是 500 个人
#    各自按人的速度玩，不是 50 个人手速超人 10 倍。所以这里按 index 绑定账号，
#    并且**账号不够就直接拒跑**（见 `on_start`）——**悄悄复用账号会给出一个看起来
#    很惨、但什么都不说明的失败率**。
#
# 2. **驱动器降到人的速度，刻意不调高业务配额**。
#    `S2GameUser` 的 `wait_time` 是 0.3~0.8s（轮询 + 立刻提交的机器人节奏，
#    折合每用户 1.5~3 req/s），而快照读配额是 60 次/分、动作是 1 次/秒——
#    **机器人节奏本身就超配额**。两条路：调高阈值，或者把驱动器放慢到真人节奏。
#    这里选后者。**调高阈值是改测量口径去迎合想要的结论**：那些阈值是按"真人能多快"
#    定的（`rate_limit.py` 文件头），为了让压测跑得漂亮把它们放大，等于测了一个
#    线上不存在的系统。放慢到 1~2s 后每用户约 0.67 req/s，快照约 40 次/分、
#    动作远低于 1 次/秒，**每个虚拟用户都在配额之内**——于是拒绝一旦出现，
#    那就真的是系统容量到了，而不是我自己配的业务规则在生效。
#
# 3. **单机压测跑不出多客户端的形状，这一条必须在跑之前处理掉**。
#    网关准入的 IP 层是 `capacity=60 / rate=20`（`RATE_LIMIT_IP_*`），而它在鉴权之前、
#    按 `request.client.host` 算（刻意不读 `X-Forwarded-For`：可伪造的限流键等于
#    没有限流键）。500 个 Locust 用户全从 127.0.0.1 出去，**共享一个 IP 桶、稳态只放
#    20 rps**,剩下的在网关就被拒了——这一轮就只测到了那个 IP 桶。
#    正确的处理是**按测量目的调 IP 层，绝不动全局层**：IP 层存在的意义是"挡住某一个
#    打我的来源"，而真实的十倍突发来自 500 个不同的来源，用一台机器模拟它就必须
#    让 IP 层反映这个事实。全局层（`RATE_LIMIT_GLOBAL_*`）**一个字都不能改**——
#    那正是 S4 要测的那层。跑法见文件末尾。
#
# 运行示例（uvicorn + Celery worker + outbox relay 全部在线，且跑的是当前代码）：
#   # ① 账号必须够：500 个虚拟用户 = 500 个独立账号
#   cd backend && source venv/bin/activate && python ../bench/prepare_users.py --count 500
#   # ② 只放宽 IP 层（模拟 500 个来源），全局层保持原值
#   RATE_LIMIT_IP_CAPACITY=1000 RATE_LIMIT_IP_RATE=1000 <启动 uvicorn>
#   # ③ 跑（用户数由 shape 控制，不要给 -u/-r）
#   locust -f ../bench/locust_s4_game.py --headless -t 180s \
#          --host http://localhost:8000 --csv results/s4_v3_game
#
# 观察点（和读接口版不同的地方）：
# - 突发瞬间建局是否被拒：那是 L4/L5 降级和全局准入在说话，**429/503 是预期行为**，
#   要看的是它有没有带 `Retry-After`、以及**已在局中的房间是否照常推进**——
#   「零中断」指的是后者，不是"一个请求都没被拒"。
# - AI 队列深度 → 自动降级是否触发（`ai_turns_degraded_total{reason="queue_depth"}`）：
#   每建一局就是 7 个 AI 回合投递，这条链路的压力主要在这儿而不在 HTTP 上。
# - 500 那档的 **500 计数必须为 0**：过载该答 429/503，答 500 就是又有一层把
#   "现在别来"翻译成了"我崩了"（同 DEVLOG 042 那个 Redis 池）。
import itertools
import json
import sys
from pathlib import Path

from locust import LoadTestShape, between

sys.path.insert(0, str(Path(__file__).parent))
from locust_s2 import S2GameUser, USERS  # 复用对局驱动器 + 原始延迟采集的 events 监听

PEAK_USERS = 500

# 账号发号器：按启动顺序取，保证一人一号。
# 用 itertools.count 而不是 random.choice，理由见文件头第 1 条。
_seat = itertools.count()


class S4GameUser(S2GameUser):
    """对局链路驱动器，改两处：账号一人一个、节奏降到真人速度。

    刻意用继承而不是把 `locust_s2.py` 改掉：S2 那个场景测的是**稳态吞吐下的动作
    P99**（简历③ 的数字出处），它的机器人节奏在那个目的下是对的。
    **两个场景要的是两种节奏，把它们统一成一个就有一个会测错。**
    """

    # 真人节奏。见文件头第 2 条：**刻意不去调高业务配额来迁就机器人节奏**。
    wait_time = between(1.0, 2.0)

    def on_start(self):
        if not USERS:
            raise RuntimeError("bench/users.json 不存在，先运行 prepare_users.py")
        if len(USERS) < PEAK_USERS:
            # **刻意直接拒跑，不退化成复用账号**：复用会给出一个看起来很惨、
            # 但只说明"限流器在拒同一个账号的超速请求"的失败率，
            # 而那个数字和系统容量无关——**一个不说明任何事的红色结果比报错更糟，
            # 因为它看起来像个结论**。
            raise RuntimeError(
                f"账号不够：users.json 有 {len(USERS)} 个，峰值需要 {PEAK_USERS} 个。\n"
                f"跑 `python ../bench/prepare_users.py --count {PEAK_USERS}` 再来。\n"
                f"（复用账号测到的是动作配额 1 次/秒在拒超速请求，不是系统容量）"
            )
        self.me = USERS[next(_seat) % len(USERS)]
        self.game_id = None


class BurstGameShape(LoadTestShape):
    """阶梯负载形态，和 `locust_s4.py` 保持同一形状以便对照：
      0~60s    50 用户    基线流量
      60~120s  500 用户   10 倍突发（spawn_rate 拉满，近似瞬时打满）
      120~180s 50 用户    回落，观察是否"起不来"（积压 / 资源泄漏）

    **回落那 60 秒不是凑数**：突发期的失败率只说明系统扛不扛得住，
    回落后起不来才是真正的伤——**积压和泄漏只在压力撤掉之后才看得出来**。
    """

    def tick(self):
        run_time = self.get_run_time()
        if run_time < 60:
            return (50, 10)
        elif run_time < 120:
            return (PEAK_USERS, PEAK_USERS)
        elif run_time < 180:
            return (50, 50)
        return None
