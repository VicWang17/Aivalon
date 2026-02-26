# 这个文件是游戏相关的数据库模型定义，包含 Game（对局元信息）和 GameEvent（对局事件日志）。
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base

class Game(Base):
    __tablename__ = "games"

    id = Column(String(36), primary_key=True, index=True, comment="对局ID (UUID)")
    status = Column(String(32), nullable=False, default="created", comment="当前状态")
    winner = Column(String(16), nullable=True, comment="获胜阵营")
    player_ids = Column(JSON, nullable=False, comment="参与玩家ID列表")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    
    events = relationship("GameEvent", back_populates="game", order_by="GameEvent.seq")

class GameEvent(Base):
    __tablename__ = "game_events"

    id = Column(Integer, primary_key=True, index=True)
    game_id = Column(String(36), ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, comment="事件序号，从1开始")
    event_type = Column(String(32), nullable=False, comment="事件类型 (ActionType)")
    player_id = Column(Integer, ForeignKey("users.id"), nullable=True, comment="触发玩家ID")
    payload = Column(JSON, nullable=True, comment="事件详情数据")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    game = relationship("Game", back_populates="events")

    __table_args__ = (
        UniqueConstraint('game_id', 'seq', name='uix_game_seq'),
    )
