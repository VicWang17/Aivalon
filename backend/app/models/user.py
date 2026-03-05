# 这个文件是用户数据库模型定义，映射到数据库中的 users 表，包含用户名、邮箱、密码哈希等字段。
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Statistics
    total_games = Column(Integer, default=0)
    wins_good = Column(Integer, default=0)  # 蓝方胜场
    wins_evil = Column(Integer, default=0)  # 红方胜场
    total_wins = Column(Integer, default=0) # 总胜场
