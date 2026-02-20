# CC Web Manager — 快速上手指南

## 你需要准备什么

| 项目 | 说明 | 预估费用 |
|------|------|----------|
| AWS EC2 | Ubuntu 24.04, t3.medium (2核4G) | ~$0.04/小时 ≈ $30/月 |
| Claude 订阅 | Claude Max Plan | $100 或 $200/月 |
| 弹性 IP | 绑定到 EC2，IP 不会变 | 免费（绑定到运行中的实例时） |

## 第一步：开 EC2

1. 登录 AWS Console → EC2 → Launch Instance
2. 选择 **Ubuntu 24.04 LTS**
3. 机型选 **t3.medium**（2核4G，够用）
4. 存储给 **30GB**（默认 8GB 不够）
5. 安全组规则：
   - SSH (22) — 你的 IP
   - Custom TCP (8000) — 0.0.0.0/0（或限定你的 IP）
6. 创建密钥对，下载 .pem 文件
7. 启动后绑定弹性 IP

## 第二步：SSH 连接 EC2

```bash
# Mac 终端
ssh -i your-key.pem ubuntu@你的EC2公网IP
```

## 第三步：上传项目文件

在你的 Mac 本地终端（不是 EC2）：
```bash
# 把整个文件夹上传到 EC2
scp -i your-key.pem -r cc-web-manager ubuntu@你的EC2公网IP:~/
```

## 第四步：运行安装脚本

回到 EC2 的 SSH 终端：
```bash
cd ~/cc-web-manager
bash setup.sh
```

脚本会自动安装 Node.js、Python、Git 等依赖。

## 第五步：登录 Claude Code

```bash
claude /login
```

按提示在浏览器中完成登录。如果 EC2 没有浏览器，它会给你一个 URL，复制到你 Mac 的浏览器中打开完成认证。

## 第六步：让 Claude Code 开发系统

```bash
cd ~/cc-web-manager
claude --dangerously-skip-permissions
```

进入 Claude Code 后，输入以下 prompt：

```
请阅读 CLAUDE.md，按照里面的架构设计，开发完整的 CC Web Manager 系统。

开发顺序：
1. 先实现 database.py — 数据库初始化和 CRUD
2. 再实现 dispatcher.py — Claude Code 任务调度器（Ralph Loop）
3. 然后实现 server.py — FastAPI 后端（REST API + WebSocket）
4. 接着实现 static/index.html — 手机端 PWA 界面
5. 最后实现 worktree_manager.py — Git Worktree 管理
6. 测试整个系统能否正常启动和运行

先从 MVP 开始，确保单实例能跑通：提交任务 → 执行 → 看日志。
多实例并行、Plan Mode 等高级功能后续再加。
每完成一个模块就 git commit 一次。
```

然后等它干活就行。

## 第七步：启动服务

Claude Code 开发完成后：
```bash
python3 server.py
```

启动后会输出类似：
```
🚀 CC Web Manager running at http://0.0.0.0:8000
🔑 Access token: abc123xyz
📱 Open on phone: http://你的EC2IP:8000?token=abc123xyz
```

## 第八步：手机上使用

1. iPhone Safari 打开上面的链接
2. 点 "分享" → "添加到主屏幕"
3. 桌面上会出现一个 App 图标
4. 以后直接点图标就能打开，和 App 一样

## 日常使用流程

```
1. 打开手机上的 PWA
2. 在输入框输入（或语音说出）你的任务
   例如："给首页加一个暗黑模式切换按钮"
3. 点提交
4. 切到日志页面，实时看 Claude Code 在干什么
5. 任务完成后，切到文件页面检查代码
6. 有新想法？继续提交，Claude Code 会自动排队执行
```

## 进阶操作

### 增加并发（多实例并行）
系统稳定后，修改环境变量增加 worker 数量：
```bash
export CC_MAX_WORKERS=3
python3 server.py
```

### 用 tmux 保持后台运行
```bash
# 创建一个 tmux 会话
tmux new -s cc-manager

# 在里面启动服务
python3 server.py

# 按 Ctrl+B 然后按 D 脱离会话（服务继续运行）

# 以后重新连接
tmux attach -t cc-manager
```

### 用 systemd 开机自启（更稳定）
```bash
sudo tee /etc/systemd/system/cc-manager.service << EOF
[Unit]
Description=CC Web Manager
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/cc-web-manager
ExecStart=/usr/bin/python3 server.py
Restart=always
Environment=CC_TOKEN=你的固定token

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable cc-manager
sudo systemctl start cc-manager
```

## 常见问题

**Q: Claude Code 登录过期了怎么办？**
SSH 到 EC2，重新运行 `claude /login`

**Q: 系统崩了怎么办？**
SSH 到 EC2，重启服务：`python3 server.py`
数据库有自动备份，在 backups/ 目录

**Q: 想让 Claude Code 开发别的项目怎么办？**
修改环境变量指向新项目：
```bash
export CC_PROJECT_DIR=~/another-project
python3 server.py
```

**Q: 如何在外网（不在家里 WiFi）访问？**
EC2 本来就在公网，手机有网就能访问，不需要额外配置。
这就是用 EC2 而不是 MacBook 的最大优势。
