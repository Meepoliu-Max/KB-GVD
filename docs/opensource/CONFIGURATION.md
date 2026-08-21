# KBRefiner 配置项总览

> 面向部署者与二次开发者：完整列出可配置项、默认值、调整后的影响，以及**是否适合暴露到 Web 管理后台**的评估。
> 使用注意与权衡背景见 [README.md](./README.md)。

## 配置分层一览

| 层级 | 配置载体 | 生效方式 | 当前能否通过 .env 调整 |
|---|---|---|---|
| A. 应用配置 | `.env` → `Settings` | 重启服务 | ✅ 全部 |
| B. 流水线性能配置 | `PipelineConfig` | 新任务生效 | ❌ 仅代码级（后台改造重点） |
| C. LLM 客户端配置 | `LLMConfig` | 新任务生效 | ❌ 仅代码级（部分经 `.env` 间接） |
| D. CLI 运行参数 | 命令行选项 | 单次运行 | — |

---

## A. 应用配置（.env / 环境变量）

来源：`kbrefiner/config.py` 的 `Settings`，改后需重启服务。

### LLM 接入

| 变量 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `LLM_API_KEY` | （空，必填） | LLM 服务商 API Key | ❌ 敏感凭据，不建议 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 切换服务商（OpenAI/Ollama 等）。改成 Ollama 即全离线 | ⚠️ 可暴露（下拉预设 + 自定义输入） |
| `LLM_MODEL` | `deepseek-chat` | **主力模型**：文档解析后的 Stage 1/2/4 + CLI/SDK 全流程。换模型直接影响质量与速度 | ✅ 建议暴露（带服务商模型列表提示） |
| `LLM_MODEL_PRO` | `deepseek-chat` | **QA 生成专用模型**（Web 端 Stage 3 使用）。QA 是质量核心，可配更强模型（如 `deepseek-reasoner`），代价是 Stage 3 变慢 | ✅ 建议暴露 |

> ⚠️ 模型名必须是真实模型名，无效名会引发"静默空 content + 重试风暴"（详见开源 README 部署前必读）。

### 文档解析

| 变量 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `PARSER_BACKEND` | `auto` | `auto` 自动检测 MinerU；`mineru` 强制（未装则报错）；`simple` 强制轻量解析（无 OCR，扫描件不可用） | ✅ 建议暴露（三选一 + 安装状态提示） |
| `MINERU_BACKEND` | `pipeline` | MinerU 内部引擎：`pipeline`（快）或 `vlm-engine`（版面理解更强、更慢） | ⚠️ 低频，可折叠在高级设置 |

### 应用与存储

| 变量 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `APP_ENV` | `development` | `development` / `production` / `test` | ❌ 部署级 |
| `APP_HOST` | `0.0.0.0` | 监听地址 | ❌ 部署级 |
| `APP_PORT` | `8000` | 监听端口（docker-compose 通过 `${APP_PORT:-8000}` 透传） | ❌ 部署级 |
| `LOG_LEVEL` | `INFO` | 日志级别，排障时可调 `DEBUG` | ⚠️ 可暴露（排障场景有用） |
| `UPLOAD_DIR` | `./data/uploads` | 上传原件目录 | ❌ 路径类，改动涉及迁移 |
| `OUTPUT_DIR` | `./data/outputs` | 任务输出目录（断点文件也在此） | ❌ 同上 |
| `MAX_UPLOAD_SIZE_MB` | `50` | 上传大小上限，超限返回 400 | ✅ 建议暴露 |
| `DATABASE_URL` | （见注） | ⚠️ **遗留死配置，当前未生效**。任务库实际固定 `./data/tasks.db`；改数据位置请调整 `data/` 目录挂载 | ❌ 待后续架构清理 |

---

## B. 流水线性能配置（PipelineConfig）

来源：`kbrefiner/core/pipeline/orchestrator.py`。**当前不读 .env**——Web 端在 `routes.py` 用默认值构造，CLI/SDK 可实例化时传入。这是管理后台改造的核心区域。

### 重试与断点

| 参数 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `stage_retries` | `2` | 单 Stage 断点校验失败后的重试次数（不含首次）。调大提高偶发失败成功率，但真失败时耗时倍增；Web 端固定 2 | ✅ 建议暴露（范围 0–4） |
| `enable_checkpoint` | `True` | 断点续跑开关。关闭可省磁盘，但失败必须全量重跑；Web 端固定开启 | ❌ 建议保持固定开启 |
| `partition_prefix` | `"P1"` | 多文档分片时 chunk_id 前缀（P1/P2/P3）。单文档场景无需关心 | ❌ 内部逻辑 |

### 并行与分段（性能核心，8 个旋钮）

**Stage 1 清洗 / Stage 2 分块**——按段落边界切段并行：

