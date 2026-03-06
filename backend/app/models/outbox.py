# 这个文件是Outbox（事务性发件箱）的数据库模型定义，用于实现可靠的事件通知。
from sqlalchemy import Column, Integer, String, JSON, DateTime, Text
from sqlalchemy.sql import func
from app.db.base import Base

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = Column(Integer, primary_key=True, index=True)
    aggregate_type = Column(String(32), nullable=False, index=True, comment="聚合根类型 (如 game)")
    aggregate_id = Column(String(36), nullable=False, index=True, comment="聚合根ID (如 game_id)")
    event_type = Column(String(64), nullable=False, comment="事件类型")
    payload = Column(JSON, nullable=False, comment="事件内容")
    status = Column(String(16), default="pending", index=True, nullable=False, comment="发送状态: pending, processing, processed, failed")
    retry_count = Column(Integer, default=0, nullable=False, comment="重试次数")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    error_log = Column(Text, nullable=True, comment="最近一次错误日志")
