# KBRefiner

**A four-stage RAG precision-refinement engine: `Clean → Chunk → QA → Tag`**

Turns raw documents (PDF / Word / TXT / Markdown) into structured **knowledge atoms**, **Q&A pairs**, and **metadata tags** that give your RAG knowledge base high-quality, query-ready data. Output can be imported directly into Agent platforms such as Coze and Dify — no manual conversion needed.

> Maintained here on GitHub for the international open-source community. The primary (Chinese) docs and ongoing development live in the Gitee repository ([gitee.com/meeoliu/kb-zhixu](https://gitee.com/meeoliu/kb-zhixu)).

## Features

- **Four-stage AI pipeline** — Parse → Clean → Chunk → QA & Tagging
- **Agent-friendly output** — Native import formats for Coze / Dify knowledge bases
- **Private by default** — Runs fully on your own machine; your data never leaves it
- **Bring your own API key** — Works with any OpenAI-compatible LLM (DeepSeek / OpenAI / Ollama)
- **Pluggable parsing** — MinerU (one-stop: OCR / tables / formulas) or a lightweight `pypdf` + `python-docx` parser
- **Three ways to use it** — CLI, Python SDK, and a local Web UI
- **Resumable pipeline** — Intermediate results are persisted; resume from the last checkpoint after a failure
- **8 export formats** — JSON, Markdown, table CSV, Coze CSV, Dify CSV, Dify JSONL, and more
- **Quality inspection report** — Atom coverage, average confidence, and an actionable exception list
- **Built-in Web platform** — Multi-user, role-based access, batch operations, and audit logs

## Quick Start

### 1. Install

```bash
git clone https://github.com/Meepoliu-Max/KB-GVD.git
cd KB-GVD
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# edit .env and fill in your LLM API key
```

### 3. Use

```bash
# CLI — process a document
kbrefiner process document.pdf -o result.json

# SDK — programmatic access
python -c "
from kbrefiner import KBRefiner
result = KBRefiner().process('document.pdf')
print(result.model_dump_json(indent=2))
"

# Web UI — start a local server
kbrefiner serve
```

### Docker one-click deployment

```bash
cp .env.example .env
# edit .env and fill in your LLM API key
docker compose up -d
# open http://localhost:8000
```

### Export formats

```bash
# CLI export
kbrefiner process document.pdf --format coze_qa     # Coze Q&A CSV
kbrefiner process document.pdf --format dify_qa     # Dify Q&A CSV
kbrefiner process document.pdf --format dify_jsonl  # Dify batch-import JSONL

# API export
curl http://localhost:8000/api/export/{task_id}?format=coze_qa -o qa.csv
```

Supported formats: `coze_qa` / `coze_text` / `dify_qa` / `dify_text` / `dify_jsonl` / `json` / `md` / `summary_csv`

## CLI Commands

```
kbrefiner process <file>            # parse + run the four-stage pipeline
kbrefiner process <file> -o out.json  # specify output path
kbrefiner process -                 # read Markdown from stdin (pipe mode)
kbrefiner serve                     # start the local Web UI
kbrefiner config                    # show current configuration
kbrefiner version                   # show the version
```

### Pipe mode

```bash
cat document.md | kbrefiner process - > result.json
```

## Python SDK

```python
from kbrefiner import KBRefiner

# configuration is auto-loaded from .env / environment variables
kb = KBRefiner()

# process a file
result = kb.process("policy.pdf")

# process Markdown text
result = kb.process_text(markdown_text, source="manual.md")

# `result` is a KbDocument object
print(result.model_dump_json(indent=2))

# override LLM settings
kb = KBRefiner(
    api_key="sk-xxx",
    base_url="https://api.deepseek.com",
    model="deepseek-chat",
)
```

## Configuration

Configure in `.env` (see `.env.example`):

| Variable          | Description                     | Default                   |
|-------------------|---------------------------------|---------------------------|
| `LLM_API_KEY`     | LLM API key (required)          | —                         |
| `LLM_BASE_URL`    | LLM API endpoint                | `https://api.deepseek.com`|
| `LLM_MODEL`       | Default model (pipeline)        | `deepseek-chat`           |
| `LLM_MODEL_PRO`   | Pro model (complex reasoning)   | `deepseek-chat`           |
| `PARSER_BACKEND`  | Document parser backend         | `auto`                    |

> **Warning**: the model name must be a real model on your provider (e.g. `deepseek-chat`). An invalid name returns empty content silently, causing retry storms.

### Supported LLMs

KBRefiner is compatible with any OpenAI-format API:

- **DeepSeek** (default) — cost-effective, Chinese-optimized
- **OpenAI** — GPT-4o / GPT-4o-mini
- **Local Ollama** — fully offline, no external API

### Document parser backends

- `auto` (default) — detect whether MinerU is installed
- `mineru` — one-stop parsing (OCR / tables / formulas), requires `pip install mineru`
- `simple` — lightweight parsing (`pypdf` + `python-docx`), no extra dependencies

## The Four-Stage Pipeline

```
Raw document → [Parse] → Markdown
         ↓
    Stage 1: Clean   — clean, denoise, structure recognition
         ↓
    Stage 2: Chunk   — semantic chunking, knowledge-atom extraction
         ↓
    ┌─ Stage 3: QA   — Q&A generation (parallel)
    └─ Stage 4: Tag  — tag annotation (parallel)
         ↓
    Merge → final result (KbDocument)
```

## Web Platform

In addition to the CLI / SDK, KBRefiner ships a full web UI:

- Upload & manage documents, track task progress in real time (WebSocket)
- Quality inspection report with an actionable exception list
- Batch cancel / retry / delete of tasks
- Multi-user with role-based access (super admin / admin / user)
- Operation audit logs
- Admin dashboard (usage stats, trends, rankings) and system settings

## Project Structure

```
kbrefiner/
├── kbrefiner/          # Python package
│   ├── cli.py          # CLI entry
│   ├── sdk.py          # Python SDK
│   ├── config.py       # configuration
│   ├── main.py         # FastAPI web app
│   ├── api/            # REST API routes
│   ├── core/
│   │   ├── llm/        # LLM clients (OpenAI-compatible)
│   │   ├── parser/     # document parsers (MinerU / SimpleParser)
│   │   ├── pipeline/   # four-stage pipeline
│   │   ├── sensitive/  # sensitive-data detection
│   │   └── validation/ # output validation
│   ├── models/         # data models
│   └── static/         # web UI assets
├── tests/              # tests
├── pyproject.toml      # package definition
├── LICENSE             # GPL-3.0
└── .env.example        # configuration template
```

## Development

```bash
# install development dependencies
pip install -e ".[dev]"

# run tests
make test

# lint
make lint

# dev server
make dev
```

## License

GPL-3.0-or-later