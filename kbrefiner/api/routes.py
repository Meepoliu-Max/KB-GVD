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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient
from kbrefiner.core.llm.exceptions import QuotaExceededError
from kbrefiner.core.parser import (
    EmptyDocumentError,
    FileType,
    ParsedDocument,
    ParserError,
    ParserFactory,
)
from kbrefiner.core.parser.review import pdf_has_usable_text, review_document
from kbrefiner.core.pipeline import Pipeline, PipelineConfig
from kbrefiner.core.sensitive import SensitiveDetector
from kbrefiner.db import AuditStore, TaskStore

from kbrefiner.auth import TOKEN_COOKIE, decode_token

from .deps import (
    check_ip_allowed,
    get_audit_store,
    get_current_user,
    get_llm_client,
    get_sensitive_detector,
    get_settings_store,
    get_task_store,
    get_user_store,
    require_admin,
    require_login_enabled,
)
from .ws_manager import make_progress_callback, manager, push_error, push_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["文档处理"])

# 任务状态存储（SQLite 持久化，重启不丢失）
_task_store = TaskStore("./data/tasks.db")
# 后台任务引用，防止 GC
_background_tasks: dict[str, asyncio.Task] = {}
_preflight_tasks: set[str] = set()

# 任务状态常量
_TASK_PENDING = "pending"
_TASK_AWAITING_CONFIRMATION = "awaiting_confirmation"
_TASK_PROCESSING = "processing"
_TASK_PAUSING = "pausing"
_TASK_PAUSED = "paused"
_PAUSE_STATES = (_TASK_PAUSING, _TASK_PAUSED)
_TASK_COMPLETED = "completed"
_TASK_FAILED = "failed"
_TASK_CANCELLED = "cancelled"

_BATCH_ACTIONS = {"cancel", "delete", "retry"}


class _BatchTasksRequest(BaseModel):
    action: str = Field(..., description="批量操作：cancel / delete / retry")
    ids: list[str] = Field(..., min_length=1, description="任务 ID 列表（最多 200）")

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


def _set_task_state(task_id: str, status: str) -> None:
    _task_store.update_status(task_id, status)
    if manager.is_connected(task_id):
        asyncio.create_task(manager.broadcast(task_id, {
            "type": "task_status_changed", "task_id": task_id, "status": status,
        }))


