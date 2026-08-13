# 这个文件是邮件发送的封装（验证码等系统通知），并且把这次发送**限时、限次、不抛**。
#
# 邮件的失败形态和 LLM 完全不一样
# --------------------------
# LLM 那边：调用方在等，所以熔断买到的是"不白等"（见 core/breaker.py）。
# 邮件这边：它跑在 FastAPI 的 `BackgroundTask` 里，**响应早就发出去了，没有任何人在等**。
# 所以熔断在这里买到的不是时间，是**并发槽位和 SMTP 连接**：
# 邮件服务挂掉时，每个发码请求都会占住一个协程和一条 SMTP 连接直到超时，
# 而邮箱服务商给的连接配额很小（163 这类只给个位数）——**占满之后连正常的信也发不出去了**。
# 一句话：**同一个模式（熔断）在不同依赖上省的东西不同，别照抄理由**。
#
# 原来这里的三个洞
# -------------
#   1. **压根没有超时**。`fm.send_message` 卡住就是永久卡住，而它跑在 API 进程的
#      事件循环里——挂住的任务只增不减。同 H-2 那条：**上界要卡在自己代码里。**
#   2. **它会抛**，而调用方是 `background_tasks.add_task`：异常抛到那里没有任何人能处理，
#      只会变成一行服务器日志。**没人能据此行动的异常，抛出去就只是噪音。**
#   3. **失败完全不可观测**。用户拿到的是"验证码已发送"，然后永远收不到信——
#      而我们这边一个计数器都没涨。**这是最坏的一种故障：用户知道坏了，我们不知道。**
#
# 熔断的 `implies_level` 刻意是 0
# ---------------------------
# 邮件挂了**不该让降级矩阵动一格**。矩阵每一级砍的都是对局链路的成本，
# 而邮件不在那条链路上——为了发不出验证码就把全站往下拧一档是荒谬的。
# **不是每个熔断器都该挂降级级别**：挂之前要问"这个依赖不可用，和'对局链路要省成本'
# 是同一件事吗"。LLM 是（AI 全走规则引擎就是 L2），邮件不是。
import asyncio
import logging
import random
import string

from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr

from app.core import breaker, metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.email")

# 邮件配置
conf = ConnectionConfig(
    MAIL_USERNAME=settings.MAIL_USERNAME,
    MAIL_PASSWORD=settings.MAIL_PASSWORD,
    MAIL_FROM=settings.MAIL_FROM,
    MAIL_PORT=settings.MAIL_PORT,
    MAIL_SERVER=settings.MAIL_SERVER,
    MAIL_STARTTLS=settings.MAIL_STARTTLS,
    MAIL_SSL_TLS=settings.MAIL_SSL_TLS,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=True
)

fm = FastMail(conf)

# 邮件熔断器。样本门槛比 LLM 低（发码本来就是低频接口，等 8 个样本可能要等很久，
# **门槛定得比流量还高的熔断器等于没有**），但失败比例要求更高：
# 发信失败有时是单个收件地址的问题（对方邮箱满了、域名不存在），
# 那不是"依赖挂了"——比例定高一点才不会被几个坏地址带跳闸。
email_breaker = breaker.register(breaker.Breaker(
    "email",
    window=settings.BREAKER_EMAIL_WINDOW,
    min_samples=settings.BREAKER_EMAIL_MIN_SAMPLES,
    failure_ratio=settings.BREAKER_EMAIL_FAILURE_RATIO,
    open_for=settings.BREAKER_EMAIL_OPEN_FOR,
    implies_level=0,        # 刻意为 0，见文件头
))


def generate_verification_code(length: int = 6) -> str:
    """生成指定长度的数字验证码"""
    return ''.join(random.choices(string.digits, k=length))


def is_available() -> bool:
    """邮件服务现在**值不值得承诺给用户**。

    这不是第二道闸——真正的闸在 `send_verification_email` 里（挂在依赖边界上，
    见 breaker.py）。这个函数回答的是另一个问题：**要不要向用户承诺"已发送"**。
    两件事必须分开，因为承诺发生在调用之前：发码接口先写 Redis（验证码 + 60s 发送间隔）
    才把发信丢进后台，邮件挂着的话用户**既收不到信、又被那个 60s 锁在外面**。
    所以要在写这两个 key 之前先问一句。
    """
    return email_breaker.state != breaker.OPEN


async def send_verification_email(email: EmailStr, code: str) -> bool:
    """发送验证码邮件。**永不抛异常**，返回是否真的发出去了。

    不抛的理由和 H-2 的 LLM 舱壁一样但更彻底：这里的调用方是 `BackgroundTask`，
    **抛出去连重试都触发不了，只会变成一行没人看的日志**。所以失败在这里就地
    收敛成日志 + 指标 + 熔断记账。
    """
    if not email_breaker.allow():
        metrics.email_sends.labels(result="breaker_open").inc()
        logger.warning("邮件熔断中，跳过发送给 %s", email)
        return False

    message = MessageSchema(
        subject="Aivalon 注册验证码",
        recipients=[email],
        body=f"""
        <div style="background-color:#f4f4f4;padding:20px">
            <div style="max-width:600px;margin:0 auto;background:#fff;padding:40px;border-radius:10px">
                <h2>欢迎注册 Aivalon</h2>
                <p>您的验证码是：</p>
                <div style="background:#f0f0f0;padding:20px;text-align:center;font-size:24px;letter-spacing:5px;font-weight:bold;margin:20px 0">
                    {code}
                </div>
                <p>验证码有效期为 5 分钟，请勿泄露给他人。</p>
            </div>
        </div>
        """,
        subtype=MessageType.html
    )

    try:
        # `wait_for` 在最外层才是权威上界（同 H-2）：SMTP 客户端自己的超时管不到
        # 建连、握手、投递这几段的总和，而卡住的是哪一段我们事前并不知道
        await asyncio.wait_for(fm.send_message(message),
                              timeout=settings.MAIL_SEND_TIMEOUT)
    except asyncio.TimeoutError:
        # 这个分支必须排在 `except Exception` 前面：3.11 起它就是内置 `TimeoutError`，
        # 而那是 `OSError` 的子类，**会被 `except Exception` 抓走**（同 H-2、DEVLOG 034）
        metrics.email_sends.labels(result="timeout").inc()
        email_breaker.record(False)
        logger.warning("邮件发送超时（上界 %.1fs）: %s", settings.MAIL_SEND_TIMEOUT, email)
        return False
    except asyncio.CancelledError:
        # 取消是我们自己的决定，不算依赖失败，也不能吞（同 DEVLOG 029）
        raise
    except Exception as e:
        metrics.email_sends.labels(result="error").inc()
        email_breaker.record(False)
        logger.warning("邮件发送失败 %s: %s", email, e)
        return False

    metrics.email_sends.labels(result="success").inc()
    email_breaker.record(True)
    return True
