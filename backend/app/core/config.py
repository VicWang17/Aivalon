# 这个文件是项目的全局配置管理，使用 pydantic-settings 从环境变量或 .env 文件加载配置，确保类型安全。
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os

class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "Aivalon"
    API_V1_STR: str = "/api/v1"
    
    # MySQL
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_HOST: str = "localhost"  # 默认为 localhost，避免 .env 缺失时报错
    MYSQL_PORT: int = 3306         # 默认为 3306，避免 .env 缺失时报错
    MYSQL_DATABASE: str
    
    @property
    def SQLALCHEMY_DATABASE_URL(self) -> str:
        return f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    
    # Email
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_PORT: int = 465
    MAIL_SERVER: str = "smtp.163.com"
    MAIL_STARTTLS: bool = False
    MAIL_SSL_TLS: bool = True

    # Security
    SECRET_KEY: str = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 600

    # LLM (DeepSeek)
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # RabbitMQ / Celery
    RABBITMQ_DEFAULT_USER: str = "guest"
    RABBITMQ_DEFAULT_PASS: str = "guest"
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672

    # Rate Limit（v2 改为按用户维度统计，见 app/core/rate_limit.py；压测时可用环境变量调高阈值）
    RATE_LIMIT_CREATE_GAME_TIMES: int = 10
    RATE_LIMIT_CREATE_GAME_SECONDS: int = 3600
    RATE_LIMIT_ACTION_TIMES: int = 1
    RATE_LIMIT_ACTION_SECONDS: int = 1

    # AI
    AI_USE_LLM: bool = True  # False 时 AI 直接走规则引擎：压测专用，避免 LLM 延迟与成本污染数据
    AI_TASK_RATE_LIMIT: str = "60/m"  # AI 任务限流：v1 为保护 LLM 配额所设；压测时调高（如 100000/m）

    @property
    def CELERY_BROKER_URL(self) -> str:
        return f"amqp://{self.RABBITMQ_DEFAULT_USER}:{self.RABBITMQ_DEFAULT_PASS}@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}//"

    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env"),
        extra="ignore"
    )

settings = Settings()
