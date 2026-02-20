"""CC Web Manager — 数据库操作模块"""
import os
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import DB_PATH


async def init_db():
    """初始化数据库，创建表结构"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt      TEXT NOT NULL,
                plan        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                worktree_id INTEGER,
                log         TEXT DEFAULT '',
                git_commit  TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                started_at  DATETIME,
                completed_at DATETIME,
                error       TEXT
            )
        """)
        await db.commit()


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    return dict(row)


async def create_task(prompt: str) -> int:
    """创建新任务，返回任务 ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (prompt, status, created_at) VALUES (?, 'pending', ?)",
            (prompt, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    """获取单个任务详情"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None


async def get_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取任务列表，可按状态筛选，按创建时间倒序"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            async with db.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def update_task(task_id: int, **fields) -> None:
    """通用字段更新，支持任意字段名"""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [task_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", values
        )
        await db.commit()


async def update_task_status(task_id: int, status: str, **extra_fields) -> None:
    """更新任务状态及附加字段"""
    await update_task(task_id, status=status, **extra_fields)


async def append_task_log(task_id: int, text: str) -> None:
    """原子追加日志内容（避免整行覆盖竞态）"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET log = COALESCE(log, '') || ? WHERE id = ?",
            (text, task_id),
        )
        await db.commit()


async def get_next_pending_task() -> Optional[Dict[str, Any]]:
    """取队列中最早的 pending 任务"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None


async def delete_task(task_id: int) -> bool:
    """删除任务，返回是否成功"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cur.rowcount > 0


async def backup_db(backup_dir: str) -> str:
    """将数据库备份到指定目录，返回备份文件路径"""
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"tasks_{ts}.db")
    async with aiosqlite.connect(DB_PATH) as src:
        async with aiosqlite.connect(dest) as dst:
            await src.backup(dst)
    return dest
