from fastapi.testclient import TestClient
from app.main import app
from app.core.redis import get_redis
from app.db.base import get_db
from unittest.mock import AsyncMock, MagicMock
from app.core.security import get_password_hash
from app.models.user import User

client = TestClient(app)

# Mock Redis
async def override_get_redis():
    mock = AsyncMock()
    # Mock verification code for register if needed, but we test login here
    return mock

# Mock DB
def override_get_db():
    db = MagicMock()
    
    from datetime import datetime
    # Mock get_password_hash to avoid bcrypt issues in test env
    # user = User(id=1, username="testuser", email="test@example.com", hashed_password=get_password_hash("password123"), is_active=True)
    user = User(id=1, username="testuser", email="test@example.com", hashed_password="hashed_password_123", is_active=True, created_at=datetime.utcnow())
    
    # Setup mock
    query_mock = MagicMock()
    db.query.return_value = query_mock
    filter_mock = MagicMock()
    query_mock.filter.return_value = filter_mock
    filter_mock.first.return_value = user
    
    yield db

# Mock verify_password to match our fake hash
def override_verify_password(plain, hashed):
    return plain == "password123" and hashed == "hashed_password_123"

# Patch the function where it is USED, not just where it is defined
from app.routers import auth
auth.verify_password = override_verify_password

app.dependency_overrides[get_redis] = override_get_redis
app.dependency_overrides[get_db] = override_get_db

def test_login_and_me():
    print("Testing login...")
    # 1. Login
    response = client.post("/api/v1/auth/login", json={"username": "testuser", "password": "password123"})
    if response.status_code != 200:
        print(f"Login failed: {response.text}")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 0
    token = data["data"]["access_token"]
    assert token is not None
    print(f"Login success, token: {token[:20]}...")
    
    # 2. Access /me
    print("Testing /me endpoint...")
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        print(f"/me failed: {response.text}")
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["data"]["username"] == "testuser"
    print("Test passed! /me returned correct user.")

if __name__ == "__main__":
    test_login_and_me()
