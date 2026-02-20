"""CC Web Manager — Git Worktree 管理模块

MVP 阶段：单实例运行，仅提供状态查询。
后续可扩展为多 worktree 并行执行。
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import PROJECT_DIR

logger = logging.getLogger(__name__)


async def _run_git(*args: str, cwd: Optional[str] = None) -> tuple[int, str, str]:
    """运行 git 命令，返回 (returncode, stdout, stderr)"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or PROJECT_DIR,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def list_worktrees() -> List[Dict[str, Any]]:
    """列出当前所有 worktree（包含主仓库）"""
    rc, stdout, stderr = await _run_git("worktree", "list", "--porcelain")
    if rc != 0:
        logger.warning(f"git worktree list 失败: {stderr}")
        return []

    worktrees = []
    current: Dict[str, Any] = {}

    for line in stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[len("worktree "):].strip(), "branch": None, "head": None, "bare": False}
        elif line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif line.startswith("branch "):
            branch = line[7:].strip()
            # refs/heads/main -> main
            current["branch"] = branch.replace("refs/heads/", "")
        elif line == "bare":
            current["bare"] = True

    if current:
        worktrees.append(current)

    return worktrees


async def create_worktree(worktree_id: int, base_branch: str = "main") -> str:
    """
    为 worker 创建独立 worktree 及分支。
    返回 worktree 路径。
    """
    project_dir = Path(PROJECT_DIR)
    worktree_path = str(project_dir.parent / f"{project_dir.name}-worker-{worktree_id}")
    branch_name = f"worker-{worktree_id}"

    # 如果已存在就直接返回
    if Path(worktree_path).exists():
        logger.info(f"Worktree 已存在: {worktree_path}")
        return worktree_path

    # 创建新分支 + worktree
    rc, stdout, stderr = await _run_git(
        "worktree", "add", "-b", branch_name, worktree_path, base_branch
    )
    if rc != 0:
        raise RuntimeError(f"创建 worktree 失败: {stderr}")

    logger.info(f"Worktree 创建成功: {worktree_path} (branch: {branch_name})")
    return worktree_path


async def remove_worktree(worktree_path: str) -> None:
    """删除指定 worktree（强制）"""
    rc, _, stderr = await _run_git("worktree", "remove", "--force", worktree_path)
    if rc != 0:
        logger.warning(f"删除 worktree 失败: {stderr}")
    else:
        logger.info(f"Worktree 已删除: {worktree_path}")


async def get_current_branch(cwd: Optional[str] = None) -> str:
    """获取指定目录的当前 Git 分支名"""
    rc, stdout, _ = await _run_git("branch", "--show-current", cwd=cwd)
    return stdout.strip() if rc == 0 else "unknown"


async def commit_changes(message: str, cwd: Optional[str] = None) -> Optional[str]:
    """
    在指定 worktree 中 add all + commit。
    返回 commit hash，如果没有变更则返回 None。
    """
    work_dir = cwd or PROJECT_DIR

    # 检查是否有变更
    rc, stdout, _ = await _run_git("status", "--porcelain", cwd=work_dir)
    if rc != 0 or not stdout.strip():
        logger.info("没有需要提交的变更")
        return None

    # git add -A
    rc, _, stderr = await _run_git("add", "-A", cwd=work_dir)
    if rc != 0:
        raise RuntimeError(f"git add 失败: {stderr}")

    # git commit
    rc, _, stderr = await _run_git("commit", "-m", message, cwd=work_dir)
    if rc != 0:
        raise RuntimeError(f"git commit 失败: {stderr}")

    # 获取最新 commit hash
    rc, stdout, _ = await _run_git("rev-parse", "HEAD", cwd=work_dir)
    return stdout.strip() if rc == 0 else None


async def merge_to_main(branch_name: str) -> None:
    """将 worker 分支合并回 main（在主仓库中执行）"""
    # 切换到 main
    rc, _, stderr = await _run_git("checkout", "main")
    if rc != 0:
        raise RuntimeError(f"切换到 main 失败: {stderr}")

    # merge
    rc, _, stderr = await _run_git("merge", "--no-ff", branch_name, "-m", f"Merge {branch_name} into main")
    if rc != 0:
        raise RuntimeError(f"merge 失败: {stderr}")

    logger.info(f"分支 {branch_name} 已合并到 main")


async def delete_branch(branch_name: str) -> None:
    """删除 worker 分支"""
    rc, _, stderr = await _run_git("branch", "-D", branch_name)
    if rc != 0:
        logger.warning(f"删除分支 {branch_name} 失败: {stderr}")
    else:
        logger.info(f"分支 {branch_name} 已删除")
