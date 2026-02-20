# PROGRESS.md — 经验教训日志

> 本文件由 Claude Code 在每次任务完成后自动更新。
> 记录遇到的问题、解决方案和注意事项，避免重复踩坑。

---

## 项目初始化

- 项目创建日期: 2026-02-20
- 当前阶段: Phase 1 - MVP（单实例任务管理）

---

## 经验教训

### 2026-02-20 — MVP 首次实现

**完成内容**
- `database.py`: aiosqlite 异步 CRUD，支持 backup
- `dispatcher.py`: Ralph Loop + WebSocket 广播 + stream-json 解析
- `server.py`: FastAPI REST API + WebSocket（lifespan 模式）
- `static/index.html`: 单文件 PWA，深色主题，四 Tab 布局
- `worktree_manager.py`: Git Worktree 基础管理

**注意事项**
1. FastAPI 0.115+ 应使用 `lifespan` 而非 `on_event`（后者已弃用）
2. `claude -p` 的 `--output-format stream-json` 输出每行一个 JSON 对象，
   需要用 `_extract_display_text()` 提取可读文本
3. Token 认证通过 `?token=xxx` URL 参数，前端存储在 `localStorage`
4. `aiosqlite.backup()` 需要两个异步上下文同时打开，不能串行
5. WebSocket 订阅用 callback set + dead-set 清理，避免已断开连接泄漏
6. `PROJECT_DIR` 默认指向 `~/my-project`，使用前需通过 `CC_PROJECT_DIR` 环境变量配置实际项目路径

**下次应注意**
- 多实例并行时需要在 dispatcher 中引入 worker slot 管理
- Plan Mode 需要两阶段 claude 调用：第一次出计划，用户确认后第二次执行
- 文件浏览器目前限制 100KB，超大文件需要提示用户 SSH 查看
