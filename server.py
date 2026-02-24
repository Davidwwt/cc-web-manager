"""CC Web Manager — FastAPI 后端主文件"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import textwrap
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import database
import dispatcher
import worktree_manager
from config import (
    ACCESS_TOKEN,
    BACKUP_DIR,
    BACKUP_INTERVAL_HOURS,
    HOST,
    LOG_DIR,
    PORT,
    PROJECT_DIR,
)

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI 应用（lifespan 管理启动/关闭）
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    await database.init_db()
    logger.info("数据库初始化完成")
    # 确保至少有一个默认项目
    await database.ensure_default_project()
    logger.info("项目初始化完成")
    asyncio.create_task(dispatcher.ralph_loop())
    logger.info("Ralph Loop 已启动")
    asyncio.create_task(_auto_backup())
    logger.info("自动备份已启动")

    yield

    # 关闭
    dispatcher.stop_loop()
    logger.info("CC Web Manager 已关闭")


app = FastAPI(title="CC Web Manager", version="2.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Token 认证
# ---------------------------------------------------------------------------


def verify_token(token: Optional[str] = Query(None)) -> None:
    """简单 Token 校验（支持 URL 参数 ?token=xxx）"""
    if token != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    prompt: str


class TaskApprove(BaseModel):
    pass


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    path: str
    git_remote: Optional[str] = None
    auto_push: bool = True
    max_workers: int = 1


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    path: Optional[str] = None
    git_remote: Optional[str] = None
    auto_push: Optional[bool] = None
    max_workers: Optional[int] = None


# ---------------------------------------------------------------------------
# 辅助：在项目目录运行 git 命令
# ---------------------------------------------------------------------------


async def _run_git_in(project_dir: str, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=project_dir,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# ---------------------------------------------------------------------------
# REST API — 项目管理
# ---------------------------------------------------------------------------


@app.post("/api/projects", dependencies=[Depends(verify_token)])
async def create_project(body: ProjectCreate):
    """
    创建新项目。
    - 路径不存在时自动 mkdir + git init
    - 提供 git_remote 时自动 git remote add origin
    - 根据 description 生成初始 CLAUDE.md 和 PROGRESS.md
    - 首次 commit 并 push（如果有 remote）
    """
    project_path = os.path.expanduser(body.path)

    # 创建目录（如不存在）
    if not os.path.exists(project_path):
        os.makedirs(project_path, exist_ok=True)
        logger.info(f"已创建项目目录: {project_path}")

    # git init（如果还不是 git 仓库）
    git_dir = os.path.join(project_path, ".git")
    if not os.path.exists(git_dir):
        rc, _, stderr = await _run_git_in(project_path, "init")
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"git init 失败: {stderr}")
        logger.info(f"git init 成功: {project_path}")

    # git remote add origin（如果提供了 git_remote）
    if body.git_remote:
        # 先检查是否已有 origin
        rc, stdout, _ = await _run_git_in(project_path, "remote", "get-url", "origin")
        if rc != 0:
            rc, _, stderr = await _run_git_in(project_path, "remote", "add", "origin", body.git_remote)
            if rc != 0:
                logger.warning(f"git remote add origin 失败: {stderr}")
        else:
            logger.info(f"origin 已存在: {stdout.strip()}")

    # 生成初始 CLAUDE.md
    claude_md_path = os.path.join(project_path, "CLAUDE.md")
    if not os.path.exists(claude_md_path):
        claude_md_content = textwrap.dedent(f"""\
            # {body.name}

            ## 项目描述
            {body.description or '（暂无描述）'}

            ## 开发约定
            - 完成任务后自动 commit（使用 feat/fix/docs 等前缀）
            - 保持代码简洁，避免过度工程化
            - 重要变更记录到 PROGRESS.md

            ## 技术栈
            （请根据实际情况补充）
        """)
        Path(claude_md_path).write_text(claude_md_content, encoding="utf-8")

    # 生成初始 PROGRESS.md
    progress_md_path = os.path.join(project_path, "PROGRESS.md")
    if not os.path.exists(progress_md_path):
        progress_md_content = textwrap.dedent(f"""\
            # {body.name} — 进度日志

            ## {datetime.utcnow().strftime('%Y-%m-%d')} 项目初始化
            - 通过 CC Web Manager 创建项目
            - 项目路径: {project_path}
            {f'- Git Remote: {body.git_remote}' if body.git_remote else ''}
        """)
        Path(progress_md_path).write_text(progress_md_content, encoding="utf-8")

    # 首次 commit
    rc, stdout, _ = await _run_git_in(project_path, "status", "--porcelain")
    if rc == 0 and stdout.strip():
        # 设置 git 用户（如果未配置）
        await _run_git_in(project_path, "config", "user.email", "cc-web-manager@localhost")
        await _run_git_in(project_path, "config", "user.name", "CC Web Manager")

        await _run_git_in(project_path, "add", "-A")
        rc, _, stderr = await _run_git_in(
            project_path, "commit", "-m", f"chore: 初始化项目 {body.name}"
        )
        if rc != 0:
            logger.warning(f"初始 commit 失败: {stderr}")

    # 如果有 remote，尝试 push
    if body.git_remote:
        rc, _, stderr = await _run_git_in(project_path, "push", "-u", "origin", "HEAD")
        if rc != 0:
            logger.warning(f"初始 push 失败（非致命）: {stderr.strip()}")

    # 写入数据库
    project_id = await database.create_project(
        name=body.name,
        description=body.description,
        path=project_path,
        git_remote=body.git_remote,
        auto_push=body.auto_push,
        max_workers=body.max_workers,
    )
    project = await database.get_project(project_id)
    return {"ok": True, "project": project}


@app.get("/api/projects", dependencies=[Depends(verify_token)])
async def list_projects():
    """获取所有项目（不含已归档的）"""
    projects = await database.get_projects(status="active")
    return {"projects": projects}


@app.get("/api/projects/{project_id}", dependencies=[Depends(verify_token)])
async def get_project(project_id: int):
    """获取单个项目详情"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"project": project}