| 参数 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `stage1_segment_chars` | `8000` | Stage 1 分段阈值（字符数）。**调小**：更快，但跨段术语/敏感检测可能漏报；**调大或 0**：更慢，全文扫描更完整 | ✅ 建议暴露 |
| `stage1_concurrency` | `4` | Stage 1 分段并行数。调大更快，受 API 限流约束 | ✅ 建议暴露（上限提示） |
| `stage2_segment_chars` | `8000` | Stage 2 分段阈值。**调小**：更快，但段边界 chunk 无法跨段合并、粒度变细（实测 800KB PDF：10 → 15 个 chunk）；**调大或 0**：分块更贴近整篇语义，耗时约 +40% | ✅ 建议暴露（附权衡说明） |
| `stage2_concurrency` | `4` | Stage 2 分段并行数，同上受限流约束 | ✅ 建议暴露 |

**Stage 3 QA / Stage 4 打标**——chunks 按批并行：

| 参数 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `stage34_batch_size` | `4` | 每批 chunk 数。**调大**：LLM 调用次数少，但单次输出长（小模型易截断）；**调小**：更稳但调用多、开销大 | ✅ 建议暴露 |
| `stage34_concurrency` | `4` | 批间并行数。DeepSeek 实测安全值 4；调大易 429 | ✅ 建议暴露（默认勿超 4 的提示） |

> 速度参考（800KB PDF，DeepSeek-chat）：默认并行配置全程约 **1 分 36 秒**；全部关闭分段/并行约 5–6 分钟；历史上还有效模型名 + 串行的反面案例 12 分钟未完成 20%。

---

## C. LLM 客户端配置（LLMConfig）

来源：`kbrefiner/core/llm/deepseek_client.py`。当前仅代码级（`api_key/base_url/model` 三项经 `.env` 间接配置）。

| 参数 | 默认值 | 作用与调整影响 | 后台暴露建议 |
|---|---|---|---|
| `max_retries` | `3` | 网络层重试次数（含首次共 N+1 次）。与 `stage_retries` 是两层不同重试 | ⚠️ 高级设置 |
| `retry_base_delay` | `1.0` | 重试退避起始延迟（秒），指数递增 | ❌ 一般无需动 |
| `retry_max_delay` | `30.0` | 重试最大延迟 | ❌ 同上 |
| `timeout` | `300.0` | 单次请求超时（秒）。**reasoner 类推理模型必须留大**（长推理可能 3–5 分钟）；快模型可调小快速失败 | ⚠️ 高级设置 |
| `max_tokens` | `16384` | 单次响应上限。设大避免 JSON 截断；**本地小模型必须按其上下文下调**（如 4096），否则静默截断→校验失败→重试 | ✅ 建议暴露（本地模型场景） |
| `temperature` | `0.0` | 采样温度。0 = 确定性输出（流水线默认）；希望 Stage 3 口语化 QA 更多样可调 0.3–0.7，代价是可复现性下降 | ✅ 建议暴露（带场景说明） |

---

## D. CLI 运行参数

`kbrefiner process` 的单次运行选项（不进配置系统，仅供了解）：

| 选项 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | stdout | 输出文件路径 |
| `--doc-type` | 自动识别 | 强制指定文档类型（制度合规/FAQ/产品活动/技术运维） |
| `--parser` | `auto` | 本次运行的解析后端（覆盖 .env） |
| `--output-dir` | `./output` | 中间结果目录（断点文件位置） |
| `--no-checkpoint` | 关 | 本次运行禁用断点续跑 |
| `--format` | `json` | 导出格式：coze_qa/coze_text/dify_qa/dify_text/dify_jsonl/json |
| `-` | — | 管道模式，从 stdin 读 Markdown |

---

## 管理后台改造建议（汇总）

若要在 Web 管理后台新增配置入口，建议按优先级分三期：

**第一期（价值最高，性能旋钮 8 项 + 上传限制 1 项）**

`stage1/2_segment_chars`、`stage1/2_concurrency`、`stage34_batch_size/concurrency`、`stage_retries`、`MAX_UPLOAD_SIZE_MB`

- 改造点：`Settings` 增加对应字段（带默认值）→ `routes.py` 构造 `PipelineConfig` 时透传。新任务生效、无需重启，风险低
- 界面建议：预设三档「质量优先（分段关闭）/ 均衡（默认）/ 速度优先（segment 5000）」+ 自定义高级面板，比裸露 8 个数字更友好

**第二期（模型与解析 5 项）**

`LLM_MODEL`、`LLM_MODEL_PRO`、`PARSER_BACKEND`、`temperature`、`max_tokens`

- 注意：换模型涉及断点复用问题（见开源 README 坑 4），界面需提示"换模型后建议全量重跑"

**第三期（谨慎或不做）**

`LLM_BASE_URL`、`LOG_LEVEL`、`MINERU_BACKEND`、`timeout` 等高级项，折叠进高级设置；路径/端口/Key 类不建议进后台。

**改造前置工作**：B/C 两层目前不在 `Settings` 中，需先在 `config.py` 加字段（含校验：segment_chars ≥ 0、concurrency 1–16 等），再在 `routes.py` / `sdk.py` 透传，避免直接暴露内部 dataclass。

---

*配置项与代码对应关系基于 v0.3.x（2026-08）：`config.py` / `orchestrator.py` / `deepseek_client.py` / `cli.py`。*
