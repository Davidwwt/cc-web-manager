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
# task_id -> 回调集合（对话流式订阅）
_chat_subscribers: Dict[int, Set[Callable]] = {}
# task_id -> 当前对话的流式缓冲（供晚连接的 WS 重放）
_chat_buffers: Dict[int, list] = {}


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


def subscribe_chat(task_id: int, callback: Callable) -> None:
    _chat_subscribers.setdefault(task_id, set()).add(callback)


def unsubscribe_chat(task_id: int, callback: Callable) -> None:
    _chat_subscribers.get(task_id, set()).discard(callback)


def get_chat_buffer(task_id: int) -> list:
    return list(_chat_buffers.get(task_id, []))


async def _broadcast_chat(task_id: int, data: Dict[str, Any]) -> None:
    """向该任务的对话订阅者广播，并维护缓冲供晚连接的 WS 重放"""
    buf = _chat_buffers.setdefault(task_id, [])
    buf.append(data)

    dead: Set[Callable] = set()
    for cb in list(_chat_subscribers.get(task_id, set())):
        try:
            await cb(data)
        except Exception:
            dead.add(cb)
    _chat_subscribers.get(task_id, set()).difference_update(dead)

    if data.get("type") == "chat_done":
        _chat_buffers.pop(task_id, None)


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

def _tool_brief(name: str, inp: dict) -> str:
    """将工具调用转换为简短的中文描述"""
    name_l = name.lower()
    path = inp.get("path") or inp.get("file_path") or inp.get("notebook_path") or ""
    cmd = inp.get("command", "")

    if "write" in name_l or "create" in name_l:
        return f"  ✍ 写入: {path}"
    if "read" in name_l:
        return f"  📖 读取: {path}"
    if "edit" in name_l or "replace" in name_l:
        return f"  ✏ 编辑: {path}"
    if "bash" in name_l or "execute" in name_l:
        return f"  $ {cmd[:100]}"
    if "glob" in name_l:
        return f"  🔍 查找: {inp.get('pattern', '')}"
    if "grep" in name_l:
        return f"  🔍 搜索: {inp.get('pattern', '')}"
    if "task" in name_l:
        return f"  🤖 子任务"
    if "todo" in name_l:
        return f"  📋 任务列表"
    return f"  [{name}]"


def _extract_display_text(raw_line: str) -> str:
    """
    从 stream-json 行中提取人类可读的简洁文本。
    只显示 assistant 的文字和工具调用摘要，跳过原始 JSON。
    """
    line = raw_line.strip()
    if not line:
        return ""
    try:
        data = json.loads(line)
        event_type = data.get("type", "")

        # assistant 消息：显示文字 + 简洁工具摘要
        if event_type == "assistant":
            msg = data.get("message", {})
            parts = []
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block["text"].strip()
                    if text:
                        parts.append(text)
                elif block.get("type") == "tool_use":
                    parts.append(_tool_brief(block.get("name", "tool"), block.get("input", {})))
            return "\n".join(parts) + "\n" if parts else ""

        # tool_result：跳过（太冗长）
        if event_type == "tool_result":
            return ""

        # result：显示完成/失败简报
        if event_type == "result":
            is_error = data.get("is_error", False)
            result_text = data.get("result", "")
            if is_error and result_text:
                return f"\n[失败] {result_text}\n"
            return "\n[完成]\n" if not is_error else ""

        # system、其他：静默跳过
        return ""

    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return ""  # 无法解析的原始 JSON 行不展示


# ---------------------------------------------------------------------------
# PROGRESS.md 经验注入
# ---------------------------------------------------------------------------

# cc-web-manager 自身目录（PROGRESS.md 所在位置）
_MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))

# 注入的最大字符数，避免占用太多上下文
_PROGRESS_MAX_CHARS = 4000


async def _build_prompt_with_progress(prompt: str) -> str:
    """
    读取 PROGRESS.md 并将历史经验注入到 prompt 前缀。
    若文件不存在或内容为空则直接返回原始 prompt。
    """
    progress_path = os.path.join(_MANAGER_DIR, "PROGRESS.md")
    try:
        loop = asyncio.get_event_loop()
        content = await loop.run_in_executor(
            None, lambda: open(progress_path, encoding="utf-8").read()
        )
        content = content.strip()
        if not content:
            return prompt
        # 只取最近的部分，避免 context 过长
        if len(content) > _PROGRESS_MAX_CHARS:
            content = "...\n" + content[-_PROGRESS_MAX_CHARS:]
        return (
            f"# 项目历史经验（来自 PROGRESS.md）\n\n"
            f"{content}\n\n"
            f"---\n\n"
            f"# 当前任务\n\n"
            f"{prompt}"
        )
    except FileNotFoundError:
        return prompt
    except Exception as exc:
        logger.warning(f"读取 PROGRESS.md 失败，跳过注入: {exc}")
        return prompt


# ---------------------------------------------------------------------------
# PROGRESS.md 自动更新
# ---------------------------------------------------------------------------

