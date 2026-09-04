#!/usr/bin/env bash
# =============================================================================
# KBRefiner 腾讯云 Nginx + HTTPS 配置脚本
# 用途：为 KBRefiner 配置域名 + Let's Encrypt 免费证书
# 前置：已部署 KBRefiner（docker compose up -d）；域名已解析到本机；80/443 已开放
#
# 用法：sudo bash setup-nginx.sh <your-domain>
# 示例：sudo bash setup-nginx.sh kb.example.com
# =============================================================================
set -e

if [ "$EUID" -ne 0 ]; then
  echo "✗ 请用 root 或 sudo 运行"
  exit 1
fi

DOMAIN="$1"
if [ -z "$DOMAIN" ]; then
  echo "✗ 用法: sudo bash setup-nginx.sh <你的域名>"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL_CONF="$SCRIPT_DIR/nginx.conf"

echo "=============================================="
echo " KBRefiner HTTPS 配置"
echo " 域名: $DOMAIN"
echo "=============================================="

# ---------- 0. 安装 nginx / certbot ----------
install_pkg() {
  yum install -y "$@" 2>/dev/null || apt-get install -y "$@" 2>/dev/null || true
}
command -v nginx   >/dev/null 2>&1 || { echo "→ 安装 Nginx";   install_pkg nginx; }
command -v certbot >/dev/null 2>&1 || { echo "→ 安装 certbot"; install_pkg certbot python3-certbot-nginx; }

# ---------- 1. 生成临时纯 HTTP 配置（用于首次签发证书） ----------
get_conf_dir() {
  if [ -d /etc/nginx/sites-available ]; then
    echo "/etc/nginx/sites-available/kbrefiner.conf"
  else
    echo "/etc/nginx/conf.d/kbrefiner.conf"
  fi
}
CONF=$(get_conf_dir)

cat > "$CONF" <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
}
EOF

if [ -d /etc/nginx/sites-available ]; then
  ln -sf "$CONF" /etc/nginx/sites-enabled/kbrefiner.conf
fi

echo "→ 拉起 nginx（HTTP）"
nginx -t
systemctl enable nginx 2>/dev/null || true
systemctl restart nginx 2>/dev/null || service nginx restart 2>/dev/null || true
echo "  ✓ HTTP 生效: http://$DOMAIN"

# ---------- 2. 签发证书 ----------
echo "→ 申请 Let's Encrypt 证书..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
  --register-unsafely-without-email || true

# ---------- 3. 应用完整 HTTPS 配置 ----------
echo "→ 应用 HTTPS 配置..."
sed "s/SERVER_DOMAIN/$DOMAIN/g" "$FULL_CONF" > "$CONF"
nginx -t
systemctl reload nginx 2>/dev/null || service nginx reload 2>/dev/null || true

echo ""
echo "=============================================="
echo " ✓ HTTPS 配置完成！"
echo "   访问: https://$DOMAIN"
echo "   证书续期: certbot renew (crontab 每天执行即可)"
echo "=============================================="