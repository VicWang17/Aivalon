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

@router.get("/leaderboard", response_model=ResponseModel[List[LeaderboardEntry]])
async def get_leaderboard(
    type: Literal["total", "good", "evil"] = Query("total", description="排行榜类型"),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """
    获取排行榜
    """
    query = db.query(User)
    
    if type == "total":
        query = query.order_by(desc(User.total_wins))
    elif type == "good":
        query = query.order_by(desc(User.wins_good))
    elif type == "evil":
        query = query.order_by(desc(User.wins_evil))
        
    # 次要排序：总场次少者优先（效率高）
    query = query.order_by(User.total_games.asc())
    
    users = query.limit(limit).all()
    
    results = []
    for u in users:
        win_rate = 0.0
        if u.total_games > 0:
            win_rate = u.total_wins / u.total_games
            
        entry = LeaderboardEntry(
            user_id=u.id,
            username=u.username,
            total_games=u.total_games,
            wins_good=u.wins_good,
            wins_evil=u.wins_evil,
            total_wins=u.total_wins,
            win_rate=round(win_rate * 100, 1)
        )
        results.append(entry)
        
    return ResponseModel(data=results)
