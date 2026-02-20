#!/bin/bash
# ============================================================
# CC Web Manager — EC2 一键启动脚本
# 
# 用法：
#   1. SSH 到你的 EC2
#   2. 把整个 cc-web-manager 文件夹上传到 EC2
#   3. 运行: bash setup.sh
#   4. 脚本会安装依赖、初始化项目，然后启动 Claude Code
#   5. Claude Code 会根据 CLAUDE.md 自动开发整个系统
# ============================================================

set -e

echo "========================================="
echo "  CC Web Manager — 环境准备"
echo "========================================="

# --- 1. 系统依赖 ---
echo ""
echo "📦 安装系统依赖..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# --- 2. 检查 Node.js ---
if ! command -v node &> /dev/null; then
    echo "📦 安装 Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt install -y nodejs
fi
echo "✅ Node.js $(node --version)"

# --- 3. 检查 Claude Code ---
if ! command -v claude &> /dev/null; then
    echo "📦 安装 Claude Code..."
    npm install -g @anthropic-ai/claude-code
    echo ""
    echo "⚠️  请先运行 'claude /login' 登录你的 Claude 账号"
    echo "   登录完成后重新运行本脚本"
    exit 1
fi
echo "✅ Claude Code 已安装"

# --- 4. Python 依赖 ---
echo ""
echo "📦 安装 Python 依赖..."
cd "$(dirname "$0")"
pip install -r requirements.txt --break-system-packages 2>/dev/null || pip install -r requirements.txt

# --- 5. 创建必要目录 ---
echo ""
echo "📁 创建目录..."
mkdir -p data logs backups static

# --- 6. 初始化 Git ---
if [ ! -d ".git" ]; then
    echo "📁 初始化 Git 仓库..."
    git init
    git add -A
    git commit -m "Initial commit: project skeleton with CLAUDE.md"
fi

# --- 7. 初始化目标项目目录 ---
PROJECT_DIR="${CC_PROJECT_DIR:-$HOME/my-project}"
if [ ! -d "$PROJECT_DIR" ]; then
    echo "📁 创建目标项目目录: $PROJECT_DIR"
    mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR"
    git init
    echo "# My Project" > README.md
    git add -A
    git commit -m "Initial commit"
    cd -
fi

echo ""
echo "========================================="
echo "  ✅ 环境准备完成！"
echo "========================================="
echo ""
echo "接下来有两种方式启动开发："
echo ""
echo "方式 A: 让 Claude Code 自动开发整个系统（推荐）"
echo "  cd $(pwd)"
echo "  claude --dangerously-skip-permissions"
echo "  然后输入: 请阅读 CLAUDE.md，按照里面的架构设计，开发完整的 CC Web Manager 系统。先从 MVP 开始：实现任务提交、单实例执行、实时日志展示。"
echo ""
echo "方式 B: 让 Claude Code 非交互式地一步步开发"
echo "  claude -p '请阅读 CLAUDE.md，先实现 database.py（数据库初始化和CRUD操作）' --dangerously-skip-permissions"
echo "  claude -p '请阅读 CLAUDE.md，实现 config.py 的完善和 dispatcher.py（任务调度器）' --dangerously-skip-permissions"
echo "  claude -p '请阅读 CLAUDE.md，实现 server.py（FastAPI 后端，包含所有 API 和 WebSocket）' --dangerously-skip-permissions"
echo "  claude -p '请阅读 CLAUDE.md，实现 static/index.html（单文件 PWA 前端）' --dangerously-skip-permissions"
echo "  claude -p '请阅读 CLAUDE.md，实现 worktree_manager.py 并测试整个系统能否启动' --dangerously-skip-permissions"
echo ""
echo "========================================="
