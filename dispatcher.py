"""CC Web Manager — Claude Code 任务调度器（Ralph Loop）"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set

import database
from config import LOG_DIR, PROJECT_DIR, TASK_TIMEOUT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WebSocket 订阅者管理
# ---------------------------------------------------------------------------

# task_id -> 回调集合（实时日志订阅）
_log_subscribers: Dict[int, Set[Callable]] = {}
# 全局事件订阅（任务状态变更）
_event_subscribers: Set[Callable] = set()


def subscribe_log(task_id: int, callback: Callable) -> None:
    _log_subscribers.setdefault(task_id, set()).add(callback)


def unsubscribe_log(task_id: int, callback: Callable) -> None:
    _log_subscribers.get(task_id, set()).discard(callback)


def subscribe_events(callback: Callable) -> None:
    _event_subscribers.add(callback)


def unsubscribe_events(callback: Callable) -> None:
    _event_subscribers.discard(callback)


async def _broadcast_log(task_id: int, text: str) -> None:
    """向该任务的所有日志订阅者广播文本"""
    dead: Set[Callable] = set()
    for cb in list(_log_subscribers.get(task_id, set())):
        try:
            await cb(text)
        except Exception:
            dead.add(cb)
    _log_subscribers.get(task_id, set()).difference_update(dead)


async def _broadcast_event(event: Dict[str, Any]) -> None:
    """向所有全局事件订阅者广播事件"""
    dead: Set[Callable] = set()
    for cb in list(_event_subscribers):
        try:
            await cb(event)
        except Exception:
            dead.add(cb)
    _event_subscribers.difference_update(dead)


# ---------------------------------------------------------------------------
# 日志解析：从 stream-json 提取可读文本
# ---------------------------------------------------------------------------

def _extract_display_text(raw_line: str) -> str:
    """
    尝试从 stream-json 行中提取人类可读的文本。
    无法解析时原样返回。
    """
    line = raw_line.strip()
    if not line:
        return raw_line
    try:
        data = json.loads(line)
        event_type = data.get("type", "")

        # assistant 消息
        if event_type == "assistant":
            msg = data.get("message", {})
            parts = []
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                    parts.append(f"[{name}] {inp}")
            if parts:
                return "\n".join(parts) + "\n"

        # tool 结果
        if event_type == "tool_result":
            content = data.get("content", "")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                content = "\n".join(texts)
            return f"[tool_result] {content}\n" if content else raw_line

        # 系统消息 / 统计信息
        if event_type in ("system", "result"):
            return raw_line

    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return raw_line


# ---------------------------------------------------------------------------
# Git push 辅助
# ---------------------------------------------------------------------------

async def _git_push(project_dir: str, task_id: int) -> None:
    """
    在指定项目目录执行 git push origin。
    失败只记日志，不影响任务状态。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "push", "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            logger.info(f"任务 {task_id}: git push origin 成功")
        else:
            logger.warning(
                f"任务 {task_id}: git push origin 失败 (exit={proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )
    except asyncio.TimeoutError:
        logger.warning(f"任务 {task_id}: git push origin 超时（60s）")
    except Exception as exc:
        logger.warning(f"任务 {task_id}: git push origin 异常: {exc}")


# ---------------------------------------------------------------------------
# 核心：执行单个任务
# ---------------------------------------------------------------------------

async def execute_task(task: Dict[str, Any]) -> None:
    task_id: int = task["id"]
    prompt: str = task["prompt"]
    project_id: Optional[int] = task.get("project_id")

    # 确定工作目录：有 project_id 则从数据库取项目路径，否则用全局默认
    project_dir = PROJECT_DIR
    auto_push = False
    if project_id is not None:
        project = await database.get_project(project_id)
        if project:
            project_dir = project["path"]
            auto_push = bool(project.get("auto_push", False))

    logger.info(f"开始执行任务 {task_id} (project_id={project_id}, cwd={project_dir}): {prompt[:80]}")

    # 更新状态
    await database.update_task_status(
        task_id, "executing",
        started_at=datetime.utcnow().isoformat(),
        log="",
    )
    await _broadcast_event({
        "type": "task_status",
        "task_id": task_id,
        "status": "executing",
    })

    # 准备日志文件
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file_path = os.path.join(LOG_DIR, f"task_{task_id}.log")

    cmd = [
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]

    full_log = ""
    line_count = 0
    # 从 stream-json 的 result 行解析出的成功/失败信息
    claude_result: Dict[str, Any] = {}

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_dir,
            limit=10 * 1024 * 1024,  # 10MB — 防止大行触发 StreamReader 默认 64KB 限制
        )

        async def _read_stream():
            nonlocal full_log, line_count, claude_result
            with open(log_file_path, "w", encoding="utf-8") as lf:
                assert process.stdout is not None
                async for raw_bytes in process.stdout:
                    raw_line = raw_bytes.decode("utf-8", errors="replace")
                    full_log += raw_line
                    lf.write(raw_line)
                    lf.flush()

                    # 捕获 result 行，用于后续判断成功/失败
                    try:
                        parsed = json.loads(raw_line)
                        if parsed.get("type") == "result":
                            claude_result = parsed
                    except (json.JSONDecodeError, TypeError):
                        pass

                    display = _extract_display_text(raw_line)
                    await _broadcast_log(task_id, display)

                    line_count += 1
                    # 每 20 行持久化一次日志（减少写入频率）
                    if line_count % 20 == 0:
                        await database.update_task(task_id, log=full_log)

        try:
            await asyncio.wait_for(_read_stream(), timeout=TASK_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            error_msg = f"任务超时（{TASK_TIMEOUT} 秒）"
            logger.warning(f"任务 {task_id} 超时")
            await database.update_task_status(
                task_id, "failed",
                log=full_log,
                error=error_msg,
                completed_at=datetime.utcnow().isoformat(),
            )
            await _broadcast_event({
                "type": "task_status",
                "task_id": task_id,
                "status": "failed",
                "error": error_msg,
            })
            return

        await process.wait()

        # 优先用 claude 输出的 result.is_error 判断，忽略进程退出码
        # （claude CLI 有时即使任务成功也会返回非 0 退出码）
        is_claude_error = claude_result.get("is_error", True) if claude_result else (process.returncode != 0)

        if not is_claude_error:
            logger.info(f"任务 {task_id} 完成")
            await database.update_task_status(
                task_id, "completed",
                log=full_log,
                completed_at=datetime.utcnow().isoformat(),
            )
            await _broadcast_event({
                "type": "task_status",
                "task_id": task_id,
                "status": "completed",
            })
            # 任务成功后，检查是否需要自动 push
            if auto_push:
                logger.info(f"任务 {task_id}: auto_push=True，执行 git push origin...")
                await _git_push(project_dir, task_id)
        else:
            # 优先取 claude result 里的错误信息，否则用退出码
            if claude_result:
                error_msg = claude_result.get("result") or f"claude 报告失败（is_error=true）"
            else:
                error_msg = f"进程退出码: {process.returncode}（未收到 result 行）"
            logger.error(f"任务 {task_id} 失败: {error_msg[:120]}")
            await database.update_task_status(
                task_id, "failed",
                log=full_log,
                error=error_msg,
                completed_at=datetime.utcnow().isoformat(),
            )
            await _broadcast_event({
                "type": "task_status",
                "task_id": task_id,
                "status": "failed",
                "error": error_msg,
            })

    except FileNotFoundError:
        error_msg = "找不到 claude 命令，请确认 Claude Code 已安装并在 PATH 中"
        logger.error(error_msg)
        await database.update_task_status(
            task_id, "failed",
            error=error_msg,
            completed_at=datetime.utcnow().isoformat(),
        )
        await _broadcast_event({
            "type": "task_status",
            "task_id": task_id,
            "status": "failed",
            "error": error_msg,
        })
    except Exception as exc:
        logger.exception(f"执行任务 {task_id} 时发生未预期错误")
        await database.update_task_status(
            task_id, "failed",
            log=full_log,
            error=str(exc),
            completed_at=datetime.utcnow().isoformat(),
        )
        await _broadcast_event({
            "type": "task_status",
            "task_id": task_id,
            "status": "failed",
            "error": str(exc),
        })


# ---------------------------------------------------------------------------
# Ralph Loop — 主调度循环
# ---------------------------------------------------------------------------

_running = False


async def ralph_loop() -> None:
    """持续轮询并调度 pending 任务的主循环"""
    global _running
    _running = True
    logger.info("Ralph Loop 已启动，开始轮询任务队列...")

    while _running:
        try:
            task = await database.get_next_pending_task()
            if task:
                await execute_task(task)
            else:
                await asyncio.sleep(5)
        except Exception:
            logger.exception("Ralph Loop 主循环出错，5 秒后重试")
            await asyncio.sleep(5)


def stop_loop() -> None:
    global _running
    _running = False
