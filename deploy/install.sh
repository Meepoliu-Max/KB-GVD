#!/usr/bin/env bash
# =============================================================================
# KBRefiner 腾讯云一键环境安装脚本
# 用途：在全新的腾讯云服务器上安装 Docker + Docker Compose 插件
# 适用：Rocky Linux / CentOS / Ubuntu / Debian
#
# 用法：sudo bash install.sh
# =============================================================================
set -e

echo "=============================================="
echo " KBRefiner 环境安装脚本 (Docker + Compose)"
echo "=============================================="

# 检测 root
if [ "$EUID" -ne 0 ]; then
  echo "✗ 请用 root 或 sudo 运行: sudo bash install.sh"
  exit 1
fi

detect_os() {
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "$ID"
  else
    echo "unknown"
  fi
}

OS=$(detect_os)
echo "检测到系统: $OS"

install_docker() {
  echo "→ 安装 Docker..."
  if command -v docker >/dev/null 2>&1; then
    echo "  已安装 Docker: $(docker --version)"
    return 0
  fi

  case "$OS" in
    ubuntu|debian)
      apt-get update -y
      curl -fsSL https://get.docker.com | sh
      ;;
    centos|rhel|rocky|almalinux|tencentos)
      yum install -y yum-utils
      yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
      systemctl enable --now docker
      ;;
    *)
      echo "✗ 无法识别的系统，请手动安装 Docker: https://docs.docker.com/engine/install/"
      exit 1
      ;;
  esac
  echo "  ✓ Docker 安装完成"
}

install_compose() {
  echo "→ 检查 Docker Compose..."
  if docker compose version >/dev/null 2>&1; then
    echo "  已安装 Compose: $(docker compose version --short)"
    return 0
  fi
  # 兼容旧版 docker-compose
  if command -v docker-compose >/dev/null 2>&1; then
    echo "  已安装 docker-compose: $(docker-compose --version)"
    alias docker-compose=docker compose 2>/dev/null || true
    return 0
  fi
  echo "✗ 未找到 docker compose 插件，请升级 Docker 至 >= 20.10: https://docs.docker.com/compose/install/"
  exit 1
}

install_docker
install_compose

echo ""
echo "=============================================="
echo " 环境准备完成！下一步:"
echo "   sudo bash deploy.sh"
echo "=============================================="