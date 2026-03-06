from celery import Celery
from kombu import Queue, Exchange

from app.core.config import settings

celery_app = Celery("aivalon", broker=settings.CELERY_BROKER_URL, include=["app.tasks.test_tasks", "app.tasks.stats", "app.tasks.ai"])

# 配置 Result Backend
celery_app.conf.result_backend = settings.CELERY_RESULT_BACKEND

# 定义 Exchange
default_exchange = Exchange("default", type="direct")
ai_exchange = Exchange("ai", type="direct")
stats_exchange = Exchange("stats", type="direct")
game_events_exchange = Exchange("game_events", type="topic")  # 游戏事件交换机 (Topic)
dlx_exchange = Exchange("dlx", type="direct")  # 死信交换机

# 定义 Queues
celery_app.conf.task_queues = (
    # 默认队列
    Queue("default", default_exchange, routing_key="default"),
    
    # AI 任务队列（高并发限制，慢速）
    Queue("ai_queue", ai_exchange, routing_key="ai.#", queue_arguments={
        "x-dead-letter-exchange": "dlx",
        "x-dead-letter-routing-key": "dead_letter"
    }),
    
    # 统计任务队列（快速，如更新排行榜）
    Queue("stats_queue", stats_exchange, routing_key="stats.#", queue_arguments={
        "x-dead-letter-exchange": "dlx",
        "x-dead-letter-routing-key": "dead_letter"
    }),
    
    # 游戏事件队列 (Outbox Relay 转发至此)
    Queue("game_events_queue", game_events_exchange, routing_key="game.#", queue_arguments={
        "x-dead-letter-exchange": "dlx",
        "x-dead-letter-routing-key": "dead_letter"
    }),
    
    # 死信队列（存放失败任务）
    Queue("dead_letter_queue", dlx_exchange, routing_key="dead_letter"),
)

# 定义路由规则
celery_app.conf.task_routes = {
    "app.tasks.ai.*": {"queue": "ai_queue", "routing_key": "ai.task"},
    "app.tasks.stats.*": {"queue": "stats_queue", "routing_key": "stats.task"},
    "*": {"queue": "default", "routing_key": "default"},
}

# 其他配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 任务失败重试配置
    task_acks_late=True,  # 任务执行完成后再确认，保证不丢失
    task_reject_on_worker_lost=True,  # Worker 崩溃时重新入队
    
    # 并发与背压控制
    worker_prefetch_multiplier=1,  # 防止 Worker 一次性领取过多耗时任务（公平调度）
    worker_concurrency=8,  # 默认并发数，确保至少能同时处理一个 8 人局的 AI (7 个)
)

# 自动发现任务模块
celery_app.autodiscover_tasks(["app.tasks"])
