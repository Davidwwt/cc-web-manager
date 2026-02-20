# CC Web Manager — Claude Code 任务管理系统

## 项目概述

这是一个运行在 EC2 上的 Web 应用，让用户通过手机浏览器（PWA）远程管理和调度 Claude Code 实例。
核心理念来自胡渊鸣的文章《我给 10 个 Claude Code 打工》：把 Claude Code 变成非交互式组件，通过 Web 界面派发任务、查看进度、管理代码。

## 技术栈

- **后端**: Python 3.12 + FastAPI + Uvicorn
- **前端**: 原生 HTML/CSS/JS（单文件 PWA，不用框架，保持简单）
- **数据库**: SQLite（通过 aiosqlite 异步访问）
- **实时通信**: WebSocket（FastAPI 原生支持）
- **Claude Code 调度**: Python subprocess 调用 `claude -p`
- **版本控制**: Git + Git Worktree（多实例并行）
- **语音输入**: 浏览器 Web Speech API（前端实现，零成本）

## 目录结构

```
cc-web-manager/
├── CLAUDE.md              # 本文件，项目说明书（你正在读）
├── PROGRESS.md            # 经验教训日志，每次任务完成后更新
├── server.py              # FastAPI 后端主文件
├── dispatcher.py          # Claude Code 任务调度器（Ralph Loop）
├── worktree_manager.py    # Git Worktree 管理
├── database.py            # SQLite 数据库操作
├── config.py              # 配置文件（端口、Token、并发数等）
├── static/                # 前端文件
│   └── index.html         # 单文件 PWA（包含所有 HTML/CSS/JS）
├── data/                  # 数据目录
│   └── tasks.db           # SQLite 数据库
├── logs/                  # Claude Code 执行日志
├── backups/               # 自动备份目录
└── requirements.txt       # Python 依赖
```

## 核心架构设计

### 1. 任务生命周期

```
pending → planning → plan_review → executing → completed / failed
```

- **pending**: 用户提交的新任务，等待调度
- **planning**: Claude Code 正在生成执行计划（Plan Mode）
- **plan_review**: 计划已生成，等待用户在手机上确认
- **executing**: 用户确认后，Claude Code 正在执行
- **completed**: 任务成功完成，代码已 commit
- **failed**: 任务失败，需要查看日志排查原因

### 2. Ralph Loop 调度模式

调度器是一个持续运行的后台循环：
```python
while True:
    task = get_next_pending_task()
    if task:
        execute_task(task)  # 调用 claude -p
    else:
        sleep(5)  # 没有任务时等待
```

### 3. Claude Code 调用方式

所有 Claude Code 调用必须使用非交互模式：
```bash
claude -p "任务描述" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --verbose
```

- `--dangerously-skip-permissions`: 跳过所有权限确认
- `--output-format stream-json`: 以 JSON 流格式输出，方便程序解析
- `--verbose`: 输出详细信息

### 4. Git Worktree 并行

每个并行的 Claude Code 实例运行在独立的 Git Worktree 中：
```
~/project/              ← 主仓库 (main 分支)
~/project-worker-1/     ← worktree 1 (worker-1 分支)
~/project-worker-2/     ← worktree 2 (worker-2 分支)
```

任务完成后自动 commit 到 worker 分支，然后 merge 回 main。

### 5. PROGRESS.md 自动更新

每个任务完成后，调度器会额外让 Claude Code 执行：
"总结刚才任务的经验教训，更新 PROGRESS.md。包括：遇到了什么问题、如何解决的、下次应该注意什么。"

## 数据库 Schema

### tasks 表
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PRIMARY KEY | 任务 ID |
| prompt | TEXT | 用户输入的任务描述 |
| plan | TEXT | Claude Code 生成的执行计划（Plan Mode） |
| status | TEXT | 任务状态：pending/planning/plan_review/executing/completed/failed |
| worktree_id | INTEGER | 分配的 worktree 编号 |
| log | TEXT | Claude Code 的完整输出日志 |
| git_commit | TEXT | 任务完成后的 commit hash |
| created_at | DATETIME | 创建时间 |
| started_at | DATETIME | 开始执行时间 |
| completed_at | DATETIME | 完成时间 |
| error | TEXT | 失败时的错误信息 |

