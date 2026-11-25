"""Local, conservative checks before the Web pipeline sends text to a model."""
from __future__ import annotations

import re

from .base import ParsedDocument

_IMAGE_MARKUP = re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)|<img\b[^>]*>|<!--.*?-->", re.I | re.S)

_FICTION_LABEL = re.compile(
    r"^\s*#{0,6}\s*(?:[《【\[]\s*)?(?:小说(?:正文|全文)|长篇小说|网络小说|轻小说|"
    r"fiction|a novel|novel)\s*(?:[:：·—\-】\]》]|$)",
    re.IGNORECASE | re.MULTILINE,
)
_CHAPTER_HEADING = re.compile(
    r"^\s*#{0,6}\s*(?:第[零〇一二三四五六七八九十百千0-9]+[章回卷]|chapter\s+[\divxlc]+\b)",
    re.IGNORECASE | re.MULTILINE,
)
_DIALOGUE = re.compile(r"[“「『\"][^”」』\"\n]{4,200}[”」』\"]")
_NARRATION = re.compile(r"(?:他|她|他们|她们)(?:说|问|答|低声|笑|转身|抬头|想)|"
                        r"\b(?:he|she|they) (?:said|asked|replied|whispered|thought)\b",
                        re.IGNORECASE)


def pdf_has_usable_text(markdown: str) -> bool:
    """Ignore image references and parser comments when checking PDF text."""
    return any(character.isalnum() for character in _IMAGE_MARKUP.sub("", markdown))


def review_document(document: ParsedDocument, filename: str) -> list[str]:
    """Return actionable warnings without classifying ambiguous documents as unsuitable."""
    warnings = []
    missing = document.pages_without_text
    if missing:
        pages = "、".join(map(str, missing[:8]))
        if len(missing) > 8:
            pages += f"等 {len(missing)} 页"
        else:
            pages += " 页"
        warnings.append(
            f"这份 PDF 共 {document.page_count} 页，第 {pages}未提取到文字。"
            "继续处理会跳过这些页面。你可以先做 OCR，再上传完整版本。"
        )

    # Bound the check independently of document size, and include more than the opener.
    text = document.markdown
    sample = text[:6000]
    if len(text) > 6000:
        midpoint = len(text) // 2
        sample += "\n" + text[midpoint:midpoint + 2000] + "\n" + text[-2000:]
    if (
        _FICTION_LABEL.search(sample)
        or _FICTION_LABEL.search(re.sub(r"[_.]", " ", filename.rsplit(".", 1)[0]))
        or (_CHAPTER_HEADING.search(sample) and len(_DIALOGUE.findall(sample)) >= 2
            and len(_NARRATION.findall(sample)) >= 2)
    ):
        warnings.append(
            "这份文档疑似以故事叙事为主，自动生成的问答可能不适合直接入库。"
            "你可以换成作品介绍、人物设定或章节摘要，也可以继续处理。"
        )
    return warnings
