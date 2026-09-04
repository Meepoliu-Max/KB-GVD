# KBRefiner 部署指南

> 面向**运维和部署者**，覆盖从本地试用到生产上线的全流程。
> 配置项明细见 [CONFIGURATION.md](./CONFIGURATION.md)，使用注意与踩坑见 [README.md](./README.md)。

## 目录

- [环境要求](#环境要求)
- [方式一：Docker 部署（推荐生产）](#方式一docker-部署推荐生产)
- [方式二：pip 安装 + systemd 服务](#方式二pip-安装--systemd-服务)
- [方式三：源码运行（开发用）](#方式三源码运行开发用)
- [Nginx 反向代理](#nginx-反向代理)
- [数据备份与恢复](#数据备份与恢复)
- [升级流程](#升级流程)
- [健康检查与监控](#健康检查与监控)
- [安全加固](#安全加固)

---

## 环境要求

| 项目 | 要求 |
|---|---|
| Python | >= 3.10（推荐 3.11 / 3.12） |
| 操作系统 | Linux / macOS / Windows（WSL2） |
| 磁盘 | 最低 2GB（程序 + 依赖），数据盘按上传量预留 |
| 内存 | 最低 512MB（无 MinerU），推荐 2GB+ |
| LLM API | 任一 OpenAI 兼容 API（DeepSeek / OpenAI / Ollama 等） |
| 文件格式 | PDF / DOCX / MD / TXT（MVP 1.0 支持） |

---

## 方式一：Docker 部署（推荐生产）

### 1. 准备配置

```bash
git clone https://gitee.com/meeoliu/kb-zhixu.git
cd kb-zhixu
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 等必填项
```

### 2. 构建并启动

```bash
# 标准启动
docker compose up -d

# 同时启用 MinerU 解析服务（可选，镜像较大）
docker compose --profile mineru up -d

# 查看日志
docker compose logs -f app

# 停止 / 重启
docker compose stop
docker compose restart
```

### 3. 验证

```bash
curl http://localhost:8000/health
# 返回 {"status":"ok","version":"1.0.0",...}
```

### 4. 数据持久化

Docker 卷 `kbrefiner-data` 挂载到 `/app/data`，包含：

```
/app/data/
├── uploads/       # 用户上传的原始文件
├── outputs/       # 任务处理结果（含断点中间文件）
├── tasks.db       # 任务/用户/设置等 SQLite 数据库
└── auth.db        # 认证相关数据（如启用）
```

- `docker compose down` **不删数据**
- `docker compose down -v` **删除数据卷**（谨慎！）

### 5. 自定义端口

`.env` 中设置：

```bash
APP_PORT=9000  # 默认 8000
```

或直接在 `docker-compose.yml` 的 `ports` 映射中修改。

---

## 方式二：pip 安装 + systemd 服务

适合裸金属服务器部署，不使用 Docker。

### 1. 安装

```bash
# 创建虚拟环境（推荐）
python3 -m venv /opt/kbrefiner/venv
source /opt/kbrefiner/venv/bin/activate

# 安装
git clone https://gitee.com/meeoliu/kb-zhixu.git /opt/kbrefiner/app
cd /opt/kbrefiner/app
pip install -e .

# 配置
cp .env.example /opt/kbrefiner/.env
# 编辑 /opt/kbrefiner/.env
```

### 2. 创建系统用户和目录

```bash
sudo useradd -r -s /sbin/nologin -d /opt/kbrefiner kbrefiner
sudo chown -R kbrefiner:kbrefiner /opt/kbrefiner
sudo mkdir -p /opt/kbrefiner/data/uploads /opt/kbrefiner/data/outputs
```

### 3. systemd 服务文件

创建 `/etc/systemd/system/kbrefiner.service`：

```ini
[Unit]
Description=KBRefiner RAG Pipeline Service
After=network.target

[Service]
Type=simple
User=kbrefiner
Group=kbrefiner
WorkingDirectory=/opt/kbrefiner/app
EnvironmentFile=/opt/kbrefiner/.env
ExecStart=/opt/kbrefiner/venv/bin/uvicorn kbrefiner.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

# 安全限制
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/kbrefiner/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 4. 启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable kbrefiner
sudo systemctl start kbrefiner

# 查看状态
sudo systemctl status kbrefiner

# 查看日志
sudo journalctl -u kbrefiner -f
```

---

## 方式三：源码运行（开发用）

```bash
git clone https://gitee.com/meeoliu/kb-zhixu.git
cd kb-zhixu
pip install -e ".[dev]"
cp .env.example .env
# 编辑 .env

# 启动开发服务器（热重载）
python3 -c "
from kbrefiner.main import app
import uvicorn
uvicorn.run('kbrefiner.main:app', host='0.0.0.0', port=8000, reload=True)
"

# 或通过 CLI
kbrefiner serve
```

---

## Nginx 反向代理

生产环境推荐在 KBRefiner 前面加 Nginx，处理 TLS 和 WebSocket 转发。

```nginx
server {
    listen 80;
    server_name kbrefiner.example.com;

    # 反向代理
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket 转发（任务进度实时推送）
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }

    # 上传文件大小限制
    client_max_body_size 50M;
}
```

TLS 配置（使用 Let's Encrypt）：

```bash
sudo certbot --nginx -d kbrefiner.example.com
```

---

## 数据备份与恢复

### 备份

```bash
# 方式一：文件级备份（停服后执行最安全）
docker compose stop
tar -czf kbrefiner-backup-$(date +%Y%m%d).tar.gz -C ./data .
docker compose start

# 方式二：SQLite 在线备份（不停服）
sqlite3 ./data/tasks.db ".backup ./backup-tasks-$(date +%Y%m%d).db"
```

### 恢复

```bash
# 停服 → 恢复数据 → 启服
docker compose stop
rm -rf ./data/*
tar -xzf kbrefiner-backup-20260904.tar.gz -C ./data
docker compose start
```

### 自动备份（crontab）

```bash
# 每天凌晨 3 点备份，保留 30 天
0 3 * * * cd /opt/kbrefiner/app && sqlite3 data/tasks.db ".backup data/backup-$(date +\%Y\%m\%d).db" && find data/backup-*.db -mtime +30 -delete
```

---

## 升级流程

```bash
# 1. 备份数据（重要！）
sqlite3 ./data/tasks.db ".backup ./data/backup-pre-upgrade.db"

# 2. 拉取最新代码
git pull origin master

# 3. 重新构建（Docker）
docker compose up -d --build

# 或 pip 方式
pip install -e . --upgrade

# 4. 验证
curl http://localhost:8000/health
python3 -m pytest tests/ -q   # 可选：跑测试确认

# 5. 如出问题，回滚
git checkout <上一个稳定 commit>
docker compose up -d --build
```

> 升级不会丢失任务数据。SQLite 表使用 `CREATE TABLE IF NOT EXISTS`，新字段通过 `ALTER TABLE ADD COLUMN` 兼容旧数据。

---

## 健康检查与监控

### 健康检查端点

```bash
curl http://localhost:8000/health
```

返回示例：

```json
{
  "status": "ok",
  "version": "1.0.0",
  "llm_model": "deepseek-chat"
}
```

- `status` 为 `ok` 表示服务正常
- `llm_model` 可快速确认模型配置是否正确

### Docker 健康检查

Dockerfile 内置了 `HEALTHCHECK`：

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1
```

查看健康状态：

```bash
docker inspect --format='{{.State.Health.Status}}' kbrefiner-app
```

### 日志

```bash
# Docker
docker compose logs -f app

# systemd
journalctl -u kbrefiner -f

# 日志级别调整（.env）
LOG_LEVEL=DEBUG  # 排障时临时开启
```

---

## 安全加固

### 1. 开启登录认证

`.env` 或系统设置中：

```bash
REQUIRE_LOGIN=true  # 前台页面和 API 均需登录
```

### 2. 创建管理员账号

```bash
kbrefiner createsuperuser
# 按提示输入用户名和密码
```

### 3. LLM API Key 保护

- `.env` 文件权限设为 `600`：`chmod 600 .env`
- 不要将 `.env` 提交到 Git（已在 `.gitignore` 中）
- 后台「系统设置」页可在线配置 Key（AES 加密存储）

### 4. 修改默认端口

```bash
APP_PORT=9000  # 避免使用常见端口
```

### 5. 限制上传大小

```bash
MAX_UPLOAD_SIZE_MB=20  # 按需调小
```

### 6. 定期备份

参考 [数据备份与恢复](#数据备份与恢复) 节，至少每日备份 SQLite 数据库。

---

*文档基于 v1.0.0（2026-09）整理。*
