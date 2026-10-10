# =============================================================================
# 迪庆清风青年公益联合会 · 公益项目数据运营分析
# 统一入口：make <目标>。所有目标都通过 qingfeng CLI 调用，保证与 CI 行为一致。
# =============================================================================

SHELL       := /bin/bash
PYTHON      ?= python3
ROOT        := $(shell cd "$(dir $(lastword $(MAKEFILE_LIST)))" && pwd)
export PYTHONPATH := $(ROOT)/src
export QINGFENG_CONFIG ?= $(ROOT)/config/config.yaml
PY          := $(PYTHON) -m qingfeng.cli

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------
.PHONY: help
help: ## 显示所有可用目标
	@echo "迪庆清风青年公益联合会 · 公益项目数据运营分析"
	@echo ""
	@echo "用法：make <目标>"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "环境：PYTHON=$(PYTHON)  ROOT=$(ROOT)"

# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
.PHONY: setup
setup: ## 安装运行依赖与开发依赖
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	@echo "✔ 依赖安装完成。若中文字体缺失，图表中文可能显示为方块。"

# ---------------------------------------------------------------------------
# 管道各阶段（可单独执行，便于调试）
# ---------------------------------------------------------------------------
.PHONY: data
data: ## 1. 生成模拟多源数据（含刻意注入的数据缺陷与 ground truth）
	$(PY) data

.PHONY: pipeline
pipeline: ## 端到端运行：清洗 -> 校验 -> 建模 -> 建库 -> 指标 -> 分析 -> 异常 -> 图表 -> 报告
	$(PY) pipeline

.PHONY: no-regen
no-regen: ## 复用已有 raw 数据，仅重跑下游（调试用，快得多）
	$(PY) pipeline --no-data

.PHONY: fast
fast: ## 跳过图表生成的快速全流程（CI 用）
	$(PY) pipeline --skip-figures

# ---------------------------------------------------------------------------
# 质检
# ---------------------------------------------------------------------------
.PHONY: test
test: ## 运行单元测试与一致性测试
	$(PYTHON) -m pytest -q

.PHONY: coverage
coverage: ## 运行测试并输出覆盖率报告
	$(PYTHON) -m pytest -q --cov=qingfeng --cov-report=term-missing --cov-report=html

.PHONY: lint
lint: ## 代码风格检查（ruff）
	$(PYTHON) -m ruff check src scripts tests || true

.PHONY: lint-fix
lint-fix: ## 自动修复可修复的风格问题
	$(PYTHON) -m ruff check --fix src scripts tests || true

.PHONY: verify
verify: ## 只读体检：检查产物是否齐备、外键是否自洽
	$(PY) verify

.PHONY: docs
docs: ## 从实际产物重新生成数据字典（docs/data_dictionary.md）
	$(PYTHON) scripts/build_docs.py --config "$(QINGFENG_CONFIG)"

.PHONY: sql
sql: ## 列出 SQL 文件并打印数据仓库概览
	$(PY) sql

.PHONY: sql-all
sql-all: ## 执行 sql/ 下的全部查询并打印结果
	@for f in sql/03_kpi_queries.sql sql/04_analysis_queries.sql; do \
		echo ""; echo "############ $$f ############"; \
		$(PY) sql --file $$f; \
	done

.PHONY: info
info: ## 打印当前配置、数据窗口与产物概况
	$(PY) info

# ---------------------------------------------------------------------------
# 组合目标
# ---------------------------------------------------------------------------
.PHONY: all
all: pipeline test verify ## 完整构建 + 测试 + 体检

.PHONY: ci
ci: pipeline test verify ## CI 中执行的目标（与 all 一致，便于语义区分）

.PHONY: notebook
notebook: ## 启动 Jupyter Lab 浏览 notebooks/
	$(PYTHON) -m jupyter lab notebooks

.PHONY: dashboard
dashboard: ## 打开生成的 HTML 仪表盘（macOS）
	@open reports/dashboard.html 2>/dev/null || echo "请手动打开 reports/dashboard.html"

# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------
.PHONY: clean
clean: ## 清理派生数据与报告（保留 data/raw 与 data/external）
	rm -rf data/interim/* data/processed/* reports/figures/*
	@echo "✔ 已清理中间产物与图表。运行 make pipeline 可重新生成。"

.PHONY: clean-all
clean-all: clean ## 连原始数据一并清理（完全重建）
	rm -rf data/raw/*
	@echo "✔ 已清理全部数据。运行 make pipeline 可完全重建。"