@app.put("/api/projects/{project_id}", dependencies=[Depends(verify_token)])
async def update_project(project_id: int, body: ProjectUpdate):
    """更新项目配置"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"ok": True, "project": project}

    await database.update_project(project_id, **updates)

    # 如果更新了 git_remote，同步到仓库
    if "git_remote" in updates and updates["git_remote"]:
        project_path = updates.get("path") or project["path"]
        rc, _, _ = await _run_git_in(project_path, "remote", "get-url", "origin")
        if rc != 0:
            await _run_git_in(project_path, "remote", "add", "origin", updates["git_remote"])
        else:
            await _run_git_in(project_path, "remote", "set-url", "origin", updates["git_remote"])

    updated = await database.get_project(project_id)
    return {"ok": True, "project": updated}


@app.delete("/api/projects/{project_id}", dependencies=[Depends(verify_token)])
async def archive_project(project_id: int):
    """归档项目（软删除，设置 status=archived）"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    await database.update_project(project_id, status="archived")
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST API — 项目级任务（向后兼容旧全局端点）
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/tasks", dependencies=[Depends(verify_token)])
async def create_project_task(project_id: int, body: TaskCreate):
    """向指定项目提交任务"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project["status"] != "active":
        raise HTTPException(status_code=400, detail="项目已归档")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    task_id = await database.create_task(body.prompt.strip(), project_id=project_id)
    task = await database.get_task(task_id)
    return {"ok": True, "task": task}


@app.get("/api/projects/{project_id}/tasks", dependencies=[Depends(verify_token)])
async def list_project_tasks(project_id: int, status: Optional[str] = Query(None)):
    """获取指定项目的任务列表"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    tasks = await database.get_tasks(status=status, project_id=project_id)
    for t in tasks:
        if t.get("log") and len(t["log"]) > 2000:
            t["log"] = t["log"][-2000:]
    return {"tasks": tasks}