class _TaskPause:
    """Control model dispatch for a single background task."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.ready = asyncio.Event()
        self.ready.set()
        self.in_flight = 0
        self.stopped = False

    def pause(self) -> str:
        self.ready.clear()
        status = _TASK_PAUSING if self.in_flight else _TASK_PAUSED
        _set_task_state(self.task_id, status)
        return status

    @asynccontextmanager
    async def request_slot(self):
        await self.wait_until_ready()
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            if not self.ready.is_set() and self.in_flight == 0:
                task = _task_store.get(self.task_id)
                if task and task["status"] == _TASK_PAUSING:
                    _set_task_state(self.task_id, _TASK_PAUSED)

    def stop(self) -> None:
        self.stopped = True
        self.ready.set()

    async def wait_until_ready(self) -> None:
        while not self.ready.is_set():
            await self.ready.wait()
        if self.stopped:
            raise asyncio.CancelledError


_task_pauses: dict[str, _TaskPause] = {}


def recover_interrupted_tasks() -> None:
    """服务启动时把上次运行遗留的 processing 任务标记为失败。

    后台 asyncio 任务随进程终止而消失，不处理会永远停留在 processing
    （僵尸状态，列表页一直转圈）。断点文件仍在输出目录，
    对同一文件重新发起处理可从断点续跑。
    """
    for task_id, info in _task_store.items():
        if info.get("status") == _TASK_PAUSING:
            _task_store.update_status(task_id, _TASK_PAUSED)

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
    pause = _task_pauses[task_id]
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
            before_finalize=pause.wait_until_ready,
        )
        await pause.wait_until_ready()
        # 运行完成后再次检查是否被取消
        info = _task_store.get(task_id)
        if info and info.get("status") == _TASK_CANCELLED:
            logger.info("后台任务完成后已被标记取消, 丢弃结果: task_id=%s", task_id)
            return

        result = json.loads(doc.model_dump_json(ensure_ascii=False))
        _task_store.update_status(task_id, _TASK_COMPLETED)
        await push_result(task_id, result)
        logger.info("后台任务完成: task_id=%s", task_id)
    except asyncio.CancelledError:
        logger.warning("后台任务被取消: task_id=%s", task_id)
        raise
    except Exception as e:
        logger.error("后台任务失败: task_id=%s, error=%s", task_id, e)
        _task_store.update_status(task_id, _TASK_FAILED, error=str(e))
        await push_error(task_id, str(e))
    finally:
        _record_pending_tokens(task_id, llm_client)


def _check_token_quota(user_id: str | None) -> None:
    if user_id is None:
        return
    limit = int(get_settings_store().get("quota_token_daily") or 0)
    if _task_store.daily_user_stats(user_id)["token_total"] >= limit:
        raise QuotaExceededError("已达今日 Token 消耗上限，请明天再试")


def _configure_task_client(task_id: str, client: DeepSeekClient) -> None:
    owner_id = (_task_store.get(task_id) or {}).get("user_id")
    client.before_request = lambda: _check_token_quota(owner_id)
    client.on_usage = lambda tokens: _task_store.add_tokens(task_id, tokens)


def _record_pending_tokens(task_id: str, client: DeepSeekClient) -> None:
    # 兼容模拟客户端以及调用方预先累计的 usage。
    pending = int(client.usage_total.get("total_tokens", 0)) - client.usage_recorded
    _task_store.add_tokens(task_id, pending)
    client.usage_recorded += max(pending, 0)


def _start_background_task(
    task_id: str, markdown: str, filename: str, llm_client: DeepSeekClient,
    output_dir: Path, settings: Settings,
) -> None:
    _configure_task_client(task_id, llm_client)
    pause = _TaskPause(task_id)
    _task_pauses[task_id] = pause
    llm_client.async_request_slot = pause.request_slot
    task = asyncio.create_task(
        _run_pipeline_background(task_id, markdown, filename, llm_client, output_dir, settings)
    )
    _background_tasks[task_id] = task

    def cleanup(_: asyncio.Task) -> None:
        pause.stop()
        if _background_tasks.get(task_id) is task:
            _background_tasks.pop(task_id)
            _task_pauses.pop(task_id)

    task.add_done_callback(cleanup)


def _parse_uploaded_document(file_id: str, settings: Settings) -> tuple[ParsedDocument, str]:
    upload_dir = Path(settings.upload_dir)
    file_path = next((path for path in upload_dir.glob(f"{file_id}.*")
                      if path.stem == file_id and path.is_file()), None)
    if file_path is None:
        raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
    file_type = FileType.from_path(file_path)
    try:
        parser = ParserFactory.get_parser(file_type, backend=settings.parser_backend)
        document = parser.parse(str(file_path))
        if file_type == FileType.PDF and not pdf_has_usable_text(document.markdown):
            raise EmptyDocumentError("PDF text layer is empty")
        return document, file_path.name
    except EmptyDocumentError as e:
        detail = "未提取到可用文字。请确认文件包含正文，再重新上传。"
        if file_type == FileType.PDF:
            detail = (
                "该 PDF 未提取到可用文字，可能是扫描件或纯图片。"
                "请先用 OCR 识别后重新上传，或上传原始 Word／文本版。"
            )
        raise HTTPException(status_code=422, detail=detail) from e
    except ParserError as e:
        logger.warning("无法解析文档 %s: %s", file_id, e)
        raise HTTPException(
            status_code=422, detail="无法读取此文件。请确认文件可以正常打开，或重新导出后上传。",
        ) from e
    except Exception as e:
        logger.exception("解析文档 %s 时发生异常", file_id)
        raise HTTPException(status_code=500, detail="文档解析暂时失败，请重试。") from e


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
    configured_max_mb = settings_store.get("quota_file_size_mb")
    max_mb = int(settings.max_upload_size_mb if configured_max_mb is None else configured_max_mb)
    max_bytes = max_mb * 1024 * 1024
    size_guidance = f"单文件上限为 {max_mb} MB，请按章节拆分或重新导出较小的文件。"
    if file.size and file.size > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制（当前 {file.size:,} 字节）。{size_guidance}",
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

    # 有界读取同样覆盖 size 未提供或不准确的上传。
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"文件大小超过限制。{size_guidance}")

    # 写文件到登记任务之间没有 await，最终配额检查在数据库事务中执行。
    try:
        file_path.write_bytes(content)
        exceeded = _task_store.create_with_quota(
            file_id, file.filename or "", len(content),
            current_user["id"] if current_user else None,
            int(settings_store.get("quota_task_daily") or 0),
            int(settings_store.get("quota_upload_daily") or 0),
        )
    except Exception:
        file_path.unlink(missing_ok=True)
        raise
    if exceeded:
        file_path.unlink(missing_ok=True)
        label = "任务数" if exceeded == "task" else "上传文档数"
        raise HTTPException(status_code=429, detail=f"已达今日{label}上限，请明天再试")

    logger.info("文件上传成功: %s (%s, %d bytes)", file_path, file.filename, len(content))

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
    accept_document_warnings: bool = False,
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
        accept_document_warnings: 明确继续处理已触发文档检查提醒的文件

    Returns:
        同步: {"task_id": "...", "status": "completed", "result": {...}}
        异步: {"task_id": "...", "status": "processing"}
    """
    # IP 白名单（启用时；回环始终放行）
    check_ip_allowed(request)

    try:
        _check_token_quota(current_user["id"] if current_user else None)
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    stored_info = _task_store.get(file_id) or {}
    if stored_info:
        _task_for_control(file_id, current_user)
    try:
        _check_token_quota(stored_info.get("user_id"))
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    if stored_info.get("status") in (_TASK_PROCESSING, *_PAUSE_STATES) or file_id in _background_tasks:
        raise HTTPException(status_code=409, detail="任务正在处理或已暂停")
    if file_id in _preflight_tasks:
        raise HTTPException(status_code=409, detail="正在检查此文件，请稍候")

    _preflight_tasks.add(file_id)
    try:
        document, uploaded_name = await run_in_threadpool(_parse_uploaded_document, file_id, settings)
    except HTTPException as e:
        if e.status_code == 422 and stored_info.get("status") in (
            _TASK_PENDING, _TASK_AWAITING_CONFIRMATION,
        ):
            latest = _task_store.get(file_id)
            if latest and latest["status"] == stored_info["status"]:
                _task_store.update_status(file_id, _TASK_FAILED, error=e.detail)
        raise
    finally:
        _preflight_tasks.discard(file_id)
    if stored_info:
        latest = _task_for_control(file_id, current_user)
        if latest["status"] != stored_info["status"]:
            raise HTTPException(status_code=409, detail="任务状态已变更，请刷新后重试")
        stored_info = latest
    doc_name = filename or stored_info.get("filename") or uploaded_name
    warnings = (
        review_document(document, stored_info.get("filename") or uploaded_name)
        if not accept_document_warnings else []
    )
    if warnings:
        message = "\n".join(warnings)
        if stored_info.get("status") in (_TASK_PENDING, _TASK_AWAITING_CONFIRMATION):
            _task_store.update_status(file_id, _TASK_AWAITING_CONFIRMATION, error=message)
        raise HTTPException(
            status_code=409,
            detail={"code": "document_review_required", "message": message},
        )
    markdown = document.markdown
    owner_id = stored_info.get("user_id")
    output_dir = Path(settings.output_dir) / file_id
    output_dir.mkdir(parents=True, exist_ok=True)

    if async_mode:
        _task_store[file_id] = {
            **stored_info,
            "filename": doc_name,
            "status": _TASK_PROCESSING,
            "error": None,
            "result": None,
        }
        _start_background_task(file_id, markdown, doc_name, llm_client, output_dir, settings)
        return {"task_id": file_id, "status": _TASK_PROCESSING}

    # 同步模式也先登记运行状态，避免同一任务被重复提交。
    _task_store[file_id] = {
        **stored_info, "status": _TASK_PROCESSING, "filename": doc_name, "error": None,
    }
    _configure_task_client(file_id, llm_client)
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
            **(_task_store.get(file_id) or stored_info),
            "status": _TASK_FAILED,
            "error": str(e),
            "filename": doc_name,
            "user_id": owner_id,
            "created_at": stored_info.get("created_at", time.time()),
            "file_size": stored_info.get("file_size", 0),
        }
        _record_pending_tokens(file_id, llm_client)
        code = 429 if isinstance(e, QuotaExceededError) else 500
        raise HTTPException(status_code=code, detail=f"处理失败: {e}")

    result = json.loads(doc.model_dump_json(ensure_ascii=False))
    # 更新任务状态（同步模式也写入 _task_store，供后续 status/result 查询）
    _task_store[file_id] = {
        **(_task_store.get(file_id) or stored_info),
        "status": _TASK_COMPLETED,
        "result": result,
        "filename": doc_name,
        "user_id": owner_id,
        "created_at": stored_info.get("created_at", time.time()),
        "file_size": stored_info.get("file_size", 0),
    }
    # 回写该任务累计的 LLM Token 消耗（与后台任务口径一致）
    _record_pending_tokens(file_id, llm_client)

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
async def get_status(
    task_id: str,
    settings: Settings = Depends(get_settings),
    current_user: Optional[dict] = Depends(get_current_user),
):
    """查询任务处理状态。

    Returns:
        {"task_id": "...", "status": "completed|processing|failed|pending", "progress": 0.75, "filename": "..."}
    """
    _authorize_task_read(task_id, current_user, settings)
    # 优先检查持久化任务存储（异步任务）
    stored = _task_store.get(task_id)
    if stored:
        status = stored.get("status", _TASK_PROCESSING)
        progress = 0.0
        if status == _TASK_COMPLETED:
            progress = 1.0
        elif status in (_TASK_PROCESSING, *_PAUSE_STATES):
            # 结合输出目录已落盘的阶段文件估算真实进度
            progress = _estimate_progress(Path(settings.output_dir), task_id)
        return {
            "task_id": task_id,
            "filename": stored.get("filename", "未知文件"),
            "status": status,
            "can_pause": status == _TASK_PROCESSING and task_id in _task_pauses,
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
async def get_result(
    task_id: str,
    settings: Settings = Depends(get_settings),
    current_user: Optional[dict] = Depends(get_current_user),
):
    """获取最终处理结果 JSON。"""
    _authorize_task_read(task_id, current_user, settings)
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
    current_user: Optional[dict] = Depends(get_current_user),
):
    """按指定格式导出任务结果（扣子/Dify 可直接导入）。

    format 取值：coze_qa / coze_text / dify_qa / dify_text / dify_jsonl / json
    CSV 格式以 UTF-8-BOM 返回（Excel 中文不乱码），可直接下载后导入。
    """
    from kbrefiner.core.exporter import SUPPORTED_FORMATS, get_exporter
    from kbrefiner.models import KbDocument

    _authorize_task_read(task_id, current_user, settings)

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

    # 下载文件名跟随用户上传的原文件名（如 产品手册_qa.csv），
    # 取不到时回退任务 ID。原始名含中文，Content-Disposition 采用
    # RFC 5987 双写：filename= ASCII 兜底 + filename*= UTF-8 主值。
    info = _task_store.get(task_id) or {}
    raw_name = str(info.get("filename") or "").strip()
    stem = Path(raw_name).stem if raw_name else ""
    stem = stem.replace('"', "").replace("\r", "").replace("\n", "").strip()
    if not stem:
        stem = f"kbrefiner_{task_id}"

    download_name = f"{stem}_{format}{exporter.extension}"
    ascii_fallback = f"kbrefiner_{task_id}_{format}{exporter.extension}"
    from urllib.parse import quote

    from fastapi.responses import Response

    media_types = {
        ".csv": "text/csv",
        ".jsonl": "application/x-ndjson",
        ".json": "application/json",
        ".md": "text/markdown; charset=utf-8",
    }
    return Response(
        content=exporter.export(doc),
        media_type=media_types.get(exporter.extension, "text/plain"),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(download_name)}"
            )
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
        elif status in (_TASK_PROCESSING, *_PAUSE_STATES):
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


