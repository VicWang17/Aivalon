# 这个文件是项目的全局配置管理，使用 pydantic-settings 从环境变量或 .env 文件加载配置，确保类型安全。
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

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

    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

settings = Settings()
