import sys
import os
# Add backend path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app
from app.core.deps import get_current_user
from app.models.user import User

# Mock user dependency
# We assume user with ID 1 exists and has played games (from previous tests)
def override_get_current_user():
    # Construct a user object that mimics the DB model enough for dependencies
    # The actual user data doesn't matter much as long as ID matches existing data
    return User(id=2, username="闪电杰尼", email="user2@example.com")

app.dependency_overrides[get_current_user] = override_get_current_user

client = TestClient(app)

def test_history():
    print("Fetching history for user 1...")
    response = client.get("/api/v1/games/history")
    if response.status_code != 200:
        print(f"Failed to fetch history: {response.status_code} {response.text}")
        return

    games = response.json()
    print(f"Found {len(games)} games in history.")
    
    if not games:
        print("No games found. Please run create_test_users.py and test_concurrency.py to generate data.")
        return

    # Check structure
    first_game = games[0]
    print(f"Latest game: {first_game['id']} created at {first_game['created_at']}")
    print(f"Player IDs: {first_game['player_ids']}")
    
    game_id = first_game["id"]
    
    print(f"\nFetching events for game {game_id}...")
    response = client.get(f"/api/v1/games/{game_id}/events")
    if response.status_code != 200:
        print(f"Failed to fetch events: {response.status_code} {response.text}")
        return
        
    events = response.json()
    print(f"Found {len(events)} events.")
    
    # Print first 5 events
    print("First 5 events:")
    for e in events[:5]:
        print(f"  Seq {e['seq']}: {e['event_type']} - Payload: {str(e['payload'])[:50]}...")

if __name__ == "__main__":
    test_history()