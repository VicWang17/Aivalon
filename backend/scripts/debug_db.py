import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal
from app.models.game import Game
from app.models.user import User

db = SessionLocal()
users = db.query(User).all()
print("Users:")
for u in users:
    print(f"  {u.id}: {u.username}")

games = db.query(Game).all()
print(f"Games: {len(games)}")
for g in games:
    print(f"  Game {g.id}: Players {g.player_ids}")

db.close()