import time
import logging
from sqlalchemy.sql import func
from app.db.base import SessionLocal
from app.models.outbox import OutboxEvent
from app.core.celery_app import celery_app, game_events_exchange

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbox_relay")

def process_outbox_events():
    """
    轮询 Outbox 表，将 pending 事件转发到 RabbitMQ (Exchange: game_events)
    """
    db = SessionLocal()
    try:
        # 1. 获取待处理事件 (一次取 50 条)
        events = db.query(OutboxEvent).filter(
            OutboxEvent.status == "pending"
        ).order_by(OutboxEvent.id).limit(50).all()

        if not events:
            return 0

        count = 0
        # 获取 Kombu Producer
        with celery_app.producer_pool.acquire(block=True) as producer:
            for event in events:
                try:
                    # 2. 构造 Routing Key (e.g. game.vote, game.game_start)
                    # event_type 可能是 "VOTE", "GAME_START"
                    routing_key = f"game.{event.event_type.lower()}"
                    
                    # 3. 发布到 Exchange
                    producer.publish(
                        event.payload,
                        exchange=game_events_exchange,
                        routing_key=routing_key,
                        declare=[game_events_exchange],
                        retry=True
                    )
                    
                    # 4. 更新状态
                    event.status = "processed"
                    event.processed_at = func.now()
                    count += 1
                    
                except Exception as e:
                    logger.error(f"Failed to relay event {event.id}: {e}")
                    event.status = "failed"
                    event.error_log = str(e)
                    event.retry_count += 1
                    # 简单重试策略：如果重试 < 3 次，重置为 pending (或者由专门的重试任务处理)
                    # 这里简化为：失败就标记 failed，避免阻塞后续事件
        
        db.commit()
        if count > 0:
            logger.info(f"Relayed {count} events.")
        return count
        
    except Exception as e:
        logger.error(f"Outbox relay error: {e}")
        db.rollback()
        return 0
    finally:
        db.close()

def start_relay_loop(interval: float = 1.0):
    """
    启动 Outbox Relay 循环 (阻塞)
    """
    logger.info("Starting Outbox Relay loop...")
    while True:
        try:
            count = process_outbox_events()
            if count == 0:
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Stopping Outbox Relay loop...")
            break
        except Exception as e:
            logger.error(f"Relay loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    start_relay_loop()
