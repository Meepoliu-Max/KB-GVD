# ===== 知序 KBRefiner 生产镜像 =====
# 架构：FastAPI + BackgroundTasks + SQLite（无 Redis / Celery 依赖）
#
# 构建：  docker build -t kbrefiner .
# 运行：  docker run -p 8000:8000 --env-file .env -v kbrefiner-data:/app/data kbrefiner

FROM python:3.11-slim AS base

# 环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖（curl 供 healthcheck 使用）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN groupadd -r kbrefiner && useradd -r -g kbrefiner -d /app -s /sbin/nologin kbrefiner

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存
COPY pyproject.toml README.md LICENSE ./
COPY kbrefiner/__init__.py kbrefiner/__init__.py

# 安装依赖（仅核心依赖，不含 MinerU / dev）
COPY kbrefiner kbrefiner
RUN pip install --no-cache-dir .

# 创建数据目录并授权（SQLite / 上传文件 / 输出结果）
RUN mkdir -p /app/data/uploads /app/data/outputs \
    && chown -R kbrefiner:kbrefiner /app

USER kbrefiner

EXPOSE 8000

# 健康检查：调用 /health 接口
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 启动 API 服务（Web 界面随 FastAPI 静态挂载一同提供）
CMD ["uvicorn", "kbrefiner.main:app", "--host", "0.0.0.0", "--port", "8000"]
