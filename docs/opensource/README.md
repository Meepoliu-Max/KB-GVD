# KBRefiner 开源用户指南

> 本目录面向**开源使用者和部署者**，聚焦真实使用中踩过的坑、需要做的权衡。
> 项目总览与快速开始见仓库根目录 [README.md](../../README.md)。

## 目录

- [部署前必读](#部署前必读)
- [已知坑与规避方法](#已知坑与规避方法)
- [性能调优与权衡](#性能调优与权衡)
- [隐私与数据安全](#隐私与数据安全)
- [断点续跑机制](#断点续跑机制)
- [故障排查](#故障排查)
- [FAQ](#faq)

---

## 部署前必读

### 1. 模型名必须是服务商的真实模型名（最重要）

本项目所有 LLM 调用走 OpenAI 兼容协议，`LLM_MODEL` / `LLM_MODEL_PRO` 填写的模型名**必须是服务商真实存在的名字**：

| 服务商 | 正确示例 | 说明 |
|---|---|---|
| DeepSeek | `deepseek-chat` / `deepseek-reasoner` | 官方真实模型名 |
| OpenAI | `gpt-4o` / `gpt-4o-mini` | |
| Ollama | `qwen2.5:14b` | `ollama list` 查看 |

⚠️ **为什么强调**：DeepSeek API 对无效模型名**不报错**，会静默返回空 content（HTTP 200）。本项目内置的断点校验会判定输出为空并重试，最终表现为"任务极慢或失败"——我们曾在 800KB PDF 上实测 12 分钟进度不足 20%，根因就是历史版本默认值 `deepseek-v4-flash` 是虚构模型名。客户端已加入空 content 诊断日志，但填对名字永远是最好的解法。

### 2. 三种部署形态

```bash
# 形态一：pip 安装后 CLI / SDK 使用（最轻量）
pip install -e .
kbrefiner process document.pdf -o result.json

# 形态二：本地 Web UI（推荐个人使用）
kbrefiner serve            # 打开 http://localhost:8000

# 形态三：Docker（推荐团队/服务器部署，数据卷持久化）
docker compose up -d
```

### 3. MinerU 是可选依赖

- `PARSER_BACKEND=auto`（默认）：检测到 MinerU 则用之，否则回退轻量解析器
- 轻量解析器（pypdf + python-docx）**无法处理扫描件 PDF（无 OCR）**，复杂表格/公式还原效果弱于 MinerU
- 对 PDF 质量要求高时：`pip install mineru` 或使用 docker-compose 的 mineru profile

### 4. 必填配置只有一个

`.env` 中 `LLM_API_KEY` 必填，其余均有合理默认值。完整配置项说明见 [CONFIGURATION.md](./CONFIGURATION.md)。

---

## 已知坑与规避方法

以下均来自真实开发与实测过程，按严重程度排列：

### 坑 1：DeepSeek 专有特殊 token 污染

**现象**：使用 OpenAI SDK 调 DeepSeek 时，若 prompt 中混入 `<｜begin▁of▁sentence｜>` 等 DeepSeek 专有 token（全角分隔符），输出会异常或为空。

**规避**：不要手工拼接 DeepChat/DeepSeek 网页版复制的 token；本项目 prompt 均为纯文本模板，无此问题。

### 坑 2：JSON Output 模式返回空 content

**现象**：`response_format={'type': 'json_object'}` 在 DeepSeek 上偶发返回空 content。

**规避**：本项目已改用 normal 模式 + 手动 JSON 解析（提取首个 `{...}` 块），使用者无需处理；若自行改造客户端，勿改回 JSON 模式。

### 坑 3：并发过高触发 API 限流

**现象**：Stage 1/2/3/4 均有并行机制（默认并发 4）。把并发调到 8+ 时，DeepSeek 可能返回 429。

**规避**：默认值 4 是 DeepSeek 实测安全值；使用其他服务商时按其限流额度调整（见 [CONFIGURATION.md](./CONFIGURATION.md) 性能参数节）。

### 坑 4：换模型后断点文件仍是旧模型产出

**现象**：断点续跑会复用 `output_dir` 下的 `stage*.json`。更换 LLM 模型后重跑同一任务，命中断点的阶段仍是旧模型的结果。

**规避**：换模型后需全量重跑时，删除任务输出目录或调用 `pipeline.clear_checkpoints()`；Web 端重新上传同一文件即可全量重跑（每次上传生成新任务目录）。

### 坑 5：本地小模型的 max_tokens 截断

**现象**：默认 `max_tokens=16384`，部分本地小模型上下文不足时会静默截断输出 → JSON 校验失败 → 重试 → 失败。

**规避**：Ollama 小模型场景下调 `max_tokens`（如 4096），或换更大参数模型。

### 坑 6：遗留死配置 DATABASE_URL

`config.py` 中的 `DATABASE_URL` 为架构演进遗留项，**当前未生效**——任务库实际固定为 `./data/tasks.db`（SQLite）。不要试图通过它改数据库位置，改 `data/` 目录挂载即可。

---

## 性能调优与权衡

> 下表中的分段/并行参数**不读 .env**，修改位置与代码示例见 [CONFIGURATION.md 的自助调整指引](./CONFIGURATION.md#自助调整指引b--c-层参数修改位置)——Web 部署改 `routes.py` 两处 `PipelineConfig` 构造，SDK 用户直接构造 `Pipeline` 传参，新任务即生效。

### 实测基线（800KB PDF，DeepSeek-chat，2026-08）

| 优化状态 | 总耗时 |
|---|---|
| 串行 + 无效模型名（反面教材） | 12 分钟未完成 20% |
| 修复模型名 + Stage 3/4 分批并行 | 2 分 13 秒 |
| + Stage 1 分段并行 | 1 分 49 秒 |
| + Stage 2 分段并行 | **1 分 36 秒** |

### 核心权衡：速度 vs 语义完整性

四个 Stage 均已并行化，但并行依赖**切段**，切段有代价：

| 参数 | 调小（更细分段） | 调大 / 关闭（更粗分段） |
|---|---|---|
| `stage1_segment_chars` | 更快；但跨段术语一致性检测可能漏报 | 更慢；全文术语/敏感扫描更完整 |
| `stage2_segment_chars` | 更快；但段边界处相邻小块**无法跨段合并**，chunk 粒度变细（实测 10 → 15 个） | 更慢；分块更贴近整篇语义 |
| `stage34_batch_size` | 单次调用上下文小、输出短、不易截断 | 调用次数少；但单次输出长，小模型易截断 |
| `stage*_concurrency` | 限流风险低 | 更快；但可能触发 429 限流 |

**建议起点**：保持默认（8000 / 4 / 4 / 4）。追求分块质量可把 `stage2_segment_chars` 调到 12000+ 或设 0（关闭 Stage 2 分段，接受该阶段耗时约 +40%）。

### 质量相关的固定行为

- `temperature=0`：流水线场景默认确定性输出。若希望 Stage 3 的口语化 QA 更多样的，目前需改代码（`LLMConfig.temperature`），见 [CONFIGURATION.md](./CONFIGURATION.md)
- `doc_type` 判定：Stage 1 分段后采用**各段多数表决**（平票取第一段），分段过细时类型判定可能受局部内容影响
- chunk 粒度：Stage 2 目标区间 300~800 字，偏离会登记到 `exception_list.chunk_anomalies`

---

## 隐私与数据安全

| 关注点 | 实情 |
|---|---|
| 文档原文是否离开本机 | **是**——流水线需将文档内容发送到你所配置的 LLM API |
| 敏感数据保护机制 | Stage 1 识别敏感数据（手机号/密码/密钥等）并标记，Stage 3 生成的 QA 答案同步脱敏为【敏感数据】占位符 |
| 完全离线方案 | 配置 Ollama（`LLM_BASE_URL=http://localhost:11434/v1`），全流程不出内网 |
| 数据落盘位置 | 上传原件 `data/uploads/`、中间与最终结果 `data/outputs/{task_id}/`、任务库 `data/tasks.db` |
| Docker 持久化 | 统一挂载在 `kbrefiner-data` 卷，`docker compose down` 不丢数据（`down -v` 才删除） |

> 提醒：即有脱敏机制，**LLM 服务商仍能看到原文**。高敏文档请使用本地模型。

---

## 断点续跑机制

- 每个 Stage 成功后落盘 `stage{N}.json` 到任务输出目录；失败重跑时命中断点的阶段跳过 LLM 调用
- CLI：默认开启，`--no-checkpoint` 关闭
- Web：固定开启；服务重启后遗留的 `processing` 任务会自动标记为 failed（提示"重新发起可从断点续跑"），对**同一文件重新发起处理**即从断点继续
- 中间产物同时是排障依据：哪个 `stage{N}.json` 缺失，就说明卡在哪个阶段

---

## 故障排查

| 症状 | 排查方向 |
|---|---|
| 任务极慢、日志大量"空 content 重试" | 模型名无效（见部署前必读第 1 条）；`/health` 返回的 `llm_model` 可快速核对 |
| 单个 Stage 反复校验失败 | 看 `data/outputs/{task_id}/` 缺哪个 stage 文件；把该 stage 的 raw 输出与 `.env` 的模型对照（小模型截断常见） |
| Web 列表任务一直 processing | 服务是否重启过（重启自动标 failed）；若未重启，看 uvicorn 日志 |
| PDF 解析结果是空文本 | 扫描件 PDF 无 OCR（换 MinerU）；或加密 PDF |
| 429 / Rate limit 报错 | 调低 `stage*_concurrency` |
| 上传 400 "文件大小超过限制" | `MAX_UPLOAD_SIZE_MB`（默认 50） |
| 导出 CSV Excel 打开乱码 | 已内置 UTF-8-BOM，若仍乱码确认文件未被二次转存为 ANSI |

---

## FAQ

**Q：支持哪些文件格式？**
PDF / DOCX / PPTX / XLSX / 图片（PNG/JPG/BMP，图片解析需 MinerU）/ Markdown / TXT。

**Q：处理结果怎么导入 RAG 平台？**
六种导出格式：`coze_qa` / `coze_text`（扣子表格）、`dify_qa` / `dify_text` / `dify_jsonl`（Dify）、`json`（通用）。Web 端在任务完成页选择格式下载；CLI 用 `--format` 参数。

**Q：多文档批量处理？**
CLI/SDK 逐个调用即可（各自独立任务目录互不干扰）；`partition_prefix`（P1/P2/P3...）用于多分片场景的 chunk_id 前缀隔离，Web 单文档场景无需关心。

**Q：换一家 LLM 服务商要改什么？**
`.env` 里改 `LLM_BASE_URL` + `LLM_API_KEY` + 模型名，重启服务。注意并发参数按新服务商限流额度复核。

**Q：测试会污染我的任务数据吗？**
0.3.0 起测试套件已隔离任务库到临时目录。若使用旧版本跑过测试，`data/tasks.db` 里可能有假任务（file_size 很小的 policy.pdf/test.pdf 条目），可手动清理。

---

*文档基于 v0.3.x（2026-08）实测整理，配置项明细见 [CONFIGURATION.md](./CONFIGURATION.md)。*
