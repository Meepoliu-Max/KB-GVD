# 贡献指南

感谢你对 KBRefiner 的关注！本文档说明如何参与项目开发。

## 开发环境

```bash
git clone https://github.com/your-org/kbrefiner.git
cd kbrefiner
pip install -e ".[dev]"
```

Python >= 3.10。

## 开发流程

1. Fork 仓库并创建分支：`git checkout -b feature/your-feature`
2. 编写代码，确保通过测试：`make test`
3. 代码风格检查：`make lint`
4. 提交 PR，描述改动内容和动机

## 代码规范

- 使用 ruff 进行 lint 和 format（`make lint` / `make format`）
- 行宽 100 字符
- 新增功能需配套测试（`tests/` 目录下）
- Pydantic 模型字段需有 description

## 测试

```bash
# 全量测试
make test

# 单个测试文件
python -m pytest tests/test_prompts.py -v

# 带覆盖率
python -m pytest tests/ --cov=kbrefiner --cov-report=term-missing
```

## 项目结构要点

- `kbrefiner/core/pipeline/prompts/` — Jinja2 模板，修改后需同步更新 `tests/test_prompts.py`
- `kbrefiner/core/validation/` — 校验模块，修改后需同步更新 `tests/test_validation.py`
- `kbrefiner/core/exporter/` — 导出格式，新增格式需注册到 `_EXPORTERS` 字典
- `kbrefiner/models/schemas.py` — 数据模型变更需全链路验证（pipeline + validation + tests）

## 提交信息

使用简洁的提交信息，说明改动内容：

```
修复 Stage 3 问题前缀污染向量检索
新增覆盖率指标到 QualitySummary
```

## License

贡献的代码遵循 GPL-3.0-or-later 许可。
