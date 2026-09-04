# KBRefiner 配置项总览

> 完整列出可配置项、默认值、调整后的影响，以及**到哪里修改**（本项目部分参数不读 .env，需改代码，文中给出精确位置与示例，方便你或你的 coding agent 直达调整）。
> 使用注意与权衡背景见 [README.md](./README.md)。

## 配置分层一览

| 层级 | 配置载体 | 生效方式 | 修改入口 |
|---|---|---|---|
| A. 应用配置 | `.env` → `Settings` | 重启服务 | 直接改 `.env`，无需动代码 |
| B. 流水线性能配置 | `PipelineConfig` | 新任务生效 | **需改代码**，位置见 [自助调整指引](#自助调整指引b--c-层参数修改位置) |
| C. LLM 客户端配置 | `LLMConfig` | 新任务生效 | 部分经 `.env` 间接，其余需改代码（同上） |
| D. CLI 运行参数 | 命令行选项 | 单次运行 | 命令行直接传参 |

---

## A. 应用配置（.env / 环境变量）

来源：`kbrefiner/config.py` 的 `Settings`，改后需重启服务。

### LLM 接入

| 变量 | 默认值 | 作用与调整影响 |
|---|---|---|
| `LLM_API_KEY` | （空，必填） | LLM 服务商 API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 切换服务商（OpenAI/Ollama 等）。改成 Ollama 即全离线 |
| `LLM_MODEL` | `deepseek-chat` | **主力模型**：文档解析后的 Stage 1/2/4 + CLI/SDK 全流程。换模型直接影响质量与速度 |
| `LLM_MODEL_PRO` | `deepseek-chat` | **QA 生成专用模型**（Web 端 Stage 3 使用）。QA 是质量核心，可配更强模型（如 `deepseek-reasoner`），代价是 Stage 3 变慢 |

> ⚠️ 模型名必须是真实模型名，无效名会引发"静默空 content + 重试风暴"（详见开源 README 部署前必读）。

### 文档解析

| 变量 | 默认值 | 作用与调整影响 |
|---|---|---|
| `PARSER_BACKEND` | `auto` | `auto` 自动检测 MinerU；`mineru` 强制（未装则报错）；`simple` 强制轻量解析（无 OCR，扫描件不可用） |
| `MINERU_BACKEND` | `pipeline` | MinerU 内部引擎：`pipeline`（快）或 `vlm-engine`（版面理解更强、更慢） |

### 应用与存储

| 变量 | 默认值 | 作用与调整影响 |
|---|---|---|
| `APP_ENV` | `development` | `development` / `production` / `test` |
| `APP_HOST` | `0.0.0.0` | 监听地址 |
| `APP_PORT` | `8000` | 监听端口（docker-compose 通过 `${APP_PORT:-8000}` 透传） |
| `LOG_LEVEL` | `INFO` | 日志级别，排障时可调 `DEBUG` |
| `UPLOAD_DIR` | `./data/uploads` | 上传原件目录 |
| `OUTPUT_DIR` | `./data/outputs` | 任务输出目录（断点文件也在此） |
| `MAX_UPLOAD_SIZE_MB` | `50` | 上传大小上限，超限返回 400 |
| `DATABASE_URL` | （见注） | ⚠️ **遗留死配置，当前未生效**。任务库实际固定 `./data/tasks.db`；改数据位置请调整 `data/` 目录挂载 |

---

## B. 流水线性能配置（PipelineConfig）

来源：`kbrefiner/core/pipeline/orchestrator.py`。**不读 .env**——修改位置见文末[自助调整指引](#自助调整指引b--c-层参数修改位置)，参数定义与影响如下。

### 重试与断点

| 参数 | 默认值 | 作用与调整影响 |
|---|---|---|
| `stage_retries` | `2` | 单 Stage 断点校验失败后的重试次数（不含首次）。调大提高偶发失败成功率，但真失败时耗时倍增 |
| `enable_checkpoint` | `True` | 断点续跑开关。关闭可省磁盘，但失败必须全量重跑 |
| `partition_prefix` | `"P1"` | 多文档分片时 chunk_id 前缀（P1/P2/P3）。单文档场景无需关心 |

### 并行与分段（性能核心，8 个旋钮）

**Stage 1 清洗 / Stage 2 分块**——按段落边界切段并行：

| 参数 | 默认值 | 作用与调整影响 |
|---|---|---|
| `stage1_segment_chars` | `8000` | Stage 1 分段阈值（字符数）。**调小**：更快，但跨段术语/敏感检测可能漏报；**调大或 0**：更慢，全文扫描更完整 |
| `stage1_concurrency` | `4` | Stage 1 分段并行数。调大更快，受 API 限流约束 |
| `stage2_segment_chars` | `8000` | Stage 2 分段阈值。**调小**：更快，但段边界 chunk 无法跨段合并、粒度变细（实测 800KB PDF：10 → 15 个 chunk）；**调大或 0**：分块更贴近整篇语义，耗时约 +40% |
| `stage2_concurrency` | `4` | Stage 2 分段并行数，同上受限流约束 |

**Stage 3 QA / Stage 4 打标**——chunks 按批并行：

| 参数 | 默认值 | 作用与调整影响 |
|---|---|---|
| `stage34_batch_size` | `4` | 每批 chunk 数。**调大**：LLM 调用次数少，但单次输出长（小模型易截断）；**调小**：更稳但调用多、开销大 |
| `stage34_concurrency` | `4` | 批间并行数。DeepSeek 实测安全值 4；调大易 429 |

> 速度参考（800KB PDF，DeepSeek-chat）：默认并行配置全程约 **1 分 36 秒**；全部关闭分段/并行约 5–6 分钟。

---

## C. LLM 客户端配置（LLMConfig）

来源：`kbrefiner/core/llm/deepseek_client.py`。`api_key / base_url / default_model` 三项经 `.env` 间接配置，其余**修改位置见文末指引**。

| 参数 | 默认值 | 作用与调整影响 |
|---|---|---|
| `max_retries` | `3` | 网络层重试次数（含首次共 N+1 次）。与 `stage_retries` 是两层不同重试 |
| `retry_base_delay` | `1.0` | 重试退避起始延迟（秒），指数递增 |
| `retry_max_delay` | `30.0` | 重试最大延迟 |
| `timeout` | `300.0` | 单次请求超时（秒）。**reasoner 类推理模型必须留大**（长推理可能 3–5 分钟）；快模型可调小快速失败 |
| `max_tokens` | `16384` | 单次响应上限。设大避免 JSON 截断；**本地小模型必须按其上下文下调**（如 4096），否则静默截断→校验失败→重试 |
| `temperature` | `0.0` | 采样温度。0 = 确定性输出（流水线默认）；希望 Stage 3 口语化 QA 更多样可调 0.3–0.7，代价是可复现性下降 |

---

## D. CLI 运行参数

`kbrefiner process` 的单次运行选项（无需改代码，命令行直接传）：

| 选项 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | stdout | 输出文件路径 |
| `--doc-type` | 自动识别 | 强制指定文档类型（制度合规/FAQ/产品活动/技术运维） |
| `--parser` | `auto` | 本次运行的解析后端（覆盖 .env） |
| `--output-dir` | `./output` | 中间结果目录（断点文件位置） |
| `--no-checkpoint` | 关 | 本次运行禁用断点续跑 |
| `--format` | `json` | 导出格式：json/md/summary_csv/coze_qa/coze_text/dify_qa/dify_text/dify_jsonl |
| `-` | — | 管道模式，从 stdin 读 Markdown |

---

## 自助调整指引：B / C 层参数修改位置

> B/C 层参数当前不读 `.env`，按部署方式到下列位置修改。**新任务生效，无需重启服务**（Web 端下一次上传起作用；改代码后 `docker compose up -d --build` 或重启 uvicorn）。

### 场景 1：Web 部署（Docker / uvicorn）

**B 层流水线参数** — 编辑 `kbrefiner/api/routes.py`，共**两处** `PipelineConfig(` 构造需同步修改（搜 `stage3_model=` 可一次定位两处）：

- 后台异步任务处（`_run_pipeline_background` 函数内，约 L100）
- 同步处理处（`process_document` 函数内，约 L280）

```python
pipeline = Pipeline(
    llm_client=llm_client,
    config=PipelineConfig(
        output_dir=output_dir,
        stage_retries=2,
        enable_checkpoint=True,
        partition_prefix="P1",
        stage1_model=settings.llm_model,
        stage2_model=settings.llm_model,
        stage3_model=settings.llm_model_pro,
        stage4_model=settings.llm_model,
        # ↓ 性能旋钮在此追加（不写则用默认值 8000/4/8000/4/4/4）
        stage2_segment_chars=12000,   # 例：更粗的分段，chunk 更贴近整篇语义
        stage34_concurrency=4,
    ),
)
```

**C 层 LLM 参数** — 编辑 `kbrefiner/api/deps.py` 的 `get_llm_client`：

```python
config = LLMConfig(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    default_model=settings.llm_model,
    # ↓ 按需追加
    temperature=0.0,
    max_tokens=16384,
    timeout=300.0,
)
```

### 场景 2：SDK / CLI 使用

`KBRefiner(...)` 目前只透传 `output_dir / enable_checkpoint / model*` 等；调整全部性能参数请直接使用底层组件（均公开导出）：

```python
from pathlib import Path
from kbrefiner.core.llm import DeepSeekClient, LLMConfig
from kbrefiner.core.pipeline import Pipeline, PipelineConfig

client = DeepSeekClient(api_key="sk-xxx", base_url="https://api.deepseek.com")
pipeline = Pipeline(client, PipelineConfig(
    output_dir=Path("./output"),
    stage2_segment_chars=12000,   # 分段/并发/批大小等全部可传
    stage34_concurrency=4,
    stage3_model="deepseek-reasoner",
))
doc = await pipeline.run(markdown_text, document_source="policy.pdf")
```

### 场景 3：想让 B/C 层参数走 .env（推荐进阶玩家）

三步：`config.py` 的 `Settings` 加字段 → `routes.py` 两处构造点透传 → `.env` 里配。示例：

```python
# 1) kbrefiner/config.py — Settings 内追加
stage2_segment_chars: int = 8000        # .env: STAGE2_SEGMENT_CHARS=12000
stage34_concurrency: int = 4            # .env: STAGE34_CONCURRENCY=4

# 2) kbrefiner/api/routes.py — 两处 PipelineConfig 内透传
stage2_segment_chars=settings.stage2_segment_chars,
stage34_concurrency=settings.stage34_concurrency,
```

> 注意：`.env` 改动需重启服务；换模型后旧任务的断点文件仍是旧模型产出（见开源 README 坑 4），建议全量重跑。

---

*配置项与代码对应关系基于 v1.0.0（2026-09）：`config.py` / `orchestrator.py` / `deepseek_client.py` / `cli.py` / `routes.py` / `deps.py`。*
