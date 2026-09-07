#!/usr/bin/env bash
#
# KBRefiner 双平台 / 双分支同步脚本
#
#   分支映射:
#     master    (中文文档)   ->  Gitee origin/master
#     github-en (英文文档)   ->  GitHub github/main
#
# 用法:
#   ./scripts/sync.sh status            # 查看各分支与远端的新旧状态
#   ./scripts/sync.sh push              # 推送当前分支到对应远端
#   ./scripts/sync.sh pull              # 拉取当前分支对应的远端
#   ./scripts/sync.sh push -f           # 强制推送(覆盖远端历史, 慎用)
#   ./scripts/sync.sh push master       # 指定中文分支推送
#   ./scripts/sync.sh push github-en    # 指定英文分支推送
#
# 说明:
#   - 无参数时默认仅显示 status(只读, 不会改动任何分支)
#   - push 前会自动做一次 fetch 并在有分叉时提示, 避免误覆盖
#   - 切换分支前请确保工作区干净(git status)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ---------- 分支 -> 远端映射 ----------
resolve_remote() {
  case "$1" in
    master)    echo "origin master label=Gitee(中文)";;
    github-en) echo "github main  label=GitHub(英文)";;
    *)         echo "";;
  esac
}

CMD="${1:-status}"
BRANCH="${2:-}"
FORCE=0
if [ "${CMD}" = "-f" ] || [ "${BRANCH}" = "-f" ]; then
  echo "提示: -f 需放在命令之后, 示例: ./scripts/sync.sh push -f"
  FORCE=1
  [ "${CMD}" = "-f" ] && { CMD="${BRANCH:-}"; BRANCH=""; }
fi

# 若还残留 -f(如 "push master -f"), 额外解析一次
if [ "${BRANCH}" = "-f" ]; then
  FORCE=1
  BRANCH=""
fi

if [ -z "${BRANCH}" ]; then
  BRANCH="$(git branch --show-current 2>/dev/null || true)"
fi

# 默认命令过滤: 仅支持 status / push / pull
case "${CMD}" in
  status|push|pull) ;;
  *) echo "未知命令: ${CMD} (支持 status / push / pull)"; exit 1;;
esac

if [ -z "${BRANCH}" ]; then
  echo "错误: 无法确定当前分支, 请显式指定, 如: ./scripts/sync.sh push master"
  exit 1
fi

REMOTE_META="$(resolve_remote "${BRANCH}")"
if [ -z "${REMOTE_META}" ]; then
  echo "错误: 分支 '${BRANCH}' 没有可同步的远端(支持 master / github-en)。"
  exit 1
fi

REMOTE="$(echo "${REMOTE_META}" | awk '{print $1}')"
REMOTE_BRANCH="$(echo "${REMOTE_META}" | awk '{print $2}')"
LABEL="$(echo "${REMOTE_META}" | sed 's/.*label=//')"

echo "==> 分支: ${BRANCH}  ->  ${REMOTE}/${REMOTE_BRANCH}  [${LABEL}]"

# ---------- status ----------
if [ "${CMD}" = "status" ]; then
  git fetch "${REMOTE}" 2>/dev/null || true
  echo "---------------------------------------------"
  printf "本地   %s: " "${BRANCH}";        git log -1 --oneline "${BRANCH}" 2>/dev/null || echo "(无)"
  printf "远端   %s/%s: " "${REMOTE}" "${REMOTE_BRANCH}";
  git log -1 --oneline "${REMOTE}/${REMOTE_BRANCH}" 2>/dev/null || echo "(无)"
  echo "---------------------------------------------"
  AHEAD=$(( $(git rev-list --count "${REMOTE}/${REMOTE_BRANCH}..${BRANCH}" 2>/dev/null || echo 0) ))
  BEHIND=$(( $(git rev-list --count "${BRANCH}..${REMOTE}/${REMOTE_BRANCH}" 2>/dev/null || echo 0) ))
  echo "本地领先远端 ${AHEAD} 个提交, 落后 ${BEHIND} 个提交"
  exit 0
fi

# ---------- push / pull 前置校验: 工作区需干净 ----------
DIRTY="$(git status --porcelain)"
if [ -n "${DIRTY}" ]; then
  echo "警告: 工作区有未提交的改动, 请先提交或暂存(stash)后再同步。"
  echo "未提交文件:"
  echo "${DIRTY}"
  exit 1
fi

git fetch "${REMOTE}" 2>/dev/null || true

# ---------- push ----------
if [ "${CMD}" = "push" ]; then
  AHEAD=$(( $(git rev-list --count "${REMOTE}/${REMOTE_BRANCH}..${BRANCH}" 2>/dev/null || echo 0) ))
  BEHIND=$(( $(git rev-list --count "${BRANCH}..${REMOTE}/${REMOTE_BRANCH}" 2>/dev/null || echo 0) ))

  if [ "${BEHIND}" -gt 0 ] && [ "${AHEAD}" -gt 0 ] && [ "${FORCE}" -eq 0 ]; then
    echo "警告: 本地分支与远端产生分叉(本地领先 ${AHEAD}, 落后 ${BEHIND})。"
    echo "      push 前需整合远端改动。立即强制覆盖请使用: ./scripts/sync.sh push -f"
    exit 1
  fi

  git push "${REMOTE}" "${BRANCH}:${REMOTE_BRANCH}" ${FORCE:+-f}
  echo "✓ 已推送 ${BRANCH} -> ${REMOTE}/${REMOTE_BRANCH}"
  exit 0
fi

# ---------- pull ----------
if [ "${CMD}" = "pull" ]; then
  git pull "${REMOTE}" "${REMOTE_BRANCH}"
  echo "✓ 已拉取 ${REMOTE}/${REMOTE_BRANCH} -> ${BRANCH}"
  exit 0
fi