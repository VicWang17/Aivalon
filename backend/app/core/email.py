# 这个文件是邮件发送的工具类，封装了 FastAPI-Mail 的配置和发送逻辑，用于发送验证码等系统通知。
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from app.core.config import settings
from pydantic import EmailStr
import random
import string

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

def generate_verification_code(length: int = 6) -> str:
    """生成指定长度的数字验证码"""
    return ''.join(random.choices(string.digits, k=length))

async def send_verification_email(email: EmailStr, code: str):
    """发送验证码邮件"""
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
    
    await fm.send_message(message)
