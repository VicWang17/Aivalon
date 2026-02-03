# 这个文件是应用程序运行时连接数据库的入口，提供数据库引擎、会话工厂、Base模型基类和FastAPI依赖注入函数，用于业务代码与数据库的交互。
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(os.path.join(os.path.dirname(__file__), "../../../.env"))

# 从环境变量读取配置
MYSQL_USER = os.getenv("MYSQL_USER", "aivalon_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "aivalon_password")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "aivalon")

SQLALCHEMY_DATABASE_URL = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    pool_pre_ping=True,  # 自动重连
    pool_recycle=3600,   # 连接回收时间
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """Dependency for FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