def _task_for_control(task_id: str, user: dict | None) -> dict:
    task = _task_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    if user and user.get("role") in ("admin", "super_admin"):
        return task
    if task.get("user_id") != (user["id"] if user else None):
        raise HTTPException(status_code=403, detail="无权操作此任务")
    return task


def _is_admin(user: dict | None) -> bool:
    return bool(user and user.get("role") in ("admin", "super_admin"))


def _authorize_task_read(task_id: str, user: dict | None, settings: Settings) -> None:
    """状态、结果和导出的任务归属校验。

    - 管理员：全部可读
    - 任务记录存在：仅归属用户可读，开放模式匿名读取他人任务返回 401
    - 仅存在于输出目录的历史任务没有归属：开放模式匿名与管理员可读
    """
    task = _task_store.get(task_id)
    if task is None:
        if _is_admin(user) or user is None:
            return
        if (Path(settings.output_dir) / task_id).exists():
            raise HTTPException(status_code=403, detail="无权访问此任务")
        return
    if _is_admin(user) or task.get("user_id") == (user["id"] if user else None):
        return
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    raise HTTPException(status_code=403, detail="无权访问此任务")


def _user_from_websocket(ws: WebSocket) -> Optional[dict]:
    """从 WebSocket 握手的 Cookie 解析登录用户，失败视为匿名。"""
    token = ws.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = get_user_store().get(payload.get("sub", ""))
    if not user or user.get("status") != "active":
        return None
    return user


