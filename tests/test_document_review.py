"""Web document review, validation boundaries, and confirmation regressions."""
import asyncio
from io import BytesIO
from threading import Event
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from kbrefiner.api import routes
from kbrefiner.core.parser.review import pdf_has_usable_text
from tests.test_e2e import _make_mock_pipeline_output
from tests.test_task_pause import task_api as task_api


def _pdf(with_text_page: bool) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    if with_text_page:
        page = writer.add_blank_page(200, 200)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (Read the operations handbook.) Tj ET")
        page.replace_contents(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def test_pdf_without_text_stops_without_contacting_model(task_api, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(routes.Pipeline, "run", run)
    upload = await task_api.http.post("/api/upload", files={"file": ("scan.pdf", _pdf(False))})
    task_id = upload.raise_for_status().json()["file_id"]
    response = await task_api.http.post(
        "/api/process", params={"file_id": task_id, "accept_document_warnings": True},
    )
    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]
    assert task_api.store.get(task_id)["status"] == "failed"
    assert "OCR" in task_api.store.get(task_id)["error"]
    run.assert_not_awaited()


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("manual.pdf", _pdf(True), "第 1 页未提取到文字"),
        ("花园.md", "# 第一章\n她说：“我们去看月亮吧。”\n他问：“你听见了脚步声吗？”", "故事叙事"),
    ],
)
async def test_document_warning_requires_confirmation(task_api, monkeypatch, filename, data, message):
    run = AsyncMock(return_value=_make_mock_pipeline_output())
    monkeypatch.setattr(routes.Pipeline, "run", run)
    upload = await task_api.http.post("/api/upload", files={"file": (filename, data)})
    task_id = upload.raise_for_status().json()["file_id"]
    params = {"file_id": task_id}
    response = await task_api.http.post("/api/process", params=params)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "document_review_required"
    assert message in response.json()["detail"]["message"]
    status = await task_api.http.get(f"/api/status/{task_id}")
    assert status.json()["status"] == "awaiting_confirmation"
    assert message in status.json()["error"]
    run.assert_not_awaited()

    task_api.identity["id"] = "another-user"
    denied = await task_api.http.post(
        "/api/process", params={**params, "accept_document_warnings": True},
    )
    assert denied.status_code == 403
    task_api.identity["id"] = "owner"
    accepted = await task_api.http.post(
        "/api/process", params={**params, "accept_document_warnings": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["task_id"] == task_id
    assert task_api.store.get(task_id)["status"] == "completed"
    assert len(task_api.store.list_all()) == 1
    run.assert_awaited_once()


async def test_handbook_with_chapters_skips_confirmation(task_api, monkeypatch):
    run = AsyncMock(return_value=_make_mock_pipeline_output())
    monkeypatch.setattr(routes.Pipeline, "run", run)
    content = "# 第一章 账号开通\n1. 打开设置。\n2. 点击账号管理。\n# 第二章 常见问题"
    upload = await task_api.http.post("/api/upload", files={"file": ("手册.md", content)})
    task_id = upload.raise_for_status().json()["file_id"]
    response = await task_api.http.post("/api/process", params={"file_id": task_id})
    assert response.status_code == 200
    run.assert_awaited_once()


async def test_cancel_during_extraction_keeps_cancelled_state(task_api, monkeypatch):
    entered, release = Event(), Event()
    parse = routes._parse_uploaded_document

    def wait_then_parse(task_id, settings):
        entered.set()
        if not release.wait(3):
            raise TimeoutError
        return parse(task_id, settings)

    monkeypatch.setattr(routes, "_parse_uploaded_document", wait_then_parse)
    run = AsyncMock()
    monkeypatch.setattr(routes.Pipeline, "run", run)
    upload = await task_api.http.post("/api/upload", files={"file": ("manual.md", "# Handbook")})
    task_id = upload.raise_for_status().json()["file_id"]
    request = asyncio.create_task(task_api.http.post("/api/process", params={"file_id": task_id}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        duplicate = await task_api.http.post("/api/process", params={"file_id": task_id})
        assert duplicate.status_code == 409
        assert (await task_api.http.post(f"/api/task/{task_id}/cancel")).status_code == 200
    finally:
        release.set()
    assert (await request).status_code == 409
    assert task_api.store.get(task_id)["status"] == "cancelled"
    run.assert_not_awaited()


async def test_oversize_upload_does_not_create_task(task_api):
    payload = b"x" * (15 * 1024 * 1024 + 1)
    rejected = await task_api.http.post("/api/upload", files={"file": ("large.txt", payload)})
    assert rejected.status_code == 400
    assert "15 MB" in rejected.json()["detail"]
    assert "按章节拆分" in rejected.json()["detail"]
    assert task_api.store.list_all() == []


def test_pdf_image_markup_is_not_text():
    assert not pdf_has_usable_text('<!-- page 1 -->\n![](figure.png)\n<img src="scan.jpg">')
    assert pdf_has_usable_text("<!-- page 1 -->\n报销流程")