@app.get("/api/projects/{project_id}/files", dependencies=[Depends(verify_token)])
async def list_project_files(project_id: int, path: str = Query("")):
    """浏览指定项目的文件"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project_path = project["path"]

    base = Path(project_path).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="路径越权")
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name)):
            items.append({
                "name": entry.name,
                "path": str(entry.relative_to(base)),
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else None,
                "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限")
    return {"path": path, "items": items}


@app.get("/api/projects/{project_id}/git/log", dependencies=[Depends(verify_token)])
async def project_git_log(project_id: int, limit: int = Query(20, ge=1, le=100)):
    """查看指定项目的 Git 提交历史"""
    project = await database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project_path = project["path"]

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"--max-count={limit}",
            "--pretty=format:%H|%s|%an|%ai",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"commits": [], "error": stderr.decode()}
        commits = []
        for line in stdout.decode().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0],
                    "message": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                })
        return {"commits": commits}
    except Exception as e:
        return {"commits": [], "error": str(e)}


# ---------------------------------------------------------------------------
# REST API — 任务管理（全局，向后兼容）
# ---------------------------------------------------------------------------


@app.post("/api/tasks", dependencies=[Depends(verify_token)])
async def create_task(body: TaskCreate):
    """提交新任务（使用默认/第一个活跃项目）"""
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")

    # 使用第一个活跃项目
    projects = await database.get_projects(status="active")
    project_id = projects[0]["id"] if projects else None

    task_id = await database.create_task(body.prompt.strip(), project_id=project_id)
    task = await database.get_task(task_id)
    return {"ok": True, "task": task}


@app.get("/api/tasks", dependencies=[Depends(verify_token)])
async def list_tasks(status: Optional[str] = Query(None)):
    """获取所有任务列表（log 字段截断为最后 2000 字符）"""
    tasks = await database.get_tasks(status)
    for t in tasks:
        if t.get("log") and len(t["log"]) > 2000:
            t["log"] = t["log"][-2000:]
    return {"tasks": tasks}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(verify_token)])
async def get_task(task_id: int):
    """获取单个任务详情"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task}


@app.post("/api/tasks/{task_id}/approve", dependencies=[Depends(verify_token)])
async def approve_task(task_id: int):
    """确认 Plan，将 plan_review 状态的任务推进到 pending（等待执行）"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "plan_review":
        raise HTTPException(status_code=400, detail=f"任务状态为 {task['status']}，无法确认")
    await database.update_task_status(task_id, "pending")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/reject", dependencies=[Depends(verify_token)])
async def reject_task(task_id: int):
    """拒绝 Plan，任务标记为 failed"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "plan_review":
        raise HTTPException(status_code=400, detail=f"任务状态为 {task['status']}，无法拒绝")
    await database.update_task_status(task_id, "failed", error="用户拒绝了执行计划")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(verify_token)])
async def retry_task(task_id: int):
    """重试失败的任务"""
    task = await database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] not in ("failed",):
        raise HTTPException(status_code=400, detail=f"只能重试 failed 状态的任务，当前状态: {task['status']}")
    await database.update_task_status(
        task_id, "pending",
        error=None,
        log="",
        started_at=None,
        completed_at=None,
    )
    return {"ok": True}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(verify_token)])
async def delete_task(task_id: int):
    """删除任务"""
    ok = await database.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST API — 文件浏览（全局，向后兼容，使用默认项目路径）
# ---------------------------------------------------------------------------


def _safe_path(rel: str, base_dir: str = PROJECT_DIR) -> Path:
    """解析相对路径，防止路径穿越"""
    base = Path(base_dir).resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="路径越权")
    return target


@app.get("/api/files", dependencies=[Depends(verify_token)])
async def list_files(path: str = Query("")):
    """列出项目目录下的文件树（单层）"""
    target = _safe_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name)):
            items.append({
                "name": entry.name,
                "path": str(entry.relative_to(Path(PROJECT_DIR))),
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else None,
                "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限")
    return {"path": path, "items": items}


@app.get("/api/files/{file_path:path}", dependencies=[Depends(verify_token)])
async def read_file(file_path: str):
    """读取文件内容（最多 100KB）"""
    target = _safe_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="不是文件")

    size = target.stat().st_size
    if size > 100 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 100KB，请直接 SSH 查看")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": file_path, "content": content, "size": size}


# ---------------------------------------------------------------------------
# REST API — Git（全局，向后兼容）
# ---------------------------------------------------------------------------


