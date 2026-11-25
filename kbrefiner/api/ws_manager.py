"""WebSocket 连接管理器：按 task_id 管理连接，广播进度。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from .task_progress import STAGES, TaskProgress

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接管理器。

    每个 task_id 可关联多个 WebSocket 连接（多个客户端同时监控）。
    提供广播、单人推送、连接/断开管理。
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, task_id: str, ws: WebSocket) -> None:
        """接受 WebSocket 连接并注册到 task_id。"""
        await ws.accept()
        self._connections.setdefault(task_id, set()).add(ws)
        logger.info("WebSocket 已连接: task_id=%s, 当前连接数=%d", task_id, len(self._connections[task_id]))

    def disconnect(self, task_id: str, ws: WebSocket) -> None:
        """断开 WebSocket 并从 task_id 移除。"""
        conns = self._connections.get(task_id)
        if conns:
            conns.discard(ws)
            if not conns:
                del self._connections[task_id]

    async def send_personal(self, task_id: str, ws: WebSocket, message: dict[str, Any]) -> None:
        """向指定连接发送消息。"""
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            self.disconnect(task_id, ws)

    async def broadcast(self, task_id: str, message: dict[str, Any]) -> None:
        """向 task_id 关联的所有连接广播消息。"""
        conns = self._connections.get(task_id)
        if not conns:
            return
        text = json.dumps(message, ensure_ascii=False)
        dead: set[WebSocket] = set()
        for ws in tuple(conns):
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(task_id, ws)

    def is_connected(self, task_id: str) -> bool:
        """检查是否有连接监听指定 task_id。"""
        return task_id in self._connections and bool(self._connections[task_id])


# 全局单例
manager = ConnectionManager()


# =====================================================================
# 进度推送工具函数
# =====================================================================

async def push_progress(
    task_id: str, *, stage: str, progress: float, status: str, message: str = "",
    completed_stages: list[str] | None = None, active_stages: list[str] | None = None,
    remaining_seconds: int | None = None,
) -> None:
    """向指定 task_id 的所有 WS 连接推送进度。

    Args:
        task_id: 任务 ID
        stage: 阶段名称（如 "Stage1-Clean", "Stage2-Chunk"）
        progress: 整体进度 0.0~1.0
        status: 状态（"pending" | "processing" | "completed" | "failed"）
        message: 可读消息
    """
    await manager.broadcast(task_id, {
        "type": "progress",
        "task_id": task_id,
        "stage": stage,
        "progress": progress,
        "status": status,
        "message": message,
        "completed_stages": completed_stages,
        "active_stages": active_stages,
        "remaining_seconds": remaining_seconds,
    })


async def push_error(task_id: str, error: str) -> None:
    """推送错误消息。"""
    await manager.broadcast(task_id, {
        "type": "error",
        "task_id": task_id,
        "error": error,
    })


async def push_result(task_id: str, result: dict[str, Any]) -> None:
    """推送最终结果。"""
    await manager.broadcast(task_id, {
        "type": "result",
        "task_id": task_id,
        "result": result,
    })


# =====================================================================
# 创建 Pipeline 进度回调
# =====================================================================

def make_progress_callback(task_id: str, progress: TaskProgress):
    """Report activity immediately, with completion based on saved checkpoints."""
    def publish(stage_name: str, status: str, message: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(push_progress(
            task_id, stage=stage_name, status=status, message=message, **progress.snapshot(),
        ))

    def on_progress(stage_name: str, attempt: int, status: str) -> None:
        stage = STAGES.get(stage_name)
        if stage is None or status == "success":
            # A successful batch may still have siblings running in the same stage.
            return

        messages = {
            "calling_llm": "正在处理",
            "validating": "正在校验结果",
            "retrying": f"正在重试（第 {attempt} 次）",
            "started": "开始处理",
            "failed": "处理失败",
        }
        if status not in messages:
            return
        progress.active_stages.add(stage)
        publish(stage_name, "processing", f"{stage_name} {messages[status]}")

    def on_stage_complete(stage_name: str, output: Any) -> None:
        progress.active_stages.discard(STAGES[stage_name])
        if stage_name != "Postprocess":
            publish(stage_name, "completed", f"{stage_name} 完成")

    return on_progress, on_stage_complete


__all__ = [
    "ConnectionManager",
    "manager",
    "push_progress",
    "push_error",
    "push_result",
    "make_progress_callback",
]