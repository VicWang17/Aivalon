# S4 突发流量场景：阶梯形态负载——基线流量突然放大 10 倍再回落
#
# 运行示例：
#   locust -f ../bench/locust_s4.py --headless --host http://localhost:8000
#   （用户数由 LoadTestShape 控制，命令行不需要 -u/-r）
#
# 观察点：
# - 突发瞬间（60s 处）错误率与 P99 的跳变：系统是否被瞬间打穿
# - 回落后（120s 后）指标是否恢复：是否有积压/资源泄漏导致"起不来"
from locust import LoadTestShape

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from locustfile import S1ApiUser  # 复用 S1 的只读 API 用户行为


class StepLoadShape(LoadTestShape):
    """
    阶梯负载形态（Locust 按 tick 返回值动态调整用户数以匹配形态）：
      0~60s    50 用户    基线流量
      60~120s  500 用户   10 倍突发（spawn_rate=500/s 近似瞬时打满）
      120~180s 50 用户    回落观察恢复能力
    """

    def tick(self):
        run_time = self.get_run_time()
        if run_time < 60:
            return (50, 10)
        elif run_time < 120:
            return (500, 500)
        elif run_time < 180:
            return (50, 50)
        return None
