import sys
import os

# Ensure app is in python path
sys.path.append(os.getcwd())

from app.tasks.test_tasks import add, ai_task, stats_task

def test_celery():
    print("Sending tasks...")
    
    # Test Default Queue
    r1 = add.delay(4, 4)
    print(f"Sent add(4, 4) to default queue. Task ID: {r1.id}")
    
    # Test AI Queue
    r2 = ai_task.delay(10, 20)
    print(f"Sent ai_task(10, 20) to ai_queue. Task ID: {r2.id}")
    
    # Test Stats Queue
    r3 = stats_task.delay(100, 50)
    print(f"Sent stats_task(100, 50) to stats_queue. Task ID: {r3.id}")
    
    print("Waiting for results...")
    
    # Wait for results
    print(f"Add result: {r1.get(timeout=10)}")
    print(f"Stats result: {r3.get(timeout=10)}")
    print(f"AI result (should take ~2s): {r2.get(timeout=10)}")
    
    print("All tasks completed successfully!")

if __name__ == "__main__":
    test_celery()
