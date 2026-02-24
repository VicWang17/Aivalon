import sys
import os
import requests

# Add backend path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configuration
API_URL = "http://localhost:8000/api/v1"
# Assuming we have a test user "user_2" with password "password" (from create_test_users.py)
# If not, we might need to create one or use an existing one.
# Let's try with user_2 first.

def debug_create_game():
    # 1. Login to get token
    login_data = {
        "username": "user_2",
        "password": "password"
    }
    
    print(f"Logging in as {login_data['username']}...")
    try:
        response = requests.post(f"{API_URL}/auth/login", json=login_data)
        if response.status_code != 200:
            print(f"Login failed: {response.status_code} {response.text}")
            return
            
        token = response.json()["data"]["access_token"]
        print("Login successful. Token obtained.")
        
    except Exception as e:
        print(f"Login error: {e}")
        return

    # 2. Create Game
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Construct player IDs: user_2 (id=3) + 7 bots
    # The ID of user_2 depends on the DB. Let's get "me" first to be sure.
    
    response = requests.get(f"{API_URL}/auth/me", headers=headers)
    if response.status_code != 200:
        print(f"Get me failed: {response.status_code} {response.text}")
        return
        
    me = response.json()
    my_id = me["data"]["id"]
    print(f"My ID: {my_id}")
    
    bot_ids = [my_id + 1000 + i for i in range(7)]
    player_ids = [my_id] + bot_ids
    
    # 4. 创建对局
    print(f"Creating game with players: {player_ids}")
    payload = {"player_ids": player_ids}
    response = requests.post(f"{API_URL}/games/", json=payload, headers=headers)
    print(f"Create Game Response Status: {response.status_code}")
    print(f"Create Game Response Body: {response.text}")

    if response.status_code == 200:
        data = response.json()
        # Handle wrapped response
        if "data" in data and "game_id" in data["data"]:
            game_id = data["data"]["game_id"]
        else:
            game_id = data.get("game_id")
            
        print(f"Game Created: {game_id}")
        
        # 5. 验证数据库事件
        print(f"Verifying events for game: {game_id}")
        events_response = requests.get(f"{API_URL}/games/{game_id}/events", headers=headers)
        print(f"Events Response Status: {events_response.status_code}")
        
        if events_response.status_code == 200:
            events_data = events_response.json()
            # Handle wrapped response
            if "data" in events_data and isinstance(events_data["data"], list):
                events = events_data["data"]
            elif isinstance(events_data, list):
                events = events_data
            else:
                events = []
                
            print(f"Events found: {len(events)}")
            if len(events) > 0:
                print("SUCCESS: Events generated in database.")
                for evt in events:
                    print(f"  - Seq {evt.get('seq')}: {evt.get('event_type')}")
            else:
                print("FAILURE: No events found for the new game.")
        else:
            print(f"FAILURE: Could not fetch events. Status: {events_response.status_code}")

    else:
        print("FAILURE: Non-200 status code")

if __name__ == "__main__":
    debug_create_game()
