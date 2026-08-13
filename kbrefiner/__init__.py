"""KBRefiner — RAG 四阶流水线精度优化引擎。

四阶流水线：Clean → Chunk → QA → Tag

用法：
    # CLI
    kbrefiner process document.pdf

    # SDK
    from kbrefiner import KBRefiner
    result = KBRefiner().process("document.pdf")
"""
__version__ = "1.0.0"

from kbrefiner.sdk import KBRefiner

__all__ = ["KBRefiner", "__version__"]