## API 设计

### REST API
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/tasks | 提交新任务 |
| GET | /api/tasks | 获取任务列表（支持状态筛选） |
| GET | /api/tasks/{id} | 获取单个任务详情 |
| POST | /api/tasks/{id}/approve | 确认 Plan，开始执行 |
| POST | /api/tasks/{id}/reject | 拒绝 Plan，任务回到 pending |
| POST | /api/tasks/{id}/retry | 重试失败的任务 |
| DELETE | /api/tasks/{id} | 删除任务 |
| GET | /api/files | 浏览项目文件列表 |
| GET | /api/files/{path} | 查看文件内容 |
| GET | /api/git/log | 查看 Git 提交历史 |
| GET | /api/progress | 查看 PROGRESS.md 内容 |
| GET | /api/status | 系统状态（运行中的实例数、队列长度等） |

### WebSocket
| 路径 | 说明 |
|------|------|
| /ws/tasks/{id}/log | 实时推送某个任务的 Claude Code 输出 |
| /ws/events | 全局事件推送（任务状态变更、需要审批等） |

## 前端设计要求

### 整体原则
- **移动优先**: 所有界面为手机竖屏优化
- **单文件 PWA**: 所有 HTML/CSS/JS 放在一个 index.html 中
- **深色主题**: 类似终端风格，护眼
- **大按钮、大字体**: 手机上方便操作

### 页面结构（单页应用，底部 Tab 切换）

**Tab 1 - 任务 (Tasks)**
- 顶部：任务输入框 + 语音输入按钮 + 提交按钮
- 下方：任务列表，按状态分组显示
- 每个任务卡片显示：状态标签、任务描述摘要、创建时间
- 点击任务卡片展开详情：完整描述、Plan 内容、操作按钮（确认/拒绝/重试）

**Tab 2 - 日志 (Logs)**
- 实时显示当前执行中任务的 Claude Code 输出
- 类似终端的黑底绿字/白字风格
- 自动滚动到底部

**Tab 3 - 文件 (Files)**
- 项目文件树浏览
- 点击文件查看内容（代码高亮）
- 查看最近的 Git 提交记录

**Tab 4 - 状态 (Status)**
- 系统运行状态：活跃的 worktree 数、队列中的任务数
- PROGRESS.md 内容展示
- 配置项展示

### PWA 支持
- 添加 manifest.json 配置（内联在 HTML 中）
- 支持 "添加到主屏幕"
- 设置 viewport meta 标签
- 使用 Service Worker 缓存静态资源（可选，后续添加）

## 安全要求

- 所有 API 请求必须携带 Token（通过 URL 参数 `?token=xxx` 或 Header `Authorization: Bearer xxx`）
- Token 在 config.py 中配置，首次启动时自动生成随机 Token
- Token 显示在服务器启动日志中，方便用户复制
- 前端将 Token 保存在 localStorage 中，每次请求自动携带

## 开发规范

### Python 代码规范
- 使用 async/await 异步编程
- 类型注解 (Type hints)
- 每个模块一个文件，职责单一
- 错误处理：所有外部调用（subprocess、数据库）必须 try/except

### 前端规范
- 原生 JS，不用任何框架
- CSS 变量管理主题色
- fetch API 调用后端
- WebSocket 处理实时数据

### Git 规范
- 每个任务完成后自动 commit，message 格式: `[CC-{task_id}] {task_prompt_前50字}`
- worktree 分支合并到 main 后自动删除

## 启动方式

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务（默认端口 8000）
python server.py

# 启动后会输出：
# 🚀 CC Web Manager running at http://0.0.0.0:8000
# 🔑 Access token: xxxxxxxx
# 📱 Open on phone: http://{your-ec2-ip}:8000?token=xxxxxxxx
```

## 特别注意

1. **先做能跑的最简版本，再逐步增强**。MVP 只需要：任务提交 → 单实例执行 → 日志展示
2. **每完成一个功能就 commit 一次**，不要积累大量未提交的变更
3. **遇到问题先记录到 PROGRESS.md**，然后再解决
4. **前端不要过度设计**，手机上能用就行
5. **自动备份数据库**，每小时一次，备份到 backups/ 目录
