# KBRefiner

RAG 四阶流水线精度优化引擎：**Clean → Chunk → QA → Tag**

将原始文档（PDF / DOCX / PPTX 等）转化为结构化知识原子 + QA 对 + 元数据标签，为 RAG 知识库提供高质量入库数据。

## 特性

- **四阶 AI 流水线**：清洗 → 分块 → QA 生成 → 标签标注
- **本地部署**：数据不离本地，隐私安全由你掌控
- **自带 API Key**：支持所有 OpenAI 兼容的 LLM API（DeepSeek / OpenAI / Ollama 等）
- **插件化解析**：MinerU（一站式）或轻量解析器（pypdf + python-docx）
- **三种接入方式**：CLI 命令行 / Python SDK / 本地 Web UI
- **断点续跑**：中间结果持久化，失败后可从断点继续

## 快速开始

### 1. 安装

```bash
git clone https://github.com/your-org/kbrefiner.git
cd kbrefiner
pip install -e .
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key
```

### 3. 使用

```bash
# CLI：处理文档
kbrefiner process document.pdf -o result.json

# SDK：编程调用
python -c "
from kbrefiner import KBRefiner
result = KBRefiner().process('document.pdf')
print(result.model_dump_json(indent=2))
"

# Web UI：启动本地服务
kbrefiner serve
```

## CLI 命令

```
kbrefiner process <file>          # 处理文档（解析 + 四阶流水线）
kbrefiner process <file> -o out.json  # 指定输出路径
kbrefiner process -               # 从 stdin 读取 Markdown（管道模式）
kbrefiner serve                   # 启动本地 Web UI
kbrefiner config                  # 显示当前配置
kbrefiner version                 # 显示版本号
```

### 管道模式

```bash
cat document.md | kbrefiner process - > result.json
```

## Python SDK

```python
from kbrefiner import KBRefiner

# 从 .env / 环境变量自动读取配置
kb = KBRefiner()

# 处理文件
result = kb.process("policy.pdf")

# 处理 Markdown 文本
result = kb.process_text(markdown_text, source="manual.md")

# result 是 KbDocument 对象
print(result.model_dump_json(indent=2))

# 自定义 LLM 配置
kb = KBRefiner(
    api_key="sk-xxx",
    base_url="https://api.deepseek.com",
    model="deepseek-v4-flash",
)
```

## 配置说明

在 `.env` 文件中配置（参考 `.env.example`）：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | LLM API Key（必填） | - |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.deepseek.com` |
| `LLM_MODEL` | 默认模型（四阶流水线） | `deepseek-v4-flash` |
| `LLM_MODEL_PRO` | Pro 模型（复杂推理） | `deepseek-v4-pro` |
| `PARSER_BACKEND` | 文档解析后端 | `auto` |

### 支持的 LLM

KBRefiner 兼容所有 OpenAI 格式的 API：

- **DeepSeek**（默认）：性价比高，中文优化
- **OpenAI**：GPT-4o / GPT-4o-mini
- **本地 Ollama**：完全离线，无需外部 API

### 文档解析后端

- `auto`（默认）：自动检测 MinerU 是否安装
- `mineru`：一站式解析（OCR / 表格 / 公式），需 `pip install mineru`
- `simple`：轻量解析（pypdf + python-docx），无额外依赖

## 四阶流水线

```
原始文档 → [Parse] → Markdown
         ↓
    Stage 1: Clean    — 清洗、去噪、结构识别
         ↓
    Stage 2: Chunk    — 语义分块、知识原子提取
         ↓
    ┌─ Stage 3: QA   — QA 对生成（并行）
    └─ Stage 4: Tag  — 标签标注（并行）
         ↓
    Merge → 最终结果（KbDocument）
```

## 项目结构

```
kbrefiner/
├── kbrefiner/          # Python 包
│   ├── cli.py          # CLI 命令行工具
│   ├── sdk.py          # Python SDK
│   ├── config.py       # 配置管理
│   ├── main.py         # FastAPI Web 应用
│   ├── api/            # REST API 路由
│   ├── core/
│   │   ├── llm/        # LLM 客户端（通用 OpenAI 兼容）
│   │   ├── parser/     # 文档解析（MinerU / SimpleParser）
│   │   ├── pipeline/   # 四阶流水线
│   │   ├── sensitive/  # 敏感数据检测
│   │   └── validation/ # 输出校验
│   ├── models/         # 数据模型
│   ├── static/         # Web UI 静态文件
│   └── tasks/          # 异步任务（Celery）
├── tests/              # 测试
├── pyproject.toml      # 包定义
├── LICENSE             # GPL-3.0
└── .env.example        # 配置模板
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
make test

# 代码检查
make lint

# 启动开发服务器
make dev
```

## License

GPL-3.0-or-later
