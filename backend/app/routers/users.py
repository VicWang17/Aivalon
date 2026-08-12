from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Literal
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import LeaderboardEntry
from app.schemas.base import ResponseModel
# from app.core.deps import get_current_user # 不需要鉴权即可查看

router = APIRouter()

from app.services.rank_service import RankService

@router.get("/leaderboard", response_model=ResponseModel[List[LeaderboardEntry]])
async def get_leaderboard(
    type: Literal["total", "good", "evil"] = Query("total", description="排行榜类型"),
    limit: int = Query(10, ge=1, le=100),
    # db: Session = Depends(get_db) # 不再需要 DB 依赖，RankService 内部处理
):
    """
    获取排行榜 (使用 Redis 缓存加速)
    """
    # 归并快照里已经带着展示字段了（用户名、场次、胜率），这里不再逐个查库拼装——
    # 原来这个接口每次请求都要拿着榜上的 id 去 MySQL 查一遍，
    # 等于榜单 QPS 直接压在库上。见 rank_service.py 读路径那段说明。
    entries = await RankService.get_leaderboard(type, limit)
    return ResponseModel(data=[LeaderboardEntry(**e) for e in entries])
