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

from fastapi import APIRouter, Depends, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient
from kbrefiner.core.pipeline import Pipeline, PipelineConfig
from kbrefiner.core.sensitive import SensitiveDetector
from kbrefiner.db import TaskStore

from .deps import get_llm_client, get_sensitive_detector
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


def _make_task_id() -> str:
    return uuid.uuid4().hex[:12]


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
    settings: Settings = Depends(get_settings),
):
    """上传文档文件，支持 PDF / DOCX / PPTX / XLSX / 图片 / Markdown / TXT 格式。

    Returns:
        {"file_id": "abc123", "filename": "policy.pdf", "size": 12345}
    """
    # 验证文件类型
    allowed_extensions = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".bmp", ".md", ".markdown", ".txt"}
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {ext}。支持: {', '.join(sorted(allowed_extensions))}",
        )

    # 检查文件大小
    if file.size and file.size > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制（{settings.max_upload_size_mb}MB）",
        )

    # 保存到 upload_dir
    file_id = _make_task_id()
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"{file_id}{ext}"

    content = await file.read()
    file_path.write_bytes(content)

    logger.info("文件上传成功: %s (%s, %d bytes)", file_path, file.filename, len(content))

    # 记录任务初始状态
    _task_store[file_id] = {
        "status": _TASK_PENDING,
        "filename": file.filename,
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
    async_mode: bool = False,
    filename: Optional[str] = None,
    settings: Settings = Depends(get_settings),
    llm_client: DeepSeekClient = Depends(get_llm_client),
    detector: SensitiveDetector = Depends(get_sensitive_detector),
):
    """发起文档处理。

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
    # 查找文件
    upload_dir = Path(settings.upload_dir)
    candidates = list(upload_dir.glob(f"{file_id}.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")

    file_path = candidates[0]
    doc_name = filename or file_path.name

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
            "created_at": _task_store.get(file_id, {}).get("created_at", time.time()),
            "file_size": _task_store.get(file_id, {}).get("file_size", 0),
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
            "created_at": _task_store.get(file_id, {}).get("created_at", time.time()),
            "file_size": _task_store.get(file_id, {}).get("file_size", 0),
        }
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")

    result = json.loads(doc.model_dump_json(ensure_ascii=False))
    # 更新任务状态（同步模式也写入 _task_store，供后续 status/result 查询）
    _task_store[file_id] = {
        "status": _TASK_COMPLETED,
        "result": result,
        "filename": doc_name,
        "created_at": _task_store.get(file_id, {}).get("created_at", time.time()),
        "file_size": _task_store.get(file_id, {}).get("file_size", 0),
    }

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
            output_dir = Path(settings.output_dir) / task_id
            stage_files = ["stage1", "stage2", "stage3", "stage4", "final"]
            done = sum(1 for s in stage_files if (output_dir / f"{s}.json").exists())
            progress = done / len(stage_files)
        return {
            "task_id": task_id,
            "filename": stored.get("filename", "未知文件"),
            "status": status,
            "progress": progress,
            "error": stored.get("error"),
            "created_at": stored.get("created_at"),
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
async def list_tasks(settings: Settings = Depends(get_settings)):
    """获取所有任务列表。

    返回按创建时间倒序排列的任务列表。
    """
    tasks = []
    now = time.time()

    # 收集内存中的任务
    for task_id, info in _task_store.items():
        tasks.append({
            "task_id": task_id,
            "filename": info.get("filename", "未知文件"),
            "status": info.get("status", _TASK_PENDING),
            "created_at": info.get("created_at", now),
            "progress": 1.0 if info.get("status") == _TASK_COMPLETED else 0.5 if info.get("status") == _TASK_PROCESSING else 0.0,
            "error": info.get("error"),
            "file_size": info.get("file_size", 0),
        })

    # 补充输出目录中存在的任务
    output_base = Path(settings.output_dir)
    if output_base.exists():
        for task_dir in output_base.iterdir():
            if not task_dir.is_dir():
                continue
            tid = task_dir.name
            if tid not in _task_store:
                # 检查阶段文件
                stages = ["stage1", "stage2", "stage3", "stage4", "final"]
                completed = [s for s in stages if (task_dir / f"{s}.json").exists()]
                if "final" in completed:
                    status = _TASK_COMPLETED
                    progress = 1.0
                elif completed:
                    status = _TASK_PROCESSING
                    progress = len(completed) / len(stages)
                else:
                    status = _TASK_PENDING
                    progress = 0.0
                tasks.append({
                    "task_id": tid,
                    "filename": "未知文件",
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

    # 更新状态
    task_info["status"] = _TASK_CANCELLED
    task_info["error"] = "用户手动取消"

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