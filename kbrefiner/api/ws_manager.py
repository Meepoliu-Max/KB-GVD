"""WebSocket 连接管理器：按 task_id 管理连接，广播进度。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

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
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        for ws in dead:
            conns.discard(ws)
        if not conns:
            del self._connections[task_id]

    def is_connected(self, task_id: str) -> bool:
        """检查是否有连接监听指定 task_id。"""
        return task_id in self._connections and bool(self._connections[task_id])


# 全局单例
manager = ConnectionManager()


# =====================================================================
# 进度推送工具函数
# =====================================================================

async def push_progress(task_id: str, *, stage: str, progress: float, status: str, message: str = "") -> None:
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

def make_progress_callback(task_id: str, total_stages: int = 5):
    """创建 Pipeline 的 on_progress 回调，自动推送进度到 WebSocket。

    stage_runner 中的 on_progress(stage_name, attempt, status) 格式：
    - stage_name: "Stage1-Clean" 等
    - attempt: 当前尝试次数（1-based）
    - status: "calling_llm" | "validating" | "success" | "retrying"

    返回的回调为同步函数（匹配 stage_runner 的 ProgressCallback 签名），
    内部通过 asyncio.ensure_future 调度异步推送（stage_runner 在事件循环中运行）。

    Returns:
        on_progress callable, on_stage_complete callable
    """
    stage_order = ["Stage1-Clean", "Stage2-Chunk", "Stage3-QA", "Stage4-Tag", "Postprocess"]
    completed_count = 0

    def _schedule(coro) -> None:
        """调度协程执行；无事件循环时（同步 CLI 场景）静默关闭跳过。"""
        try:
            asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()

    def _on_progress(stage_name: str, attempt: int, status: str) -> None:
        nonlocal completed_count

        stage_idx = stage_order.index(stage_name) if stage_name in stage_order else -1
        base = stage_idx if stage_idx >= 0 else 0

        if status == "calling_llm":
            msg = f"{stage_name} 正在调用 LLM 生成..."
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=(base + 0.2) / total_stages,
                                    status="processing", message=msg))
        elif status == "validating":
            msg = f"{stage_name} 结果校验中..."
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=(base + 0.6) / total_stages,
                                    status="processing", message=msg))
        elif status == "success":
            completed_count += 1
            msg = f"{stage_name} 完成"
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=(base + 1) / total_stages,
                                    status="completed", message=msg))
        elif status == "retrying":
            msg = f"{stage_name} 第 {attempt} 次重试..."
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=(base + 0.1) / total_stages,
                                    status="processing", message=msg))
        elif status == "started":
            msg = f"{stage_name} 开始处理..."
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=base / total_stages,
                                    status="processing", message=msg))
        elif status == "failed":
            msg = f"{stage_name} 处理失败"
            _schedule(push_progress(task_id, stage=stage_name,
                                    progress=completed_count / total_stages,
                                    status="failed", message=msg))

    def _on_stage_complete(stage_name: str, output: Any) -> None:
        # stage_runner 已完成推送，这里仅做额外日志
        logger.info("Stage 完成回调: %s (task_id=%s)", stage_name, task_id)

    return _on_progress, _on_stage_complete


__all__ = [
    "ConnectionManager",
    "manager",
    "push_progress",
    "push_error",
    "push_result",
    "make_progress_callback",
]