async def _update_progress_md(task_id: int, prompt: str, status: str, result_summary: str) -> None:
    """任务完成/失败后，让 Claude Code 总结经验教训并追加到 PROGRESS.md"""
    progress_path = os.path.join(_MANAGER_DIR, "PROGRESS.md")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    status_cn = "成功" if status == "completed" else "失败"

    update_prompt = (
        f"请阅读并总结刚才执行的任务经验，将其追加到 {progress_path} 文件末尾。\n\n"
        f"任务描述：{prompt[:300]}\n"
        f"执行状态：{status_cn}\n"
        f"结果摘要：{result_summary[:600] if result_summary else '（无）'}\n\n"
        f"请用以下 Markdown 格式追加（只追加，不要修改已有内容）：\n\n"
        f"### {today} — [任务简短标题]\n\n"
        f"**任务**\n- [一句话描述]\n\n"
        f"**结果**\n- 状态: {status_cn}\n- [主要完成或失败内容]\n\n"
        f"**注意事项**\n- [遇到的问题或值得注意的点]\n- [下次应该注意什么]\n\n"
        f"---\n"
    )

    cmd = [
        "claude", "-p", update_prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=_MANAGER_DIR,
            limit=10 * 1024 * 1024,
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.warning(f"任务 {task_id}: 更新 PROGRESS.md 超时（120s）")
            return
        if process.returncode == 0:
            logger.info(f"任务 {task_id}: PROGRESS.md 已更新")
        else:
            logger.warning(f"任务 {task_id}: 更新 PROGRESS.md 失败 (exit={process.returncode})")
    except Exception as exc:
        logger.warning(f"任务 {task_id}: 更新 PROGRESS.md 异常: {exc}")


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

    # 将 PROGRESS.md 的历史经验注入到 prompt 前缀
    enriched_prompt = await _build_prompt_with_progress(prompt)

    cmd = [
        "claude", "-p", enriched_prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]

    full_log = ""
    line_count = 0
    # 从 stream-json 的 result 行解析出的成功/失败信息
    claude_result: Dict[str, Any] = {}
    # 累积可读展示文本，供任务卡片对话区回显
    display_parts: list[str] = []

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
                    if display:
                        display_parts.append(display)
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
            # 保存最终结果作为第一条助手消息，供对话界面展示
            # 优先用 result 字段，否则回退到执行过程中的累积可读文本
            final_result_text = claude_result.get("result", "") if claude_result else ""
            if not final_result_text:
                final_result_text = "".join(display_parts).strip()
            if final_result_text:
                await database.create_message(task_id, "assistant", final_result_text)
            await _broadcast_event({
                "type": "task_status",
                "task_id": task_id,
                "status": "completed",
            })
            # 任务成功后，检查是否需要自动 push
            if auto_push:
                logger.info(f"任务 {task_id}: auto_push=True，执行 git push origin...")
                await _git_push(project_dir, task_id)
            # 后台更新 PROGRESS.md（不阻塞主流程）
            asyncio.create_task(
                _update_progress_md(task_id, prompt, "completed", final_result_text)
            )
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
            # 保存最终结果作为第一条助手消息，供对话界面展示
            if error_msg:
                await database.create_message(task_id, "assistant", error_msg)
            await _broadcast_event({
                "type": "task_status",
                "task_id": task_id,
                "status": "failed",
                "error": error_msg,
            })
            # 后台更新 PROGRESS.md（不阻塞主流程）
            asyncio.create_task(
                _update_progress_md(task_id, prompt, "failed", error_msg)
            )

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
# 对话功能：对已完成任务继续提问
# ---------------------------------------------------------------------------

CHAT_TIMEOUT = 300  # 5 分钟


async def execute_chat(
    task_id: int,
    user_message: str,
    prior_messages: list,
    project_dir: str,
) -> str:
    """
    对已完成任务执行一轮对话，流式广播响应，返回完整响应文本。
    prior_messages: 当前消息之前的历史 [{role, content}, ...]
    """
    # 构建带上下文的 prompt
    if prior_messages:
        history_lines = []
        for m in prior_messages:
            role_label = "用户" if m["role"] == "user" else "Claude"
            content = m["content"]
            if m["role"] == "assistant" and len(content) > 600:
                content = content[:600] + "…（已截断）"
            history_lines.append(f"{role_label}：{content}")
        history = "\n\n".join(history_lines)
        prompt = f"以下是关于本项目的对话历史：\n\n{history}\n\n用户新问题：{user_message}"
    else:
        prompt = user_message

    cmd = [
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]

    response_parts: list[str] = []

    async def _stream():
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_dir,
            limit=10 * 1024 * 1024,
        )
        assert process.stdout is not None
        async for raw_bytes in process.stdout:
            raw_line = raw_bytes.decode("utf-8", errors="replace")
            display = _extract_display_text(raw_line)
            if display:
                response_parts.append(display)
                await _broadcast_chat(task_id, {"type": "chat_chunk", "text": display})
        await process.wait()

    try:
        await asyncio.wait_for(_stream(), timeout=CHAT_TIMEOUT)
    except asyncio.TimeoutError:
        err = "\n[对话超时，请重试]\n"
        response_parts.append(err)
        await _broadcast_chat(task_id, {"type": "chat_chunk", "text": err})
    except FileNotFoundError:
        err = "[找不到 claude 命令]"
        response_parts.append(err)
        await _broadcast_chat(task_id, {"type": "chat_chunk", "text": err})
    except Exception as exc:
        err = f"[错误: {exc}]"
        response_parts.append(err)
        await _broadcast_chat(task_id, {"type": "chat_chunk", "text": err})
    finally:
        await _broadcast_chat(task_id, {"type": "chat_done"})

    return "".join(response_parts).strip()


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
