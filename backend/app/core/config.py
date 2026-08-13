# 这个文件是项目的全局配置管理，使用 pydantic-settings 从环境变量或 .env 文件加载配置，确保类型安全。
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os

class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "Aivalon"
    API_V1_STR: str = "/api/v1"
    
    # MySQL
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_HOST: str = "localhost"  # 默认为 localhost，避免 .env 缺失时报错
    MYSQL_PORT: int = 3306         # 默认为 3306，避免 .env 缺失时报错
    MYSQL_DATABASE: str
    
    @property
    def SQLALCHEMY_DATABASE_URL(self) -> str:
        return f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    
    # Email
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_PORT: int = 465
    MAIL_SERVER: str = "smtp.163.com"
    MAIL_STARTTLS: bool = False
    MAIL_SSL_TLS: bool = True

    # Security
    SECRET_KEY: str = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 600

    # LLM (DeepSeek)
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # RabbitMQ / Celery
    RABBITMQ_DEFAULT_USER: str = "guest"
    RABBITMQ_DEFAULT_PASS: str = "guest"
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672

    # Rate Limit（v2 改为按用户维度统计，见 app/core/rate_limit.py；压测时可用环境变量调高阈值）
    # v2 后期换成滑动窗口（app/core/sliding_window.py）：原来的固定窗口在窗口边界
    # 能挤进 2 倍——上小时最后一秒建 10 局、下小时第一秒再建 10 局，2 秒内建 20 局，
    # 而每次都"没超过 10 局/小时"。业务承诺被算法实现细节打了个对折。
    RATE_LIMIT_CREATE_GAME_TIMES: int = 10
    RATE_LIMIT_CREATE_GAME_SECONDS: int = 3600
    RATE_LIMIT_ACTION_TIMES: int = 1
    RATE_LIMIT_ACTION_SECONDS: int = 1
    # 读接口（榜单/回放/历史）：真人点不出这个频率，拦的是脚本抓取。
    # 这几个接口有缓存，打进来不贵，所以给得比写接口宽——**限流阈值该由"这个操作多贵"
    # 和"真人能多快"共同决定**，不是所有接口配一个数。
    RATE_LIMIT_READ_TIMES: int = 60
    RATE_LIMIT_READ_SECONDS: int = 60
    # 注册：比登录更该限，它会往库里写一行，且是没有身份的请求（只能按 IP 算）
    RATE_LIMIT_REGISTER_TIMES: int = 5
    RATE_LIMIT_REGISTER_SECONDS: int = 3600
    # 发验证码 / 登录。发码**每次都花真钱**（一封邮件），登录是撞库的主目标
    RATE_LIMIT_SEND_CODE_TIMES: int = 3
    RATE_LIMIT_SEND_CODE_SECONDS: int = 3600
    RATE_LIMIT_LOGIN_TIMES: int = 10
    RATE_LIMIT_LOGIN_SECONDS: int = 300

    # 网关层准入（令牌桶，见 app/core/admission.py）。和上面那组是两层不同的东西：
    # 上面按 user_id 管业务规则（"这个用户别建太多局"），这里按容量管系统总量
    # （"这台系统一共能吃多少"）——一万个用户每人只建一局完全合规，机器照样倒。
    # capacity 和 rate 是两个独立旋钮：capacity = 允许多大的突发，rate = 稳态速率。
    # 这两个值目前是拍的，等 S4 复测出真实拐点再回来定（AGENTS.md 铁律：先基线后优化）。
    RATE_LIMIT_ADMISSION_ENABLED: bool = True
    RATE_LIMIT_GLOBAL_CAPACITY: int = 400   # 全局突发上限
    RATE_LIMIT_GLOBAL_RATE: float = 200.0   # 全局稳态 QPS
    RATE_LIMIT_IP_CAPACITY: int = 60        # 单 IP 突发上限
    RATE_LIMIT_IP_RATE: float = 20.0        # 单 IP 稳态 QPS

    # 资源层限流（单房间，见 app/core/room_actor.py）。这是第三层，管的既不是系统总量
    # 也不是用户配额，而是**单个资源的持有队列**：同一房间的动作在一个 Actor 里串行，
    # 八个人对着一局猛点，每个人都没超自己的配额，队列却能排到几千个。
    # 队列长度按"一局最多几个人同时有动作"给余量（8 人局一轮最多 8 个待处理动作，
    # 给到 64 是留了 8 轮的缓冲）；超过这个数说明不是正常对局节奏。
    ROOM_QUEUE_MAX: int = 64
    # 单个动作从入队到拿到结果的上界（秒）。**排队本身也得有上界**——
    # 队列不满但每个动作都慢的时候，队尾的人一样在无限等。
    ROOM_ACTION_TIMEOUT: float = 15.0

    # 资源层·下：AI 任务队列深度（见 app/core/ai_queue.py）。
    # **这一层过载时不能拒**：AI 回合被丢掉就没有任何人会再提交它，房间的阶段
    # 永远不推进——不是变慢而是永久卡死。所以响应是降级（摘掉 LLM 走规则引擎），
    # 降的是"每个任务多贵"而不是"放几个进来"：同样长的队列，走 LLM 每条十几秒、
    # 走规则引擎每条毫秒级，**队列长度没变但排空时间差三个数量级**。
    # 阈值按"一局最多 7 个 AI"给倍数：几局同时在 AI 回合是正常的，
    # 到几十个说明 worker 已经跟不上投递速度。等 S4 出真实拐点再定。
    AI_QUEUE_DEGRADE_DEPTH: int = 40
    # 在飞任务的租约（秒）：超过它还没注销的按"漏账"清掉。
    # 取值要大于单个 AI 回合的最坏耗时（LLM 舱壁上界 45s + 提交动作的往返），
    # **小了会把正在跑的任务当成漏账清掉，于是深度永远上不去、降级永不触发**。
    AI_QUEUE_LEASE: float = 120.0

    # 5 级降级矩阵（见 app/core/degrade.py）。档位本身存 Redis 运行时可切，
    # 这里只放各级生效后用到的参数。
    # L3：冷路径降频的倍数。热榜归并与回放缓存都按这个倍数拉长间隔——
    # **降频不是关掉**：榜单晚 15 秒更新没人投诉，查不到榜单会被当成故障。
    DEGRADE_COLD_PATH_FACTOR: float = 3.0
    # L4：建局排队。**这是个全局配额，不是按用户的**——按用户的那个
    # （RATE_LIMIT_CREATE_GAME_*，10 局/小时）本来就在生效，而它保护不了系统容量：
    # 一万个用户每人只建 1 局完全合规，机器照样倒（同 H-3a 网关层那条）。
    # L4 要压的是"全站每秒能开出几局"，所以键是全局的一个。
    # 刻意留一个正数而不是给 0——**给 0 就等于 L5 了，那这一级白设**。
    DEGRADE_CREATE_GAME_TIMES: int = 20
    DEGRADE_CREATE_GAME_SECONDS: float = 10.0

    # 依赖熔断（见 app/core/breaker.py）。**舱壁管单次上界，熔断管"总共白等多久"**：
    # LLM 挂掉时舱壁让每个 AI 回合照旧付满 45 秒才回落，熔断学到之后一次都不等。
    # 窗口 30s、至少 8 个样本、失败过半就跳闸。
    # **`MIN_SAMPLES` 是比例判定的前提**：1 次里失败 1 次是 100%，
    # 少了它第一次网络抖动就能把 LLM 整个摘掉（同 C07 分位数的样本量前提）。
    BREAKER_LLM_WINDOW: float = 30.0
    BREAKER_LLM_MIN_SAMPLES: int = 8
    BREAKER_LLM_FAILURE_RATIO: float = 0.5
    # 冷却期：这段时间内一次都不调，然后放**一个**探针进去试。
    # 取值比 LLM 超时上界（45s）更长，是为了让"依赖真的缓过来了"有时间发生——
    # 冷却比一次调用还短的话，等于刚跳闸就又去打那个正在过载的依赖。
    BREAKER_LLM_OPEN_FOR: float = 60.0

    # 集群（房间路由，见 app/core/node_registry.py）
    # 显式指定节点身份：重启后身份不变，名下房间会漂回来；留空则按 主机名-进程号-随机后缀 自动生成
    NODE_ID: str = ""
    # 本节点的可达地址，供其他节点转发房间请求；多节点部署必须显式配置
    NODE_ADDR: str = "http://127.0.0.1:8000"
    # 跨节点转发超时：只覆盖"转发"这一跳，业务处理本身在归属节点上计时
    ROOM_FORWARD_TIMEOUT: float = 10.0

    # WS 网关身份（见 app/gateway.py）。刻意与 NODE_ID 分开：
    # 网关和业务节点各自订阅"自己 id 的专属频道"，同机部署时若共用一个 id，
    # 两个进程会抢同一个频道，且都以为自己持有该房间的连接。
    GATEWAY_ID: str = ""

    # AI
    AI_USE_LLM: bool = True  # False 时 AI 直接走规则引擎：压测专用，避免 LLM 延迟与成本污染数据
    AI_TASK_RATE_LIMIT: str = "60/m"  # AI 任务限流：v1 为保护 LLM 配额所设；压测时调高（如 100000/m）
    # LLM 舱壁：单次调用的**整体**上界（秒），超时即回落规则引擎（见 services/llm_service.py）。
    # 发言给得宽是因为它要生成一整段话；投票/提名只输出几个数字，20 秒还不出就是不正常。
    # 这两个值是可调的：事故里把它们压小，就是"宁可 AI 说套话也别让玩家等"。
    AI_LLM_TIMEOUT_SPEECH: float = 45.0
    AI_LLM_TIMEOUT_ACTION: float = 20.0

    @property
    def CELERY_BROKER_URL(self) -> str:
        return f"amqp://{self.RABBITMQ_DEFAULT_USER}:{self.RABBITMQ_DEFAULT_PASS}@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}//"

    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".env"),
        extra="ignore"
    )

settings = Settings()
