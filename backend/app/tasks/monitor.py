from app.core.celery_app import celery_app
from app.core.config import settings
from kombu import Connection
import logging

logger = logging.getLogger("celery.monitor")

@celery_app.task(bind=True)
def monitor_dead_letter_queue(self):
    """
    定期检查死信队列 (dead_letter_queue) 的消息积压情况。
    如果发现死信，打印 CRITICAL 日志并触发告警。
    """
    queue_name = "dead_letter_queue"
    
    try:
        # 使用 kombu 连接 RabbitMQ (复用 settings 中的 Broker URL)
        with Connection(settings.CELERY_BROKER_URL) as conn:
            # 获取 channel
            channel = conn.channel()
            
            # passive=True: 仅检查队列是否存在，不创建
            # 如果队列不存在，会抛出异常，这里假设队列已由 celery_app 初始化
            try:
                name, message_count, consumer_count = channel.queue_declare(
                    queue=queue_name, 
                    passive=True
                )
                
                if message_count > 0:
                    msg = f"[Monitor] ALERT: Dead Letter Queue '{queue_name}' has {message_count} messages! Please investigate immediately."
                    logger.critical(msg)
                    print(f"\033[91m{msg}\033[0m") # Red text in console
                    
                    # TODO: 这里可以集成邮件发送逻辑
                    # from app.utils.email import send_alert_email
                    # send_alert_email("Dead Letter Queue Alert", msg)
                    
                else:
                    logger.info(f"[Monitor] DLQ '{queue_name}' is empty. System healthy.")
                    
            except Exception as e:
                # 队列可能不存在
                logger.warning(f"[Monitor] Queue '{queue_name}' not found or error checking: {e}")

    except Exception as e:
        logger.error(f"[Monitor] Failed to connect to broker: {e}")
        # 监控任务本身失败也重试一次
        raise self.retry(exc=e, countdown=60, max_retries=1)
