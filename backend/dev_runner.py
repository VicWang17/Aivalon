import subprocess
import sys
import os
import signal
import time
import threading

# 颜色代码
GREEN = '\033[92m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
RED = '\033[91m'
RESET = '\033[0m'

processes = []

def log(service, message, color=RESET):
    print(f"{color}[{service}] {message}{RESET}")

def run_process(command, name, color):
    """
    运行子进程并实时打印输出
    """
    try:
        log(name, f"Starting: {' '.join(command)}", color)
        
        # 使用 Popen 启动进程
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1, # 行缓冲
            env=os.environ.copy() # 继承环境变量
        )
        processes.append(process)

        # 实时读取输出
        for line in process.stdout:
            print(f"{color}[{name}] {line.strip()}{RESET}")
            
        process.wait()
        if process.returncode != 0 and process.returncode != -signal.SIGTERM:
            log(name, f"Exited with code {process.returncode}", RED)
            
    except Exception as e:
        log(name, f"Error: {e}", RED)

def cleanup(signum, frame):
    """
    优雅退出：收到 Ctrl+C 时杀死所有子进程
    """
    log("Runner", "Stopping all services...", YELLOW)
    for p in processes:
        if p.poll() is None: # 如果进程还在运行
            try:
                # 发送 SIGTERM 信号
                os.kill(p.pid, signal.SIGTERM)
            except Exception:
                pass
    
    # 给一点时间让它们退出
    time.sleep(1)
    
    # 强制杀死仍未退出的进程
    for p in processes:
        if p.poll() is None:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except Exception:
                pass
                
    log("Runner", "All services stopped. Bye!", GREEN)
    sys.exit(0)

def main():
    # 注册信号处理
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 确保在 backend 目录下
    if not os.path.exists("app"):
        log("Runner", "Please run this script from the 'backend' directory.", RED)
        sys.exit(1)

    threads = []

    # 1. API Server
    t1 = threading.Thread(target=run_process, args=(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"],
        "API",
        GREEN
    ))
    threads.append(t1)

    # 2. Celery Worker
    t2 = threading.Thread(target=run_process, args=(
        [sys.executable, "-m", "celery", "-A", "app.core.celery_app", "worker", "--loglevel=info", "--concurrency=4"],
        "Celery",
        BLUE
    ))
    threads.append(t2)

    # 3. Outbox Relay
    t3 = threading.Thread(target=run_process, args=(
        [sys.executable, "-m", "app.core.outbox_relay"],
        "Relay",
        YELLOW
    ))
    threads.append(t3)

    log("Runner", "Starting backend services...", GREEN)
    
    # 启动所有线程
    for t in threads:
        t.start()
        # 稍微错开启动时间，避免日志混杂太乱
        time.sleep(0.5)

    # 主线程循环等待，直到收到信号
    while True:
        try:
            time.sleep(1)
            # 检查是否有进程意外退出
            for p in processes:
                if p.poll() is not None:
                    log("Runner", "A subprocess exited unexpectedly. Shutting down...", RED)
                    cleanup(None, None)
        except KeyboardInterrupt:
            cleanup(None, None)

if __name__ == "__main__":
    main()
