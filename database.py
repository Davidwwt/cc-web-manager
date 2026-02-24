"""CC Web Manager — 数据库操作模块"""
import os
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict, Any

from config import DB_PATH


async def _add_column_if_not_exists(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    """如果列不存在则添加（SQLite 不支持 IF NOT EXISTS，需要手动检查）"""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
        existing = [row[1] for row in rows]
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
    """初始化数据库，创建/迁移表结构"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # --- projects 表 ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                path        TEXT NOT NULL,
                git_remote  TEXT,
                auto_push   BOOLEAN DEFAULT 1,
                max_workers INTEGER DEFAULT 1,
                status      TEXT NOT NULL DEFAULT 'active',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- tasks 表 ---
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

        # 迁移：为已存在的 tasks 表添加 project_id 列
        await _add_column_if_not_exists(db, "tasks", "project_id", "INTEGER REFERENCES projects(id)")

        await db.commit()


async def ensure_default_project() -> int:
    """
    如果没有任何项目，自动创建默认项目指向 ~/my-project。
    返回默认项目的 ID。
    """
    from config import PROJECT_DIR
    projects = await get_projects()
    if projects:
        return projects[0]["id"]

    default_path = os.path.expanduser("~/my-project")
    project_id = await create_project(
        name="默认项目",
        description="自动创建的默认项目",
        path=default_path,
        git_remote=None,
        auto_push=True,
        max_workers=1,
    )
    return project_id


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


async def create_project(
    name: str,
    description: str,
    path: str,
    git_remote: Optional[str] = None,
    auto_push: bool = True,
    max_workers: int = 1,
) -> int:
    """创建新项目，返回项目 ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO projects (name, description, path, git_remote, auto_push, max_workers, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
            (name, description, path, git_remote, int(auto_push), max_workers, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    """获取单个项目"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_dict(row) if row else None


async def get_projects(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """获取项目列表，可按状态筛选"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            async with db.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY created_at ASC",
                (status,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM projects ORDER BY created_at ASC"
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def update_project(project_id: int, **fields) -> None:
    """通用项目字段更新"""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [project_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?", values
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


async def create_task(prompt: str, project_id: Optional[int] = None) -> int:
    """创建新任务，返回任务 ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (prompt, status, project_id, created_at) VALUES (?, 'pending', ?, ?)",
            (prompt, project_id, datetime.utcnow().isoformat()),
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


async def get_tasks(
    status: Optional[str] = None,
    project_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """获取任务列表，可按状态和项目筛选，按创建时间倒序"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with db.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC",
            params,
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


async def get_next_pending_task(project_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """取队列中最早的 pending 任务，可限定项目"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if project_id is not None:
            async with db.execute(
                "SELECT * FROM tasks WHERE status = 'pending' AND project_id = ? ORDER BY created_at ASC LIMIT 1",
                (project_id,),
            ) as cur:
                row = await cur.fetchone()
        else:
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
