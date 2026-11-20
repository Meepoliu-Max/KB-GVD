"""Regression coverage for background task pause, cancellation, and restart."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from kbrefiner.api import routes
from kbrefiner.api.deps import get_audit_store, get_current_user, get_llm_client
from kbrefiner.config import Settings, get_settings
from kbrefiner.core.llm import DeepSeekClient
from kbrefiner.db import TaskStore
from kbrefiner.main import app
from tests.test_e2e import _make_mock_pipeline_output
from tests.test_pipeline import (
    _make_stage1_output_dict,
    _make_stage2_output_dict,
    _make_stage3_output_dict,
    _make_stage4_output_dict,
)


@pytest.fixture
async def task_api(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    settings = Settings(
        _env_file=None,
        llm_api_key="test-key",
        upload_dir=str(tmp_path / "uploads"),
        output_dir=str(tmp_path / "outputs"),
    )
    identity = {"id": "owner", "role": "user"}
    audit = Mock()
    llm = DeepSeekClient(api_key="test-key")
    monkeypatch.setattr(routes, "_task_store", store)
    monkeypatch.setattr(routes, "_background_tasks", {})
    monkeypatch.setattr(routes, "_task_pauses", {})
    monkeypatch.setattr(routes, "get_llm_client", lambda _: llm)
    for dep, override in (
        (get_settings, lambda: settings),
        (get_current_user, lambda: dict(identity)),
        (get_llm_client, lambda: llm),
        (get_audit_store, lambda: audit),
    ):
        monkeypatch.setitem(app.dependency_overrides, dep, override)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(http=client, identity=identity, llm=llm, audit=audit,
                                  store=store, settings=settings)
    finally:
        tasks = list(routes._background_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


async def _upload(api):
    upload = await api.http.post("/api/upload", files={"file": ("sample.md", "# Sample")})
    return upload.raise_for_status().json()["file_id"]


@pytest.mark.parametrize("finish", ["resume", "cancel"])
async def test_pause_drains_in_flight_and_blocks_next_request(task_api, monkeypatch, finish):
    entered = asyncio.Event()
    release = asyncio.Event()
    second = asyncio.Event()

    async def run(_, **kwargs):
        async with task_api.llm.async_request_slot():
            entered.set()
            await release.wait()
        async with task_api.llm.async_request_slot():
            second.set()
        await kwargs["before_finalize"]()
        return _make_mock_pipeline_output()

    monkeypatch.setattr(routes.Pipeline, "run", run)
    task_id = await _upload(task_api)
    started = await task_api.http.post("/api/process", params={"file_id": task_id, "async_mode": True})
    assert started.status_code == 200
    await asyncio.wait_for(entered.wait(), 2)
    background = routes._background_tasks[task_id]
    control = f"/api/task/{task_id}"

    task_api.identity["id"] = "other"
    for action in ("pause", "resume", "cancel"):
        assert (await task_api.http.post(f"{control}/{action}")).status_code == 403
    task_api.identity["id"] = "owner"

    assert (await task_api.http.post(f"{control}/pause")).json()["status"] == "pausing"
    assert (await task_api.http.post(f"{control}/resume")).status_code == 409
    release.set()
    await asyncio.sleep(0)
    assert task_api.store[task_id]["status"] == "paused"
    assert not second.is_set()
    duplicate = await task_api.http.post("/api/process", params={"file_id": task_id})
    assert duplicate.status_code == 409

    response = await task_api.http.post(f"{control}/{finish}")
    assert response.status_code == 200
    if finish == "resume":
        await asyncio.wait_for(background, 2)
        assert second.is_set()
        assert task_api.store[task_id]["status"] == "completed"
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(background, 2)
        assert not second.is_set()
        assert task_api.store[task_id]["status"] == "cancelled"
    assert task_api.audit.record.call_count >= 2


async def test_restart_preserves_pause_and_resumes_saved_checkpoints(task_api, monkeypatch):
    task_id = await _upload(task_api)
    task_api.store.update_status(task_id, "pausing")
    task_dir = Path(task_api.settings.output_dir) / task_id
    task_dir.mkdir(parents=True)
    for number, make_stage in enumerate((
        _make_stage1_output_dict, _make_stage2_output_dict,
        _make_stage3_output_dict, _make_stage4_output_dict,
    ), 1):
        (task_dir / f"stage{number}.json").write_text(json.dumps(make_stage()), encoding="utf-8")

    async def unexpected_model_call(**kwargs):
        pytest.fail("completed checkpoints should not dispatch another model request")

    monkeypatch.setattr(task_api.llm, "chat_json_async", unexpected_model_call)
    routes.recover_interrupted_tasks()
    assert task_api.store[task_id]["status"] == "paused"
    assert task_id not in routes._background_tasks
    status = (await task_api.http.get(f"/api/status/{task_id}")).json()
    assert status["progress"] == 0.8

    response = await task_api.http.post(f"/api/task/{task_id}/resume")
    assert response.status_code == 200
    task = routes._background_tasks.get(task_id)
    if task is not None:
        await asyncio.wait_for(task, 2)
    assert task_api.store[task_id]["status"] == "completed"
    assert (await task_api.http.get(f"/api/export/{task_id}")).status_code == 200


async def test_model_retry_waits_for_resume(task_api, monkeypatch):
    first_response = asyncio.Event()
    retry_pending = asyncio.Event()
    attempts = 0

    async def complete(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await first_response.wait()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="" if attempts == 1 else "finished"
            ))],
            usage=None,
        )

    async def skip_backoff(_):
        retry_pending.set()

    llm = task_api.llm
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    monkeypatch.setattr(llm, "_get_async_client", lambda: sdk)
    monkeypatch.setattr(llm, "_async_sleep_for_retry", skip_backoff)
    broadcast = AsyncMock()
    monkeypatch.setattr(routes.manager, "is_connected", lambda _: True)
    monkeypatch.setattr(routes.manager, "broadcast", broadcast)

    task_id = "retry"
    task_api.store.create(task_id, status="processing")
    pause = routes._TaskPause(task_id)
    llm.async_request_slot = pause.request_slot

    call = asyncio.create_task(llm.chat_async("system", "test"))
    try:
        await asyncio.sleep(0)
        assert attempts == 1
        assert pause.pause() == "pausing"
        first_response.set()
        await asyncio.wait_for(retry_pending.wait(), 2)
        assert task_api.store[task_id]["status"] == "paused"
        assert attempts == 1
        assert not call.done()
        assert [entry.args[1]["status"] for entry in broadcast.await_args_list] == [
            "pausing", "paused",
        ]

        pause.ready.set()
        assert (await asyncio.wait_for(call, 2)).content == "finished"
        assert attempts == 2
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
