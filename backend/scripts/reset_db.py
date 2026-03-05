# 这个文件用于重置数据库（清空所有表并重新运行迁移），用于开发环境快速恢复干净状态。
import sys
import os
import argparse
from sqlalchemy import MetaData
from alembic import command
from alembic.config import Config

# 将 backend 目录添加到 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import engine

def reset_database():
    parser = argparse.ArgumentParser(description="重置数据库工具")
    parser.add_argument("-f", "--force", action="store_true", help="强制执行，无需确认")
    args = parser.parse_args()

    if not args.force:
        print("⚠️  警告: 此操作将删除数据库中的所有数据！")
        confirm = input("确定要继续吗? (y/n): ")
        if confirm.lower() != 'y':
            print("操作已取消")
            return

    print("\n1. 正在清除所有表...")
    # 使用反射获取数据库中当前所有的表
    meta = MetaData()
    meta.reflect(bind=engine)
    
    if not meta.tables:
        print("数据库已经是空的。")
    else:
        print(f"发现 {len(meta.tables)} 个表: {', '.join(meta.tables.keys())}")
        meta.drop_all(bind=engine)
        print("✅ 所有表已删除")

    print("\n2. 正在重新运行 Alembic 迁移...")
    try:
        # 获取 alembic.ini 的绝对路径
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        alembic_ini_path = os.path.join(base_dir, "alembic.ini")
        
        # 创建 Alembic 配置对象
        alembic_cfg = Config(alembic_ini_path)
        # 设置 script_location，因为我们在 scripts 目录下运行，可能需要指定绝对路径
        alembic_cfg.set_main_option("script_location", os.path.join(base_dir, "alembic"))
        
        command.upgrade(alembic_cfg, "head")
        print("✅ 数据库迁移完成")
        print("\n🎉 数据库已重置为干净状态！")
        
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    reset_database()
