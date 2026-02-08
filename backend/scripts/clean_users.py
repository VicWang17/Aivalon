import sys
import os
import argparse
from sqlalchemy.orm import Session

# 将 backend 目录添加到 sys.path，以便能导入 app 模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal
from app.models.user import User

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def list_users(db: Session):
    users = db.query(User).all()
    print(f"当前共有 {len(users)} 个用户:")
    print("-" * 50)
    print(f"{'ID':<5} | {'Username':<20} | {'Email':<30}")
    print("-" * 50)
    for user in users:
        print(f"{user.id:<5} | {user.username:<20} | {user.email:<30}")
    print("-" * 50)

def delete_user_by_email(db: Session, email: str):
    user = db.query(User).filter(User.email == email).first()
    if user:
        db.delete(user)
        db.commit()
        print(f"已删除用户: {email}")
    else:
        print(f"未找到邮箱为 {email} 的用户")

def delete_users_by_pattern(db: Session, pattern: str):
    # 使用 SQL LIKE 语法
    like_pattern = f"%{pattern}%"
    users = db.query(User).filter(User.email.like(like_pattern)).all()
    
    if not users:
        print(f"未找到邮箱包含 '{pattern}' 的用户")
        return

    count = len(users)
    print(f"找到 {count} 个匹配的用户:")
    for user in users:
        print(f" - {user.username} ({user.email})")
    
    confirm = input(f"确定要删除这 {count} 个用户吗? (y/n): ")
    if confirm.lower() == 'y':
        for user in users:
            db.delete(user)
        db.commit()
        print(f"已成功删除 {count} 个用户")
    else:
        print("操作已取消")

def delete_all_users(db: Session):
    users = db.query(User).all()
    count = len(users)
    
    if count == 0:
        print("当前没有用户")
        return

    print(f"⚠️  警告: 即将删除所有 {count} 个用户！")
    confirm = input("确定要清空所有用户吗? 此操作不可恢复！(y/n): ")
    
    if confirm.lower() == 'y':
        db.query(User).delete()
        db.commit()
        print(f"已成功删除所有用户")
    else:
        print("操作已取消")

def main():
    parser = argparse.ArgumentParser(description="用户数据清理工具")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="列出所有用户")
    group.add_argument("--email", type=str, help="删除指定邮箱的用户")
    group.add_argument("--pattern", type=str, help="删除邮箱包含指定字符串的用户")
    group.add_argument("--all", action="store_true", help="删除所有用户")
    
    args = parser.parse_args()
    
    db = SessionLocal()
    try:
        if args.list:
            list_users(db)
        elif args.email:
            delete_user_by_email(db, args.email)
        elif args.pattern:
            delete_users_by_pattern(db, args.pattern)
        elif args.all:
            delete_all_users(db)
        else:
            parser.print_help()
    finally:
        db.close()

if __name__ == "__main__":
    main()
