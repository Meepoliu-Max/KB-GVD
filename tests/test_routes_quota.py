"""Quota regressions for concurrent uploads and per-request Token accounting."""
import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from kbrefiner.api import routes
from kbrefiner.core.llm.exceptions import EmptyContentError, QuotaExceededError
from kbrefiner.db import TaskStore
from tests.test_task_pause import _upload, task_api as task_api


@pytest.fixture
def limits(task_api, monkeypatch):
    values = {
        "allowed_file_types": ["md"], "quota_file_size_mb": 1,
        "quota_task_daily": 10, "quota_upload_daily": 10, "quota_token_daily": 100,
    }
    monkeypatch.setattr(routes, "get_settings_store", lambda: values)
    return values


@pytest.mark.parametrize("key", ["quota_task_daily", "quota_upload_daily"])
async def test_upload_quota_is_enforced(task_api, limits, key):
    limits[key] = 1
    await _upload(task_api)
    response = await task_api.http.post("/api/upload", files={"file": ("second.md", "# second")})
    assert response.status_code == 429
    assert len(task_api.store.list_all()) == 1


async def test_upload_rechecks_size_when_metadata_is_missing(task_api, limits):
    file = UploadFile(io.BytesIO(b"x" * (1024 * 1024 + 1)), filename="large.md", size=None)
    with pytest.raises(HTTPException) as error:
        await routes.upload_file(
            file=file, request=SimpleNamespace(), settings=task_api.settings,
            current_user=task_api.identity,
        )
    assert error.value.status_code == 400
    assert task_api.store.list_all() == []


def test_concurrent_connections_cannot_exceed_upload_quota(tmp_path):
    path = tmp_path / "quota.db"
    TaskStore(path).close()

    def upload(number):
        store = TaskStore(path)
        try:
            return store.create_with_quota(str(number), "sample.md", 1, "owner", 3, 3)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(upload, range(20)))
    assert results.count(None) == 3
    store = TaskStore(path)
    try:
        assert len(store.list_all()) == 3
    finally:
        store.close()


@pytest.mark.parametrize("empty", [False, True])
async def test_model_usage_is_recorded_and_blocks_following_requests(task_api, limits, monkeypatch, empty):
    task_id = await _upload(task_api)
    client = task_api.llm
    client._config.max_retries = 0
    calls = 0

    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="" if empty else "result"))],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        )

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    monkeypatch.setattr(client, "_get_async_client", lambda: sdk)
    routes._configure_task_client(task_id, client)
    if empty:
        with pytest.raises(EmptyContentError):
            await client.chat_async("system", "test")
    else:
        await client.chat_async("system", "test")
    assert task_api.store.daily_user_stats("owner")["token_total"] == 100
    with pytest.raises(QuotaExceededError):
        await client.chat_async("system", "test")
    assert calls == 1
    routes._record_pending_tokens(task_id, client)
    routes._record_pending_tokens(task_id, client)
    assert task_api.store[task_id]["token_consumed"] == 100

    task_api.store.update_status(task_id, "paused")
    response = await task_api.http.post(f"/api/task/{task_id}/resume")
    assert response.status_code == 429
    assert task_api.store[task_id]["status"] == "paused"


async def test_today_usage_for_yesterday_task_and_legacy_tokens(task_api, limits):
    task_api.store["yesterday"] = {
        "user_id": "owner", "created_at": time.time() - 86400, "token_consumed": 70,
    }
    task_api.store.add_tokens("yesterday", 30)
    assert task_api.store.daily_user_stats("owner")["token_total"] == 30
    task_api.store["legacy"] = {
        "user_id": "owner", "created_at": time.time(), "token_consumed": 20,
    }
    assert task_api.store.daily_user_stats("owner")["token_total"] == 50
    task_api.store.add_tokens("legacy", 10)
    assert task_api.store.daily_user_stats("owner")["token_total"] == 60
    # 重启不会丢失已登记的消耗。
    other = TaskStore(task_api.store._db_path)
    try:
        assert other.daily_user_stats("owner")["token_total"] == 60
    finally:
        other.close()


@pytest.mark.parametrize("exceed", [False, True])
async def test_sync_process_preserves_incremental_usage(task_api, limits, monkeypatch, exceed):
    from tests.test_e2e import _make_mock_pipeline_output

    async def run(_, **kwargs):
        task_api.llm.before_request()
        task_api.llm._accumulate_usage({"total_tokens": 100})
        if exceed:
            task_api.llm.before_request()
        return _make_mock_pipeline_output()

    monkeypatch.setattr(routes.Pipeline, "run", run)
    task_id = await _upload(task_api)
    response = await task_api.http.post("/api/process", params={"file_id": task_id})
    assert response.status_code == (429 if exceed else 200)
    assert task_api.store[task_id]["token_consumed"] == 100
    assert task_api.store.daily_user_stats("owner")["token_total"] == 100


async def test_zero_file_size_limit_blocks_upload(task_api, limits):
    limits["quota_file_size_mb"] = 0
    response = await task_api.http.post("/api/upload", files={"file": ("sample.md", "# sample")})
    assert response.status_code == 400
    assert task_api.store.list_all() == []


async def test_upload_race_rejection_cleans_unregistered_file(task_api, limits, monkeypatch):
    from pathlib import Path

    limits["quota_task_daily"] = 1
    file = UploadFile(io.BytesIO(b"# sample"), filename="sample.md", size=None)
    original_read = file.read

    async def competing_upload(size=-1):
        content = await original_read(size)
        task_api.store.create_with_quota("winner", "other.md", 1, "owner", 1, 10)
        return content

    monkeypatch.setattr(file, "read", competing_upload)
    with pytest.raises(HTTPException) as error:
        await routes.upload_file(
            file=file, request=SimpleNamespace(), settings=task_api.settings,
            current_user=task_api.identity,
        )
    assert error.value.status_code == 429
    assert list(Path(task_api.settings.upload_dir).iterdir()) == []
    assert [task["task_id"] for task in task_api.store.list_all()] == ["winner"]


async def test_background_quota_failure_preserves_consumed_tokens(task_api, limits, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(_, **kwargs):
        entered.set()
        await release.wait()
        task_api.llm.before_request()
        task_api.llm._accumulate_usage({"total_tokens": 100})
        task_api.llm.before_request()
        pytest.fail("quota should block subsequent model request")

    monkeypatch.setattr(routes.Pipeline, "run", run)
    task_id = await _upload(task_api)
    response = await task_api.http.post(
        "/api/process", params={"file_id": task_id, "async_mode": True},
    )
    assert response.status_code == 200
    await asyncio.wait_for(entered.wait(), 2)
    background = routes._background_tasks[task_id]
    release.set()
    await asyncio.wait_for(background, 2)
    assert task_api.store[task_id]["status"] == "failed"
    assert "Token" in task_api.store[task_id]["error"]
    assert task_api.store[task_id]["token_consumed"] == 100
