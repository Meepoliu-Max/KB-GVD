.PHONY: install dev test lint format serve help

install: ## 安装包（开发模式）
	pip install -e ".[dev]"

install-mineru: ## 安装 MinerU（可选，需单独安装）
	pip install mineru

dev: ## 启动开发服务器（热重载）
	uvicorn kbrefiner.main:app --reload --host 0.0.0.0 --port 8000

serve: ## 启动生产服务器
	uvicorn kbrefiner.main:app --host 0.0.0.0 --port 8000

test: ## 运行测试
	python -m pytest tests/ -v

lint: ## 代码检查
	ruff check kbrefiner/ tests/

format: ## 代码格式化
	ruff format kbrefiner/ tests/

clean: ## 清理缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -f data/*.db 2>/dev/null || true

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
