"""KBRefiner CLI 命令行工具。

命令：
    kbrefiner process <file>          # 处理文档（解析 + 四阶流水线）
    kbrefiner process <file> -o out.json  # 指定输出路径
    kbrefiner serve                   # 启动本地 Web UI
    kbrefiner config                  # 显示当前配置
    kbrefiner version                 # 显示版本号

管道模式：
    cat document.md | kbrefiner process -  # 从 stdin 读取 Markdown

示例：
    $ kbrefiner process policy.pdf
    $ kbrefiner process policy.pdf -o result.json --doc-type 制度合规
    $ kbrefiner serve --port 8000
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from kbrefiner import __version__
from kbrefiner.config import get_settings
from kbrefiner.core.exporter import SUPPORTED_FORMATS, get_exporter


@click.group()
@click.version_option(version=__version__, prog_name="kbrefiner")
def cli():
    """KBRefiner — RAG 四阶流水线精度优化引擎。"""
    pass


@cli.command()
@click.argument("file_path", type=str)
@click.option("-o", "--output", type=str, default=None, help="输出文件路径（默认输出到 stdout）")
@click.option("--doc-type", type=str, default=None, help="文档类型（制度合规/FAQ/产品活动/技术运维）")
@click.option("--parser", type=click.Choice(["auto", "mineru", "simple"]), default="auto", help="文档解析后端")
@click.option("--output-dir", type=str, default="./output", help="中间结果输出目录")
@click.option("--no-checkpoint", is_flag=True, default=False, help="禁用断点续跑")
@click.option("--indent", type=int, default=2, help="JSON 输出缩进")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(SUPPORTED_FORMATS),
    default="json",
    help="导出格式：coze_qa/coze_text/dify_qa/dify_text/dify_jsonl/json",
)
def process(
    file_path: str,
    output: Optional[str],
    doc_type: Optional[str],
    parser: str,
    output_dir: str,
    no_checkpoint: bool,
    indent: int,
    fmt: str,
):
    """处理文档（解析 + 四阶流水线 → 按指定格式输出）。

    \b
    支持的文件格式：PDF, DOCX, PPTX, XLSX, 图片（需 MinerU）
    管道模式：从 stdin 读取 Markdown 时 file_path 传 -
    导出格式：--format coze_qa 生成扣子问答表格 CSV（可直接导入扣子表格知识库）
             --format dify_jsonl 生成 Dify API 批量导入 JSONL
             其他格式见 kbrefiner process --help
    """
    from kbrefiner.sdk import KBRefiner

    # 管道模式：从 stdin 读取
    if file_path == "-":
        markdown = sys.stdin.read()
        if not markdown.strip():
            click.echo("错误：stdin 为空", err=True)
            sys.exit(1)
        click.echo("从 stdin 读取 Markdown，开始处理...", err=True)
        kb = KBRefiner(
            output_dir=output_dir,
            parser_backend=parser,
            enable_checkpoint=not no_checkpoint,
        )
        try:
            result = kb.process_text(markdown, source="stdin")
        finally:
            kb.close()
    else:
        path = Path(file_path)
        if not path.exists():
            click.echo(f"错误：文件不存在: {path}", err=True)
            sys.exit(1)

        click.echo(f"开始处理: {path}", err=True)
        kb = KBRefiner(
            output_dir=output_dir,
            parser_backend=parser,
            enable_checkpoint=not no_checkpoint,
        )
        try:
            result = kb.process(path)
        finally:
            kb.close()

    # 按指定格式导出
    exporter = get_exporter(fmt)
    content = exporter.export(result)

    if output:
        Path(output).write_text(content, encoding="utf-8-sig" if fmt.endswith("_qa") or fmt.endswith("_text") else "utf-8")
        click.echo(f"结果已保存到: {output}（格式: {fmt}）", err=True)
    else:
        # 输出到 stdout（不包含进度信息）
        click.echo(content)

    click.echo("处理完成", err=True)


@cli.command()
@click.option("--host", type=str, default=None, help="监听地址（默认 0.0.0.0）")
@click.option("--port", type=int, default=None, help="监听端口（默认 8000）")
def serve(host: Optional[str], port: Optional[int]):
    """启动本地 Web UI（参考实现）。

    提供 Web 界面用于交互式操作，可作为参考实现。
    生产环境建议使用 CLI 或 SDK 直接调用。
    """
    import uvicorn

    settings = get_settings()
    host = host or settings.app_host
    port = port or settings.app_port

    click.echo(f"启动 KBRefiner Web UI: http://{host}:{port}")
    uvicorn.run("kbrefiner.main:app", host=host, port=port, reload=False)


@cli.command()
def config():
    """显示当前配置。"""
    settings = get_settings()

    click.echo("=== KBRefiner 配置 ===")
    click.echo()

    # LLM 配置
    click.echo("LLM API:")
    click.echo(f"  Base URL:  {settings.llm_base_url}")
    click.echo(f"  Model:     {settings.llm_model}")
    click.echo(f"  Model Pro: {settings.llm_model_pro}")
    api_key = settings.llm_api_key
    if api_key:
        masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        click.echo(f"  API Key:   {masked}")
    else:
        click.echo("  API Key:   未配置")
    click.echo()

    # 解析器
    click.echo(f"Parser Backend: {settings.parser_backend}")
    click.echo()

    # 应用
    click.echo(f"App Env:  {settings.app_env}")
    click.echo(f"App Port: {settings.app_port}")
    click.echo()

    # 检查 MinerU 是否可用
    import shutil
    mineru_available = shutil.which("mineru") is not None
    click.echo(f"MinerU Available: {'是' if mineru_available else '否'}")

    if not settings.llm_api_key:
        click.echo()
        click.echo("⚠ 未配置 LLM API Key！请创建 .env 文件：", err=True)
        click.echo("  cp .env.example .env", err=True)
        click.echo("  编辑 .env 填入你的 API Key", err=True)


@cli.command()
def version():
    """显示版本号。"""
    click.echo(f"kbrefiner {__version__}")


def main():
    """CLI 入口点。"""
    cli()


if __name__ == "__main__":
    main()
