#!/usr/bin/env bash
# =============================================================================
# KBRefiner 腾讯云数据备份脚本
# 用途：备份 SQLite 数据库 + 上传/输出文件到备份目录
# 保留策略：保留最近 N 份（默认 14）
#
# 手动: sudo bash backup.sh
# 定时: 编辑 crontab -e, 加入
#         0 3 * * * /opt/kbrefiner/deploy/backup.sh
# =============================================================================
set -e

APP_DIR="${KBREFINER_DIR:-/opt/kbrefiner}"
DATA_DIR="$APP_DIR/data"
BACKUP_DIR="$APP_DIR/backups"
RETENTION="${BACKUP_RETENTION:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/kbrefiner-$STAMP"

mkdir -p "$DEST"

echo "→ 备份到 $DEST"

# SQLite 在线备份（不停服）
if [ -f "$DATA_DIR/tasks.db" ]; then
  sqlite3 "$DATA_DIR/tasks.db" ".backup '$DEST/tasks.db'"
  echo "  ✓ tasks.db"
fi
if [ -f "$DATA_DIR/auth.db" ]; then
  sqlite3 "$DATA_DIR/auth.db" ".backup '$DEST/auth.db'"
  echo "  ✓ auth.db"
fi

# 数据目录（上传文件 + 输出结果）
if [ -d "$DATA_DIR/uploads" ]; then
  cp -r "$DATA_DIR/uploads" "$DEST/uploads"
fi
if [ -d "$DATA_DIR/outputs" ]; then
  cp -r "$DATA_DIR/outputs" "$DEST/outputs"
fi

# 打包压缩
tar -czf "$DEST.tar.gz" -C "$BACKUP_DIR" "kbrefiner-$STAMP"
rm -rf "$DEST"

# 清理过期备份
echo "→ 清理 $RETENTION 天前备份..."
find "$BACKUP_DIR" -name "kbrefiner-*.tar.gz" -mtime +$RETENTION -delete

echo ""
echo "✓ 备份完成: $DEST.tar.gz"
echo "  备份列表:"
ls -lht "$BACKUP_DIR"/*.tar.gz | head -$RETENTION