# KBRefiner 腾讯云部署指南

这里提供一套在腾讯云服务器上部署 KBRefiner 的脚本。

## 脚本一览

| 脚本               | 作用                               |
| ---------------- | -------------------------------- |
| `install.sh`     | 一键安装 Docker + Compose 依赖         |
| `deploy.sh`      | 拉取代码 → 配置 .env → 构建 → 启动 → 健康检查  |
| `setup-nginx.sh` | 配置域名 + HTTPS（Let's Encrypt 免费证书） |
| `nginx.conf`     | Nginx 反向代理模板（HTTPS + WebSocket）  |
| `backup.sh`      | SQLite + 数据目录定时备份                |

## 快速开始（四步）

### 第 1 步：连接服务器并放入脚本

```bash
# 在本地终端（拥有服务器的公网 IP 和密码/密钥）
scp -r deploy/ root@<服务器公网IP>:/opt/
```

### 第 2 步：安装环境

```bash
ssh root@<服务器公网IP>
cd /opt/deploy
sudo bash install.sh
```

### 第 3 步：部署

```bash
sudo bash deploy.sh
# 之后编辑 .env 填入 LLM_API_KEY
nano /opt/kbrefiner/.env
# 重启任务生效
cd /opt/kbrefiner && docker compose up -d
```

### 第 4 步：创建管理员并（可选）配置 HTTPS

```bash
# 创建管理员账号
docker exec -it kbrefiner-app kbrefiner createsuperuser

# 配置域名 HTTPS（可选但强烈推荐）
sudo bash /opt/deploy/setup-nginx.sh kb.example.com
```

## 部署架构

```
公网 → 安全组(80/443) → Nginx(TLS) → KBRefainer(docker, 8000 内网)
                                      ↓
                                  data/(SQLite + 上传 + 输出)
```

## 腾讯云安全组建议

在腾讯云控制台 > 轻量应用服务器/CVM > 防火墙规则：

| 端口   | 是否开放     | 原因             |
| ---- | -------- | -------------- |
| 22   | 开放（限制来源） | SSH 管理         |
| 443  | 开放       | HTTPS          |
| 80   | 开放       | 证书签发 + HTTP 跳转 |
| 8000 | **不开放**  | 仅内网，Nginx 反代即可 |

## 数据备份

```bash
# 手动备份
sudo bash /opt/deploy/backup.sh

# 每日 03:00 自动备份
crontab -e
# 加入下面这行：
0 3 * * * /opt/deploy/backup.sh
```

