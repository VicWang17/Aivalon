# 生成 S3 "连接数 vs 广播延迟" 劣化曲线图
# 数据来源：document/benchmark/v1_baseline.md（S3 梯度压测实测值）
# 用法：cd backend && source venv/bin/activate && python ../bench/plot_s3.py
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = ["PingFang SC", "Arial Unicode MS", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

# (连接数, P50 ms, P99 ms) —— v1 基线实测
DATA = [
    (100, 6, 12),
    (500, 22, 46),
    (1000, 46, 91),
    (2000, 84, 180),
]

conns = [d[0] for d in DATA]
p50 = [d[1] for d in DATA]
p99 = [d[2] for d in DATA]

fig, ax = plt.subplots(figsize=(8, 5))
ax.plot(conns, p50, "o-", label="P50")
ax.plot(conns, p99, "s-", label="P99")
ax.axhline(y=200, color="r", linestyle="--", alpha=0.6, label="目标线 P99=200ms")
ax.set_xlabel("单房间 WS 连接数")
ax.set_ylabel("广播端到端延迟 (ms)")
ax.set_title("Aivalon v1 基线：广播延迟随连接数劣化曲线\n(O(N) 顺序广播，广播源 5 msg/s)")
ax.legend()
ax.grid(alpha=0.3)
for x, y50, y99 in DATA:
    ax.annotate(f"{y99}ms", (x, y99), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)

out = Path(__file__).resolve().parent.parent / "document/benchmark/images/s3_broadcast_latency.png"
out.parent.mkdir(parents=True, exist_ok=True)
fig.tight_layout()
fig.savefig(out, dpi=150)
print(f"saved: {out}")
