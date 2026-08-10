# 压测数据准备：批量创建测试用户并签发 token，输出 bench/users.json 供 Locust 使用
#
# 两个压测专用的旁路（仅限 bench 脚本，不影响业务代码）：
#   1. 验证码：直接向 Redis 写入 verification_code，跳过邮件发送（注册接口的正常校验逻辑不变）
#   2. 签发 token：登录接口有 5次/分/IP 的限流，批量登录无意义；直接用 create_access_token 本地签发
#
# 用法：cd backend && source venv/bin/activate && python ../bench/prepare_users.py --count 50
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import redis as redis_sync
import requests

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.models.user import User

BASE_URL = "http://localhost:8000"


def main():
    parser = argparse.ArgumentParser(description="准备压测用户")
    parser.add_argument("--count", type=int, default=50, help="用户数量")
    args = parser.parse_args()

    out_path = Path(__file__).parent / "users.json"
    r = redis_sync.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD, decode_responses=True,
    )
    db = SessionLocal()
    users = []
    try:
        for i in range(args.count):
            username = f"bench_user_{i:04d}"
            email = f"bench_{i:04d}@aivalon-bench.com"

            # 幂等：已存在则直接查库取 id（脚本可重复运行）
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                r.set(f"verification_code:{email}", "123456", ex=300)
                resp = requests.post(
                    f"{BASE_URL}/api/v1/auth/register",
                    json={
                        "username": username,
                        "email": email,
                        "password": "BenchPass123",
                        "verification_code": "123456",
                    },
                    timeout=10,
                )
                body = resp.json()
                if resp.status_code != 200 or body.get("code") != 0:
                    print(f"[{i}] 注册失败: {body}")
                    continue
                uid = body["data"]["id"]
            else:
                uid = user.id

            users.append({
                "user_id": uid,
                "username": username,
                "access_token": create_access_token(subject=uid),
            })
    finally:
        db.close()

    out_path.write_text(json.dumps(users, indent=2))
    print(f"完成：{len(users)} 个用户写入 {out_path}")


if __name__ == "__main__":
    main()
