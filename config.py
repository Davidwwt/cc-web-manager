"""CC Web Manager 配置文件"""
import secrets
import os

# --- 服务器配置 ---
HOST = "0.0.0.0"
PORT = 8000

# --- 安全配置 ---
# 访问 Token，首次运行自动生成并持久化到文件，重启后保持不变
# 环境变量优先: export CC_TOKEN=your_token
def _load_or_create_token() -> str:
    """从文件加载 Token，不存在则生成并保存，保证重启后 Token 不变"""
    token_file = os.path.join(os.path.dirname(__file__), "data", ".access_token")
    if os.path.exists(token_file):
        token = open(token_file).read().strip()
        if token:
            return token
    token = secrets.token_urlsafe(16)
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    open(token_file, "w").write(token)
    return token

ACCESS_TOKEN = os.environ.get("CC_TOKEN") or _load_or_create_token()

# --- Claude Code 配置 ---
# 并行 worktree 数量（Phase 1 先用 1，稳定后再增加）
MAX_WORKERS = int(os.environ.get("CC_MAX_WORKERS", "1"))

# Claude Code 单次任务超时时间（秒）
TASK_TIMEOUT = int(os.environ.get("CC_TASK_TIMEOUT", "600"))  # 10 分钟

# 是否启用 Plan Mode（先出方案再执行）
PLAN_MODE_ENABLED = os.environ.get("CC_PLAN_MODE", "true").lower() == "true"

# --- 项目配置 ---
# Claude Code 工作的目标项目路径
# 你需要把这个改成你实际要开发的项目路径
PROJECT_DIR = os.environ.get("CC_PROJECT_DIR", os.path.expanduser("~/my-project"))

# --- 数据库配置 ---
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tasks.db")

# --- 日志配置 ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# --- 备份配置 ---
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
BACKUP_INTERVAL_HOURS = 1
