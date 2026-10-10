"""端到端与可复现性测试。

包含三组：

1. **CLI 契约** —— 子命令可调用、退出码语义正确；
2. **产物完整性** —— 管道跑完后关键文件必须存在且非空；
3. **可复现性** —— 同一随机种子必须逐字节产出相同的数据；改变种子必须产出不同的数据。
   这是"可复现研究"的硬性要求，也是本仓库能被他人信任的基础。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from qingfeng.cli import main as cli_main


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_info_runs(self, cfg, capsys):
        rc = cli_main(["--config", str(cfg.source_path), "info"])
        assert rc == 0
        out = capsys.readouterr().out
        assert cfg.org_name in out
        assert "数据窗口" in out

    def test_sql_list_runs(self, cfg):
        assert cli_main(["--config", str(cfg.source_path), "sql"]) == 0

    @pytest.mark.parametrize("ref", ["03_kpi_queries.sql", "sql/03_kpi_queries.sql"])
    def test_sql_file_path_resolution(self, cfg, ref):
        """回归测试：``--file`` 既支持相对 sql/ 的写法，也支持相对仓库根的写法。

        CI 曾因只做一次路径拼接，把 ``sql/03_kpi_queries.sql`` 解析成
        ``sql/sql/03_kpi_queries.sql`` 而失败。
        """
        from qingfeng.warehouse import resolve_sql_path

        p = resolve_sql_path(cfg, ref)
        assert p.is_file(), f"{ref} 未能解析到真实文件"
        assert p.name == "03_kpi_queries.sql"

    def test_run_sql_file_executes_all_statements(self, cfg):
        from qingfeng.warehouse import run_sql_file

        results = run_sql_file(cfg, "sql/03_kpi_queries.sql")
        assert len(results) >= 15
        for label, df in results:
            assert "error" not in df.columns, f"语句执行失败：{label} -> {df.to_dict()}"

    def test_verify_exit_code_semantics(self, cfg):
        from qingfeng.pipeline import verify_pipeline

        result = verify_pipeline(cfg)
        assert isinstance(result["checks"], pd.DataFrame)
        assert len(result["checks"]) >= 8
        assert set(result["checks"]["结果"]) <= {"PASS", "FAIL"}

    def test_bad_config_returns_error_code(self, tmp_path, capsys):
        bad = tmp_path / "config" / "config.yaml"
        bad.parent.mkdir(parents=True)
        bad.write_text("project: {}\n", encoding="utf-8")
        rc = cli_main(["--config", str(bad), "info"])
        assert rc == 2
        assert "配置错误" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 产物完整性
# ---------------------------------------------------------------------------


EXPECTED_ARTIFACTS = [
    "reports/数据分析报告.md",
    "reports/executive_summary.md",
    "reports/dashboard.html",
    "reports/kpi_catalog.csv",
    "reports/run_manifest.json",
    "data/processed/validation_report.csv",
    "data/processed/cleaning_log.csv",
    "data/processed/kpi_summary.csv",
    "data/processed/star_schema_summary.csv",
    "data/processed/monthly_kpi_panel.csv",
    "data/processed/anomaly_monthly.csv",
    "data/processed/project_scorecard.csv",
    "docs/data_dictionary.md",
]


class TestArtifacts:
    @pytest.mark.parametrize("rel", EXPECTED_ARTIFACTS)
    def test_artifact_exists_and_nonempty(self, repo_root, rel):
        p = repo_root / rel
        if not p.is_file():
            pytest.skip(f"{rel} 尚未生成（先运行 make pipeline）")
        assert p.stat().st_size > 0, f"{rel} 为空文件"

    def test_report_contains_figures(self, repo_root):
        p = repo_root / "reports" / "数据分析报告.md"
        if not p.is_file():
            pytest.skip("报告尚未生成")
        text = p.read_text(encoding="utf-8")
        assert "figures/" in text
        assert "## 一、执行摘要" in text
        assert "## 十、方法与局限声明" in text

    def test_figures_rendered(self, cfg):
        figs = sorted(cfg.paths.figures.glob("*.png"))
        if not figs:
            pytest.skip("图表尚未生成")
        assert len(figs) >= 10
        for f in figs:
            assert f.stat().st_size > 5000, f"{f.name} 可能是空白图"

    def test_run_manifest_is_valid_json(self, cfg):
        p = cfg.paths.reports / "run_manifest.json"
        if not p.is_file():
            pytest.skip("manifest 尚未生成")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["seed"] == cfg.simulation.seed
        assert data["kpi_count"] >= 30
        assert "star_tables" in data


# ---------------------------------------------------------------------------
# 可复现性
# ---------------------------------------------------------------------------


def _tiny_config(base: Path, tmp: Path) -> Path:
    """构造一份规模很小、但结构完整的配置，用于快速复现性验证。"""
    raw = yaml.safe_load((base / "config" / "config.yaml").read_text(encoding="utf-8"))
    raw["simulation"].update(
        {"n_volunteers": 240, "n_projects": 14, "n_beneficiaries": 320,
         "n_donors": 420, "n_staff": 8}
    )
    raw["paths"] = {
        "raw": str(tmp / "data" / "raw"),
        "interim": str(tmp / "data" / "interim"),
        "processed": str(tmp / "data" / "processed"),
        "external": str(tmp / "data" / "external"),
        "reports": str(tmp / "reports"),
        "figures": str(tmp / "reports" / "figures"),
        "warehouse": str(tmp / "data" / "processed" / "tiny.sqlite"),
    }
    cfg_dir = tmp / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _run_generator(cfg_path: Path, out_dir: Path, seed: int, repo_root: Path) -> None:
    script = repo_root / "scripts" / "generate_synthetic_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(cfg_path),
         "--seed", str(seed), "--out", str(out_dir)],
        capture_output=True, text=True, check=False, cwd=str(repo_root),
    )
    assert result.returncode == 0, result.stderr[-2000:]


def _hash_dir(d: Path) -> dict[str, str]:
    out = {}
    for f in sorted(d.glob("*.csv")):
        out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    return out


class TestReproducibility:
    def test_same_seed_same_bytes(self, repo_root, tmp_path):
        cfg_path = _tiny_config(repo_root, tmp_path / "a")
        out1 = tmp_path / "a" / "data" / "raw"
        out2 = tmp_path / "a" / "raw2"
        _run_generator(cfg_path, out1, 12345, repo_root)
        _run_generator(cfg_path, out2, 12345, repo_root)
        h1, h2 = _hash_dir(out1), _hash_dir(out2)
        assert h1, "生成器未产出任何 CSV"
        assert h1 == h2, "同一随机种子下产出不一致，可复现性被破坏"

    def test_different_seed_different_bytes(self, repo_root, tmp_path):
        cfg_path = _tiny_config(repo_root, tmp_path / "b")
        out1 = tmp_path / "b" / "data" / "raw"
        out2 = tmp_path / "b" / "raw2"
        _run_generator(cfg_path, out1, 1, repo_root)
        _run_generator(cfg_path, out2, 2, repo_root)
        h1, h2 = _hash_dir(out1), _hash_dir(out2)
        assert h1 != h2, "不同随机种子产出完全相同，说明种子未生效"

    def test_manifest_records_seed_and_disclaimer(self, repo_root, tmp_path):
        cfg_path = _tiny_config(repo_root, tmp_path / "c")
        out = tmp_path / "c" / "data" / "raw"
        _run_generator(cfg_path, out, 777, repo_root)
        manifest = json.loads((out / "_MANIFEST.json").read_text(encoding="utf-8"))
        assert manifest["seed"] == 777
        assert "模拟数据" in manifest["disclaimer"]
        assert manifest["tables"], "manifest 未记录任何表"
        for t in manifest["tables"]:
            assert t["rows"] > 0
            assert len(t["sha256_16"]) == 16

    def test_ground_truth_recorded(self, repo_root, tmp_path):
        cfg_path = _tiny_config(repo_root, tmp_path / "d")
        out = tmp_path / "d" / "data" / "raw"
        _run_generator(cfg_path, out, 99, repo_root)
        gt_dir = out / "_ground_truth"
        assert (gt_dir / "injected_defects.csv").is_file()
        assert (gt_dir / "planted_anomalies.csv").is_file()
        defects = pd.read_csv(gt_dir / "injected_defects.csv", encoding="utf-8-sig")
        assert len(defects) > 0
        assert {"table", "row_position", "column", "defect_type"} <= set(defects.columns)
        assert defects["defect_type"].nunique() >= 4


# ---------------------------------------------------------------------------
# 管道冒烟（默认跳过，用 `pytest -m slow` 显式开启）
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestPipelineSmoke:
    def test_pipeline_runs_end_to_end(self, repo_root, tmp_path):
        cfg_path = _tiny_config(repo_root, tmp_path / "e")
        from qingfeng.config import load_config
        from qingfeng.pipeline import run_pipeline

        cfg = load_config(cfg_path, ensure_dirs=True)
        result = run_pipeline(cfg, regenerate=True, skip_figures=True)
        assert len(result["kpis"]) >= 30
        assert len(result["star"]) == 11
        assert result["reports"]["report_md"].is_file()
        assert result["reports"]["dashboard"].is_file()
