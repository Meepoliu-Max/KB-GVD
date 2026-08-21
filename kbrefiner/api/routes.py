"""API 路由层：文件上传、处理触发、状态查询、结果预览、WebSocket 进度。

提供 RESTful 接口供前端调用：
- POST /api/upload    上传文档文件，返回 file_id
- POST /api/process   发起处理，同步等待结果或启动异步处理
- GET  /api/tasks     获取所有任务列表
- GET  /api/status/{task_id}  查询处理状态
- GET  /api/result/{task_id}  获取最终结果 JSON
- POST /api/task/{task_id}/cancel  取消正在处理的任务
- WS   /api/ws/{task_id}     实时进度推送（连接后自动接收进度）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect

from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient
from kbrefiner.core.pipeline import Pipeline, PipelineConfig
from kbrefiner.core.sensitive import SensitiveDetector
from kbrefiner.db import TaskStore

from .deps import (
    check_ip_allowed,
    get_current_user,
    get_llm_client,
    get_sensitive_detector,
    get_settings_store,
)
from .ws_manager import make_progress_callback, manager, push_error, push_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["文档处理"])

# 任务状态存储（SQLite 持久化，重启不丢失）
_task_store = TaskStore("./data/tasks.db")
# 后台任务引用，防止 GC
_background_tasks: dict[str, asyncio.Task] = {}

# 任务状态常量
_TASK_PENDING = "pending"
_TASK_PROCESSING = "processing"
_TASK_COMPLETED = "completed"
_TASK_FAILED = "failed"
_TASK_CANCELLED = "cancelled"

# 阶段落盘文件（用于估算真实进度）
_STAGE_FILES = ("stage1", "stage2", "stage3", "stage4", "final")


def _estimate_progress(output_base: Path, task_id: str) -> float:
    """按输出目录已落盘的文件估算进度（0.0~1.0）。

    final.json 是流水线最后落盘的文件，存在即视为已完成（兼容
    关闭 checkpoint、只有 final.json 的场景）；否则按阶段文件数估算。
    """
    task_dir = output_base / task_id
    if (task_dir / "final.json").exists():
        return 1.0
    done = sum(1 for s in _STAGE_FILES if (task_dir / f"{s}.json").exists())
    return done / len(_STAGE_FILES)


def _make_task_id() -> str:
    return uuid.uuid4().hex[:12]


def recover_interrupted_tasks() -> None:
    """服务启动时把上次运行遗留的 processing 任务标记为失败。

    后台 asyncio 任务随进程终止而消失，不处理会永远停留在 processing
    （僵尸状态，列表页一直转圈）。断点文件仍在输出目录，
    对同一文件重新发起处理可从断点续跑。
    """
    interrupted = [
        task_id for task_id, info in _task_store.items()
        if info.get("status") == _TASK_PROCESSING
    ]
    for task_id in interrupted:
        _task_store.update_status(
            task_id, _TASK_FAILED, error="服务重启，任务已中断（重新发起可从断点续跑）"
        )
    if interrupted:
        logger.info("启动恢复：%d 个中断任务已标记为 failed: %s", len(interrupted), interrupted)


async def _run_pipeline_background(
    task_id: str,
    markdown: str,
    document_source: str,
    llm_client: DeepSeekClient,
    output_dir: Path,
    settings: Settings,
) -> None:
    """在后台运行 Pipeline，通过 WebSocket 推送进度。"""
    on_progress, on_stage_complete = make_progress_callback(task_id)

    pipeline = Pipeline(
        llm_client=llm_client,
        config=PipelineConfig(
            output_dir=output_dir,
            stage_retries=2,
            enable_checkpoint=True,
            partition_prefix="P1",
            # 显式传入 .env 配置的真实模型名。
            # PipelineConfig 内置默认值（deepseek-v4-*）为无效模型名，
            # DeepSeek API 会静默返回空 content 导致重试风暴。
            stage1_model=settings.llm_model,
            stage2_model=settings.llm_model,
            stage3_model=settings.llm_model_pro,
            stage4_model=settings.llm_model,
        ),
    )

    try:
        # 检查是否已被取消
        info = _task_store.get(task_id)
        if info and info.get("status") == _TASK_CANCELLED:
            logger.info("后台任务已被取消, 不执行: task_id=%s", task_id)
            return

        doc = await pipeline.run(
            markdown=markdown,
            document_source=document_source,
            on_progress=on_progress,
            on_stage_complete=on_stage_complete,
        )
        # 运行完成后再次检查是否被取消
        info = _task_store.get(task_id)
        if info and info.get("status") == _TASK_CANCELLED:
            logger.info("后台任务完成后已被标记取消, 丢弃结果: task_id=%s", task_id)
            return

        result = json.loads(doc.model_dump_json(ensure_ascii=False))
        await push_result(task_id, result)
        _task_store.update_status(task_id, _TASK_COMPLETED)
        # 回写该任务累计的 LLM Token 消耗（客户端实例生命周期内所有成功调用）
        _task_store.add_tokens(
            task_id, int(llm_client.usage_total.get("total_tokens", 0))
        )
        logger.info("后台任务完成: task_id=%s", task_id)
    except asyncio.CancelledError:
        logger.warning("后台任务被取消: task_id=%s", task_id)
        _task_store.update_status(task_id, _TASK_CANCELLED, error="任务已被取消")
        await push_error(task_id, "任务已被取消")
    except Exception as e:
        logger.error("后台任务失败: task_id=%s, error=%s", task_id, e)
        await push_error(task_id, str(e))
        _task_store.update_status(task_id, _TASK_FAILED, error=str(e))
    finally:
        _background_tasks.pop(task_id, None)


@router.post("/upload")
async def upload_file(
    file: UploadFile,
    request: Request,
    settings: Settings = Depends(get_settings),
    current_user: Optional[dict] = Depends(get_current_user),
):
    """上传文档文件（类型 / 大小 / 用户配额 / IP 白名单四重校验）。

    开放模式（require_login=False）匿名可上传，user_id 为 NULL（配额不限制匿名）；
    强制登录模式由依赖层保证已登录。

    Returns:
        {"file_id": "abc123", "filename": "policy.pdf", "size": 12345}
    """
    # IP 白名单（启用时；回环始终放行）
    check_ip_allowed(request)

    settings_store = get_settings_store()

    # 文件类型：后台"允许的文件类型"设置（默认保持历史行为全量开放）
    allowed_types = {str(t).lower().lstrip(".") for t in settings_store.get("allowed_file_types") or []}
    allowed_extensions = {f".{t}" for t in allowed_types} | {".markdown"}
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}。当前允许: {', '.join(sorted(allowed_extensions))}",
        )

    # 文件大小：后台配额覆盖 .env
    max_mb = int(settings_store.get("quota_file_size_mb") or settings.max_upload_size_mb)
    if file.size and file.size > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制（{max_mb}MB）",
        )

    # 用户配额（仅登录用户；匿名开放模式不限制）
    if current_user:
        stats = _task_store.daily_user_stats(current_user["id"])
        if stats["task_count"] >= int(settings_store.get("quota_task_daily") or 0):
            raise HTTPException(status_code=429, detail="已达今日任务数上限，请明天再试")
        if stats["upload_count"] >= int(settings_store.get("quota_upload_daily") or 0):
            raise HTTPException(status_code=429, detail="已达今日上传文档数上限，请明天再试")

    # 保存到 upload_dir
    file_id = _make_task_id()
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"{file_id}{ext}"

    content = await file.read()
    file_path.write_bytes(content)

    logger.info("文件上传成功: %s (%s, %d bytes)", file_path, file.filename, len(content))

    # 记录任务初始状态（关联上传用户，管理后台按用户聚合统计）
    _task_store[file_id] = {
        "status": _TASK_PENDING,
        "filename": file.filename,
        "user_id": current_user["id"] if current_user else None,
        "created_at": time.time(),
        "file_size": len(content),
    }

    return {
        "file_id": file_id,
        "filename": file.filename,
        "size": len(content),
        "path": str(file_path),
    }


@router.post("/process")
async def process_document(
    file_id: str,
    request: Request,
    async_mode: bool = False,
    filename: Optional[str] = None,
    settings: Settings = Depends(get_settings),
    llm_client: DeepSeekClient = Depends(get_llm_client),
    detector: SensitiveDetector = Depends(get_sensitive_detector),
    current_user: Optional[dict] = Depends(get_current_user),
):
    """发起文档处理（IP 白名单 + 用户 Token 配额校验）。

    用文档解析器解析 → 4 阶流水线 → 输出最终 JSON。
    两种模式：
    - 同步（默认）：等待结果返回
    - async_mode=True：asyncio 后台任务 + WebSocket 进度

    Args:
        file_id: 上传时返回的文件 ID
        async_mode: 是否 asyncio 后台处理
        filename: 可选，原始文件名

    Returns:
        同步: {"task_id": "...", "status": "completed", "result": {...}}
        异步: {"task_id": "...", "status": "processing"}
    """
    # IP 白名单（启用时；回环始终放行）
    check_ip_allowed(request)

    # Token 日配额：已消耗达到上限则拒绝发起新任务（匿名开放模式不限制）
    if current_user:
        settings_store = get_settings_store()
        stats = _task_store.daily_user_stats(current_user["id"])
        if stats["token_total"] >= int(settings_store.get("quota_token_daily") or 0):
            raise HTTPException(status_code=429, detail="已达今日 Token 消耗上限，请明天再试")

    # 查找文件
    upload_dir = Path(settings.upload_dir)
    candidates = list(upload_dir.glob(f"{file_id}.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")

    file_path = candidates[0]
    # 文件名优先级：显式参数 > 任务存储的上传原始文件名 > 磁盘文件名
    # （磁盘文件以上传时生成的 file_id 重命名存储，直接用会导致列表页显示一堆数字）
    stored_info = _task_store.get(file_id) or {}
    doc_name = filename or stored_info.get("filename") or file_path.name
    # 保留上传时关联的用户（后续写入不覆盖丢失）
    owner_id = stored_info.get("user_id")

    # MinerU 解析（同步和 async_mode 都需要）
    from kbrefiner.core.parser import ParserFactory, FileType

    file_type = FileType.from_path(file_path)
    parser = ParserFactory.get_parser(file_type)
    try:
        parsed = parser.parse(str(file_path))
    except Exception as e:
        logger.error("MinerU 解析失败: %s", e)
        raise HTTPException(status_code=500, detail=f"文档解析失败: {e}")

    markdown = parsed.markdown
    output_dir = Path(settings.output_dir) / file_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # async_mode：asyncio 后台任务 + WebSocket 进度
    if async_mode:
        # 更新任务状态
        _task_store[file_id] = {
            "status": _TASK_PROCESSING,
            "filename": doc_name,
            "user_id": owner_id,
            "created_at": stored_info.get("created_at", time.time()),
            "file_size": stored_info.get("file_size", 0),
        }
        task = asyncio.create_task(
            _run_pipeline_background(
                task_id=file_id,
                markdown=markdown,
                document_source=doc_name,
                llm_client=llm_client,
                output_dir=output_dir,
                settings=settings,
            )
        )
        _background_tasks[file_id] = task
        return {"task_id": file_id, "status": _TASK_PROCESSING}

    # 同步模式：等待结果
    pipeline = Pipeline(
        llm_client=llm_client,
        config=PipelineConfig(
            output_dir=output_dir,
            stage_retries=2,
            enable_checkpoint=True,
            partition_prefix="P1",
            # 显式传入 .env 配置的真实模型名（同后台任务）
            stage1_model=settings.llm_model,
            stage2_model=settings.llm_model,
            stage3_model=settings.llm_model_pro,
            stage4_model=settings.llm_model,
        ),
    )

    try:
        doc = await pipeline.run(
            markdown=markdown,
            document_source=doc_name,
        )
    except Exception as e:
        logger.error("流水线处理失败: %s", e)
        _task_store[file_id] = {
            "status": _TASK_FAILED,
            "error": str(e),
            "filename": doc_name,
            "user_id": owner_id,
            "created_at": stored_info.get("created_at", time.time()),
            "file_size": stored_info.get("file_size", 0),
        }
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")

    result = json.loads(doc.model_dump_json(ensure_ascii=False))
    # 更新任务状态（同步模式也写入 _task_store，供后续 status/result 查询）
    _task_store[file_id] = {
        "status": _TASK_COMPLETED,
        "result": result,
        "filename": doc_name,
        "user_id": owner_id,
        "created_at": stored_info.get("created_at", time.time()),
        "file_size": stored_info.get("file_size", 0),
    }
    # 回写该任务累计的 LLM Token 消耗（与后台任务口径一致）
    _task_store.add_tokens(
        file_id, int(llm_client.usage_total.get("total_tokens", 0))
    )

    # 保存最终结果到 checkpoint（与后台任务一致）
    final_path = output_dir / "final.json"
    final_path.write_text(
        doc.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "task_id": file_id,
        "status": "completed",
        "result": result,
    }


@router.get("/status/{task_id}")
async def get_status(task_id: str, settings: Settings = Depends(get_settings)):
    """查询任务处理状态。

    Returns:
        {"task_id": "...", "status": "completed|processing|failed|pending", "progress": 0.75, "filename": "..."}
    """
    # 优先检查持久化任务存储（异步任务）
    stored = _task_store.get(task_id)
    if stored:
        status = stored.get("status", _TASK_PROCESSING)
        progress = 0.0
        if status == _TASK_COMPLETED:
            progress = 1.0
        elif status == _TASK_PROCESSING:
            # 结合输出目录已落盘的阶段文件估算真实进度
            progress = _estimate_progress(Path(settings.output_dir), task_id)
        return {
            "task_id": task_id,
            "filename": stored.get("filename", "未知文件"),
            "status": status,
            "progress": progress,
            "error": stored.get("error"),
            "created_at": stored.get("created_at"),
            "updated_at": stored.get("updated_at"),
            "file_size": stored.get("file_size", 0),
        }

    # 检查输出目录
    output_dir = Path(settings.output_dir) / task_id
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 检查各阶段完成情况
    stages = ["stage1", "stage2", "stage3", "stage4", "final"]
    completed = []
    for s in stages:
        if (output_dir / f"{s}.json").exists():
            completed.append(s)

    if "final" in completed:
        status = _TASK_COMPLETED
        progress = 1.0
    elif completed:
        status = _TASK_PROCESSING
        progress = len(completed) / len(stages)
    else:
        status = _TASK_PENDING
        progress = 0.0

    return {
        "task_id": task_id,
        "filename": "未知文件",
        "status": status,
        "progress": progress,
        "completed_stages": completed,
    }


@router.get("/result/{task_id}")
async def get_result(task_id: str, settings: Settings = Depends(get_settings)):
    """获取最终处理结果 JSON。"""
    output_dir = Path(settings.output_dir) / task_id
    final_path = output_dir / "final.json"

    if not final_path.exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 的结果尚未生成")

    data = json.loads(final_path.read_text(encoding="utf-8"))
    return data


@router.get("/export/{task_id}")
async def export_result(
    task_id: str,
    format: str = "json",
    settings: Settings = Depends(get_settings),
):
    """按指定格式导出任务结果（扣子/Dify 可直接导入）。

    format 取值：coze_qa / coze_text / dify_qa / dify_text / dify_jsonl / json
    CSV 格式以 UTF-8-BOM 返回（Excel 中文不乱码），可直接下载后导入。
    """
    from kbrefiner.core.exporter import SUPPORTED_FORMATS, get_exporter
    from kbrefiner.models import KbDocument

    if format not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的格式 '{format}'，支持: {', '.join(SUPPORTED_FORMATS)}",
        )

    final_path = Path(settings.output_dir) / task_id / "final.json"
    if not final_path.exists():
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 的结果尚未生成")

    doc = KbDocument.model_validate_json(final_path.read_text(encoding="utf-8"))
    exporter = get_exporter(format)

    media_types = {
        ".csv": "text/csv",
        ".jsonl": "application/x-ndjson",
        ".json": "application/json",
    }
    from fastapi.responses import Response

    return Response(
        content=exporter.export(doc),
        media_type=media_types.get(exporter.extension, "text/plain"),
        headers={
            "Content-Disposition": f'attachment; filename="kbrefiner_{task_id}_{format}{exporter.extension}"'
        },
    )


@router.get("/tasks")
async def list_tasks(
    settings: Settings = Depends(get_settings),
    current_user: Optional[dict] = Depends(get_current_user),
):
    """获取任务列表（按创建时间倒序）。

    数据隔离：
    - 匿名（开放模式未登录）与管理员：返回全部任务
    - 登录的普通用户：仅返回自己上传的任务
    """
    # 普通登录用户只看自己的任务；匿名（开放模式）与管理员看全部
    scope_user_id = None
    if current_user and current_user.get("role") not in ("super_admin", "admin"):
        scope_user_id = current_user["id"]

    tasks = []
    now = time.time()
    output_base = Path(settings.output_dir)

    # 收集任务库中的任务
    for task_id, info in _task_store.items():
        if scope_user_id is not None and info.get("user_id") != scope_user_id:
            continue
        status = info.get("status", _TASK_PENDING)
        if status == _TASK_COMPLETED:
            progress = 1.0
        elif status == _TASK_PROCESSING:
            # 按落盘阶段文件估算真实进度（与 /status 口径一致）
            progress = _estimate_progress(output_base, task_id)
        else:
            progress = 0.0
        tasks.append({
            "task_id": task_id,
            "filename": info.get("filename", "未知文件"),
            "status": status,
            "created_at": info.get("created_at", now),
            "progress": progress,
            "error": info.get("error"),
            "file_size": info.get("file_size", 0),
            "token_consumed": int(info.get("token_consumed") or 0),
        })

    # 补充输出目录中存在的任务（不在任务库中的历史任务）。
    # 历史任务无归属用户，仅在非隔离视角（匿名/管理员）下展示
    if scope_user_id is None and output_base.exists():
        for task_dir in output_base.iterdir():
            if not task_dir.is_dir():
                continue
            tid = task_dir.name
            if tid not in _task_store:
                # 按阶段文件判断状态与进度
                progress = _estimate_progress(output_base, tid)
                if progress >= 1.0:
                    status = _TASK_COMPLETED
                elif progress > 0:
                    status = _TASK_PROCESSING
                else:
                    status = _TASK_PENDING
                # 从最终结果读取原始文档名（历史任务兜底）
                final_file = task_dir / "final.json"
                hist_name = "未知文件"
                if final_file.exists():
                    try:
                        hist_name = (
                            json.loads(final_file.read_text(encoding="utf-8"))
                            .get("document_info", {})
                            .get("source") or hist_name
                        )
                    except (json.JSONDecodeError, OSError):
                        pass
                tasks.append({
                    "task_id": tid,
                    "filename": hist_name,
                    "status": status,
                    "created_at": task_dir.stat().st_ctime,
                    "progress": progress,
                    "error": None,
                    "file_size": 0,
                })

    # 按创建时间倒序
    tasks.sort(key=lambda t: t["created_at"], reverse=True)
    return {"tasks": tasks, "total": len(tasks)}


@router.post("/task/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消正在处理的任务。

    仅对 async_mode 的后台任务有效。
    取消后任务状态更新为 cancelled，不再继续执行流水线。
    """
    task_info = _task_store.get(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    current_status = task_info.get("status")
    if current_status == _TASK_COMPLETED:
        raise HTTPException(status_code=400, detail="任务已完成，无法取消")
    if current_status == _TASK_CANCELLED:
        raise HTTPException(status_code=400, detail="任务已被取消")

    # 尝试取消后台 asyncio 任务
    background_task = _background_tasks.get(task_id)
    if background_task and not background_task.done():
        background_task.cancel()
        logger.info("已发送取消信号: task_id=%s", task_id)

    # 更新状态（get() 返回副本，必须写回存储才能持久化）
    _task_store.update_status(task_id, _TASK_CANCELLED, error="用户手动取消")

    # 推送取消通知（如果 WebSocket 还在）
    try:
        await push_error(task_id, "任务已被用户取消")
    except Exception:
        pass

    return {"task_id": task_id, "status": _TASK_CANCELLED, "message": "任务已取消"}


@router.websocket("/ws/{task_id}")
async def websocket_endpoint(ws: WebSocket, task_id: str):
    """WebSocket 实时进度推送。

    客户端连接后自动开始接收进度推送。
    支持监听模式：不发送消息，只接收服务端推送。

    消息格式（服务端→客户端）：
    - {"type": "progress", "task_id": "...", "stage": "...", "progress": 0.5, "status": "processing", "message": "..."}
    - {"type": "result", "task_id": "...", "result": {...}}
    - {"type": "error", "task_id": "...", "error": "..."}
    """
    await manager.connect(task_id, ws)
    try:
        # 保持连接，直到客户端断开
        while True:
            # 接收客户端消息（心跳保持）
            try:
                data = await ws.receive_text()
                # 客户端可发送 ping 维持连接
                if data == "ping":
                    await ws.send_text('{"type":"pong"}')
            except WebSocketDisconnect:
                break
    except Exception:
        logger.exception("WebSocket 异常: task_id=%s", task_id)
    finally:
        manager.disconnect(task_id, ws)
        logger.info("WebSocket 已断开: task_id=%s", task_id)