def _audit_task_control(
    audit_store: AuditStore, request: Request, user: dict | None, task_id: str, action: str,
) -> None:
    try:
        audit_store.record(user=user, action=action, target_type="task",
                           target_id=task_id, request=request)
    except Exception:
        logger.exception("记录任务操作失败: task_id=%s, action=%s", task_id, action)


@router.post("/task/{task_id}/pause")
async def pause_task(
    task_id: str, request: Request,
    user: Optional[dict] = Depends(get_current_user),
    audit_store: AuditStore = Depends(get_audit_store),
):
    check_ip_allowed(request)
    task = _task_for_control(task_id, user)
    status = task["status"]
    if status in _PAUSE_STATES:
        return {"task_id": task_id, "status": status}
    pause = _task_pauses.get(task_id)
    if status != _TASK_PROCESSING or pause is None:
        raise HTTPException(status_code=409, detail="当前任务无法暂停")
    status = pause.pause()
    _audit_task_control(audit_store, request, user, task_id, "task.pause_requested")
    return {"task_id": task_id, "status": status}


@router.post("/task/{task_id}/resume")
async def resume_task(
    task_id: str, request: Request,
    user: Optional[dict] = Depends(get_current_user),
    audit_store: AuditStore = Depends(get_audit_store),
    settings: Settings = Depends(get_settings),
):
    check_ip_allowed(request)
    task = _task_for_control(task_id, user)
    if task["status"] != _TASK_PAUSED:
        raise HTTPException(status_code=409, detail="当前任务无法继续")
    try:
        _check_token_quota(task.get("user_id"))
    except QuotaExceededError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    pause = _task_pauses.get(task_id)
    if pause is None:
        document, uploaded_name = _parse_uploaded_document(task_id, settings)
        output_dir = Path(settings.output_dir) / task_id
        client = get_llm_client(settings)
        _start_background_task(
            task_id, document.markdown, task["filename"] or uploaded_name, client, output_dir, settings,
        )
    else:
        pause.ready.set()
    _set_task_state(task_id, _TASK_PROCESSING)
    _audit_task_control(audit_store, request, user, task_id, "task.resumed")
    return {"task_id": task_id, "status": _TASK_PROCESSING}


