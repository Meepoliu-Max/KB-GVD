#!/usr/bin/env bash
# =============================================================================
# KBRefiner 腾讯云主部署脚本
# 用途：拉取代码 → 配置 .env → 构建 → 启动 → 健康检查
#
# 用法：sudo bash deploy.sh
# 可选：sudo bash deploy.sh --skip-git   (跳过 git clone，用于已 clone 的目录)
# =============================================================================
set -e

# ---------- 参数 ----------
APP_DIR="${KBREFINER_DIR:-/opt/kbrefiner}"
REPO_URL="https://gitee.com/meeoliu/kb-zhixu.git"
BRANCH="master"

echo "=============================================="
echo " KBRefiner 部署"
echo " 目录: $APP_DIR"
echo "=============================================="

# ---------- 1. 拉取代码 ----------
mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -d .git ] || [ "$1" == "--skip-git" ]; then
  if [ "$1" == "--skip-git" ]; then
    echo "→ 跳过 git clone，使用当前目录代码"
  fi
  if [ ! -d .git ]; then
    echo "→ 拉取代码..."
    git clone --depth 1 -b "$BRANCH" "$REPO_URL" .
  fi
else
  echo "→ 更新代码..."
  git fetch origin
  git checkout "$BRANCH"
  git reset --hard origin/"$BRANCH"
fi

# ---------- 2. 配置 .env ----------
if [ ! -f .env ]; then
  echo "→ 创建 .env（从模板）..."
  cp .env.example .env
  echo "⚠️  请先编辑 $APP_DIR/.env 填入 LLM_API_KEY"
  echo "    例如: nano $APP_DIR/.env"
  echo "    未配置 Key 前部署仍会继续，但任务无法运行。"
else
  echo "→ .env 已存在，保留现有配置"
fi

# ---------- 3. 构建镜像 ----------
echo "→ 构建 Docker 镜像..."
docker compose build

# ---------- 4. 启动 ----------
echo "→ 启动服务..."
docker compose up -d

# ---------- 5. 等待健康检查 ----------
echo "→ 等待服务就绪..."
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:${APP_PORT:-8000}/health" >/dev/null 2>&1; then
    STATUS=$(curl -fsS "http://localhost:${APP_PORT:-8000}/health")
    echo ""
    echo "✓ 部署成功！"
    echo "  健康检查: $STATUS"
    echo "  访问地址: http://<服务器公网IP>:${APP_PORT:-8000}"
    echo ""
    echo "  下一步:"
    echo "  1. 创建管理员账号: docker exec -it kbrefiner-app kbrefiner createsuperuser"
    echo "  2. 建议配置 HTTPS + 域名（见 deploy/setup-nginx.sh）"
    exit 0
  fi
  sleep 2
done

echo "✗ 服务未在 60 秒内就绪，请查看日志:"
echo "  docker compose logs -f app"
exit 1