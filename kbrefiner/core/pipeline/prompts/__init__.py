"""Prompt 模板渲染器。

用 Jinja2 加载 core/pipeline/prompts/*.j2 模板文件并渲染。
封装统一接口，流水线代码不直接 import Jinja2 Environment。

用法：
    from kbrefiner.core.pipeline.prompts import render_stage1, render_stage2, render_stage3, render_stage4

    system, user = render_stage1(markdown="...", doc_title="标题", document_source="file.pdf")
    system, user = render_stage2(cleaned_text="...", doc_type="技术运维", partition_prefix="P1")
    system, user = render_stage3(chunks=[{chunk_id, title, content}], doc_type="技术运维",
                                 sensitive_items=[...], chunk_id_prefix="P1")
    system, user = render_stage4(chunks=[{chunk_id, title, content}], doc_type="技术运维")

返回 (system_prompt, user_prompt) 元组，其中 system_prompt 包含系统指令段，user_prompt 包含用户输入段（含可选的 retry_errors 反馈注入）。模板中用 ===END_SYSTEM=== 标记系统段结束。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

# templates 根目录：prompts/
_PROMPTS_DIR = Path(__file__).resolve().parent

# Jinja2 环境：
# - 不启用 autoescape（Prompt 是 Markdown + JSON 说明文字与代码块混合，
#   内容由 Python 字符串传入，不需要 HTML 转义，否则会把 <table>、<td> 转义掉）
# - keep_trailing_newline=True 保留模板末尾换行，与人类阅读格式一致
_ENV = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    autoescape=select_autoescape(disabled_extensions=[".j2"]),
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)

# 4 个模板文件名
_STAGE_FILES: dict[str, str] = {
    "stage1": "stage1_clean.j2",
    "stage2": "stage2_chunk.j2",
    "stage3": "stage3_qa.j2",
    "stage4": "stage4_tag.j2",
}


def _split_system_user(rendered: str) -> tuple[str, str]:
    """把渲染后的完整模板切分为 (system_prompt, user_prompt)。

    约定：===END_SYSTEM=== 作为系统段结束标记。
    该标记之前为系统段，之后为用户段。
    """
    marker = "===END_SYSTEM==="
    pos = rendered.find(marker)
    if pos == -1:
        # 防御性：没找到标记则整个当用户段，系统段空
        return "", rendered
    return rendered[:pos].strip(), rendered[pos + len(marker):].lstrip("\n")


def _render(name: str, **kwargs: Any) -> tuple[str, str]:
    """通用渲染函数，返回 (system, user)。"""
    template = _ENV.get_template(_STAGE_FILES[name])
    rendered = template.render(**kwargs)
    return _split_system_user(rendered)


# =====================================================================
# 4 个 Stage 对外接口
# =====================================================================


def render_stage1(
    markdown: str,
    document_source: str,
    doc_title: str | None = None,
    retry_errors: list[str] | None = None,
    previous_output: str | None = None,
) -> tuple[str, str]:
    """Stage 1：文档类型识别 + 敏感预扫描 + 术语检查。"""
    return _render(
        "stage1",
        markdown=markdown,
        document_source=document_source,
        doc_title=doc_title,
        retry_errors=retry_errors,
        previous_output=previous_output,
    )


def render_stage2(
    cleaned_text: str,
    doc_type: str,
    partition_prefix: str,
    title_context: str | None = None,
    retry_errors: list[str] | None = None,
    previous_output: str | None = None,
) -> tuple[str, str]:
    """Stage 2：语义级智能分块。"""
    return _render(
        "stage2",
        cleaned_text=cleaned_text,
        doc_type=doc_type,
        partition_prefix=partition_prefix,
        title_context=title_context,
        retry_errors=retry_errors,
        previous_output=previous_output,
    )


def render_stage3(
    chunks: list[dict[str, Any]],
    doc_type: str,
    sensitive_items: list[str] | None = None,
    chunk_id_prefix: str = "P1",
    retry_errors: list[str] | None = None,
    previous_output: str | None = None,
) -> tuple[str, str]:
    """Stage 3：多视角口语化 QA 生成 + 敏感脱敏。

    chunks: 每个元素至少含 chunk_id / title / content 三个键
    chunk_id_prefix: 用于异常登记中的 Q 编号前缀（如 P1、P2）
    """
    return _render(
        "stage3",
        chunks=chunks,
        doc_type=doc_type,
        sensitive_items=sensitive_items or [],
        chunk_id_prefix=chunk_id_prefix,
        retry_errors=retry_errors,
        previous_output=previous_output,
    )


def render_stage4(
    chunks: list[dict[str, Any]],
    doc_type: str,
    retry_errors: list[str] | None = None,
    previous_output: str | None = None,
) -> tuple[str, str]:
    """Stage 4：多维元数据自动打标。

    chunks: 每个元素至少含 chunk_id / title / content 三个键
    """
    return _render(
        "stage4",
        chunks=chunks,
        doc_type=doc_type,
        retry_errors=retry_errors,
        previous_output=previous_output,
    )


__all__ = [
    "render_stage1",
    "render_stage2",
    "render_stage3",
    "render_stage4",
]