@router.post("/task/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    user: Optional[dict] = Depends(get_current_user),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """取消正在处理的任务。

    仅对 async_mode 的后台任务有效。
    取消后任务状态更新为 cancelled，不再继续执行流水线。
    """
    task_info = _task_for_control(task_id, user)

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

    # FR-6.2: 审计点
    try:
        audit_store.record(
            user=user, action="task.cancelled", target_type="task",
            target_id=task_id, request=request,
        )
    except Exception:
        pass

    return {"task_id": task_id, "status": _TASK_CANCELLED, "message": "任务已取消"}


@router.post("/tasks/batch")
async def batch_tasks(
    body: _BatchTasksRequest,
    request: Request,
    _admin: dict = Depends(require_admin),
    task_store: TaskStore = Depends(get_task_store),
    audit_store: AuditStore = Depends(get_audit_store),
):
    """批量操作任务（cancel/delete/retry，仅管理员）。

    - cancel:  将 pending/processing 任务改为 cancelled
    - delete:  删除任务（忽略不存在）
    - retry:   将 failed/cancelled 任务重置为 pending（v1 只改状态，不重跑流水线）
    """
    if body.action not in _BATCH_ACTIONS:
        raise HTTPException(status_code=400, detail=f"非法 action，需为 {sorted(_BATCH_ACTIONS)}")
    if len(body.ids) > 200:
        raise HTTPException(status_code=400, detail="单次批量最多 200 条")

    success = 0
    skipped = 0
    errors: list[dict] = []

    for tid in body.ids:
        try:
            task = task_store.get(tid)
            if body.action == "delete":
                if task is None:
                    skipped += 1
                    continue
                task_store.delete(tid)
                success += 1
            elif body.action == "cancel":
                if task is None:
                    errors.append({"id": tid, "error": "not_found"})
                    continue
                st = task.get("status")
                if st in (_TASK_COMPLETED, _TASK_CANCELLED):
                    skipped += 1
                    continue
                bg = _background_tasks.get(tid)
                if bg and not bg.done():
                    bg.cancel()
                task_store.update_status(tid, _TASK_CANCELLED, error="批量取消")
                try:
                    await push_error(tid, "任务已被管理员批量取消")
                except Exception:
                    pass
                success += 1
            elif body.action == "retry":
                if task is None:
                    errors.append({"id": tid, "error": "not_found"})
                    continue
                st = task.get("status")
                if st not in (_TASK_FAILED, _TASK_CANCELLED):
                    skipped += 1
                    continue
                task_store.update_status(tid, _TASK_PENDING, error=None)
                success += 1
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": tid, "error": str(exc)[:120]})

    # FR-6.2: 批量审计
    try:
        audit_store.record(
            user=_admin, action=f"task.batch_{body.action}", target_type="task",
            detail=f"ids={len(body.ids)};success={success};skipped={skipped};errors={len(errors)}",
            request=request,
        )
    except Exception:
        pass

    return {
        "action": body.action,
        "total": len(body.ids),
        "success": success,
        "skipped": skipped,
        "errors": errors,
    }


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
    # 订阅推送前核对身份与归属：强制登录模式匿名拒绝，普通用户只看自己的任务
    user = _user_from_websocket(ws)
    if user is None and require_login_enabled():
        await ws.close(code=1008)
        return
    if not _is_admin(user):
        task = _task_store.get(task_id)
        if task and task.get("user_id") != (user["id"] if user else None):
            await ws.close(code=1008)
            return

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