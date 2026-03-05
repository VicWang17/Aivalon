from app.core.celery_app import celery_app
import time

@celery_app.task
def add(x, y):
    return x + y

@celery_app.task(queue="ai_queue")
def ai_task(x, y):
    time.sleep(2)  # Simulate slow task
    return x * y

@celery_app.task(queue="stats_queue")
def stats_task(x, y):
    return x - y