@app.get("/api/git/log", dependencies=[Depends(verify_token)])
async def git_log(limit: int = Query(20, ge=1, le=100)):
    """查看最近的 Git 提交记录（使用默认项目路径）"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"--max-count={limit}",
            "--pretty=format:%H|%s|%an|%ai",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_DIR,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"commits": [], "error": stderr.decode()}
        commits = []
        for line in stdout.decode().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0],
                    "message": parts[1],
                    "author": parts[2],
                    "date": parts[3],
                })
        return {"commits": commits}
    except Exception as e:
        return {"commits": [], "error": str(e)}


# ---------------------------------------------------------------------------
# REST API — 系统状态 & PROGRESS.md
# ---------------------------------------------------------------------------


@app.post("/api/restart", dependencies=[Depends(verify_token)])
async def restart_service():
    """重启 cc-manager systemd 服务"""
    try:
        subprocess.Popen(["sudo", "systemctl", "restart", "cc-manager"])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/progress", dependencies=[Depends(verify_token)])
async def get_progress():
    """读取 PROGRESS.md 内容"""
    progress_path = Path(__file__).parent / "PROGRESS.md"
    if progress_path.exists():
        return {"content": progress_path.read_text(encoding="utf-8")}
    return {"content": "PROGRESS.md 不存在"}


@app.get("/api/status", dependencies=[Depends(verify_token)])
async def get_status():
    """系统状态：队列、worktree、数据库、项目"""
    tasks = await database.get_tasks()
    counts: dict = {}
    for t in tasks:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1

    worktrees = await worktree_manager.list_worktrees()
    projects = await database.get_projects(status="active")

    return {
        "task_counts": counts,
        "total_tasks": len(tasks),
        "worktrees": worktrees,
        "project_dir": PROJECT_DIR,
        "log_dir": LOG_DIR,
        "projects": projects,
    }


# ---------------------------------------------------------------------------
# WebSocket — 任务日志实时推送
# ---------------------------------------------------------------------------


@app.websocket("/ws/tasks/{task_id}/log")
async def ws_task_log(websocket: WebSocket, task_id: int, token: Optional[str] = Query(None)):
    if token != ACCESS_TOKEN:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    # 先把已有日志一次性发送
    task = await database.get_task(task_id)
    if not task:
        await websocket.send_text(json.dumps({"error": "任务不存在"}))
        await websocket.close()
        return

    existing_log = task.get("log") or ""
    if existing_log:
        await websocket.send_text(json.dumps({"type": "history", "text": existing_log}))

    # 注册实时日志回调
    async def send_log(text: str):
        await websocket.send_text(json.dumps({"type": "log", "text": text}))

    dispatcher.subscribe_log(task_id, send_log)

    try:
        while True:
            # 保持连接，等待客户端断开
            data = await websocket.receive_text()
            # 可扩展：处理客户端发来的控制指令
    except WebSocketDisconnect:
        pass
    finally:
        dispatcher.unsubscribe_log(task_id, send_log)


# ---------------------------------------------------------------------------
# WebSocket — 全局事件推送
# ---------------------------------------------------------------------------


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, token: Optional[str] = Query(None)):
    if token != ACCESS_TOKEN:
        await websocket.close(code=4001)
        return

    await websocket.accept()

    async def send_event(event: dict):
        await websocket.send_text(json.dumps(event))

    dispatcher.subscribe_events(send_event)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        dispatcher.unsubscribe_events(send_event)


# ---------------------------------------------------------------------------
# 静态文件 & 前端入口
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>CC Web Manager</h1><p>index.html not found</p>")


# 也挂载 /static 供直接引用
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def _auto_backup():
    """每小时备份一次数据库"""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
        try:
            dest = await database.backup_db(BACKUP_DIR)
            logger.info(f"数据库备份完成: {dest}")
        except Exception:
            logger.exception("数据库备份失败")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ec2_ip = os.environ.get("EC2_IP", "your-ec2-ip")
    print("\n" + "=" * 60)
    print("CC Web Manager 启动中...")
    print(f"   地址: http://{HOST}:{PORT}")
    print(f"   访问 Token: {ACCESS_TOKEN}")
    print(f"   手机访问: http://{ec2_ip}:{PORT}?token={ACCESS_TOKEN}")
    print("=" * 60 + "\n")

    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
