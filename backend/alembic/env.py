# 这个文件是Alembic迁移工具的运行环境配置，负责加载应用配置、建立数据库连接并执行迁移脚本，是数据库版本控制的核心入口。
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
from dotenv import load_dotenv

# 1. 加载环境变量
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

# 2. 将 backend 目录加入 sys.path，以便能导入 app 模块
sys.path.append(os.path.join(os.path.dirname(__file__), "../"))

# 3. 导入 SQLAlchemy Base (这里需要导入所有定义了 Model 的文件，目前只有 Base)
from app.db.base import Base
from app.models.user import User  # 导入 User 模型以便 Alembic 识别

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata 用于自动生成迁移脚本
target_metadata = Base.metadata

# 构建数据库 URL
MYSQL_USER = os.getenv("MYSQL_USER", "aivalon_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "aivalon_password")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "aivalon")

# 覆盖 alembic.ini 中的 sqlalchemy.url
database_url = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
