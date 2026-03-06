import sys
import os
import time

# Ensure app is in python path
sys.path.append(os.getcwd())

from app.core.celery_app import celery_app
from app.tasks.monitor import monitor_dead_letter_queue
from kombu import Connection, Exchange, Queue
from app.core.config import settings
import logging

# 配置日志输出到控制台
logging.basicConfig(level=logging.INFO)

def main():
    print("--- Verifying Dead Letter Queue Monitor ---")
    
    # 1. 模拟死信
    # 直接向 dead_letter_queue 发送一条消息
    print("1. Injecting a message into 'dead_letter_queue'...")
    try:
        with Connection(settings.CELERY_BROKER_URL) as conn:
            channel = conn.channel()
            # 使用 passive=True 避免参数冲突
            channel.queue_declare(queue="dead_letter_queue", passive=True)
            
            # 发送消息
            producer = conn.Producer(channel)
            producer.publish(
                body={"error": "Test Dead Letter"},
                exchange="dlx", # 发送到 DLX Exchange
                routing_key="dead_letter",
                retry=True
            )
            print("   Message injected.")
    except Exception as e:
        print(f"   Failed to inject message: {e}")
        # 如果队列不存在，可能是 Celery 还没启动过，尝试创建
        if "NOT_FOUND" in str(e):
             print("   Queue not found. Please start Celery Worker first to initialize queues.")
        return

    # 2. 触发监控任务
    print("2. Triggering monitor task (synchronously)...")
    try:
        # 同步执行任务以查看输出
        monitor_dead_letter_queue.apply()
        print("   Monitor task executed.")
    except Exception as e:
        print(f"   Monitor task failed: {e}")
    
    # 3. 观察日志
    print("3. Please check the console output above for '[Monitor] ALERT' logs in RED.")

if __name__ == "__main__":
    main()