"""pytest 共享夹具。

设计原则
--------
* **会话级缓存**：整条管道很重，测试会话内只跑一次，所有测试复用同一份产物。
* **自动引导**：如果 `data/processed` 不存在（例如刚 clone），夹具会自动触发一次
  完整管道，因此 `pytest` 在干净仓库上也能一次通过。
* **可降级**：需要网络的测试一律不写；依赖外网或可选包的断言会被跳过而不是失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qingfeng.config import load_config  # noqa: E402
from qingfeng.logging_utils import setup_logging  # noqa: E402

setup_logging("WARNING")


# ---------------------------------------------------------------------------
# 基础夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.fixture(scope="session")
def raw_data(cfg) -> dict[str, pd.DataFrame]:
    """原始（含缺陷的）数据。"""
    from qingfeng.tables import ALL_TABLES

    if not (cfg.paths.raw / "_MANIFEST.json").is_file():
        pytest.skip("缺少原始数据，请先运行 `make data`")
    return {name: pd.read_csv(cfg.paths.raw_file(f"{name}.csv"), encoding="utf-8-sig", low_memory=False)
            for name in ALL_TABLES}


@pytest.fixture(scope="session")
def cleaned_data(cfg) -> dict[str, pd.DataFrame]:
    """清洗后的中间数据，缺失时现场生成。"""
    from qingfeng.clean import run_cleaning, write_cleaned
    from qingfeng.tables import ALL_TABLES

    path = cfg.paths.interim_file(f"{ALL_TABLES[0]}_clean.csv")
    if not path.is_file():
        cleaned, log = run_cleaning(cfg)
        write_cleaned(cfg, cleaned, log)
        return cleaned

    from qingfeng.clean import load_cleaned

    return load_cleaned(cfg)


@pytest.fixture(scope="session")
def star(cfg, cleaned_data) -> dict[str, pd.DataFrame]:
    """星型模型，缺失时现场构建。"""
    from qingfeng.transform import build_star_schema, load_star_schema, write_star_schema

    try:
        return load_star_schema(cfg)
    except FileNotFoundError:
        schema = build_star_schema(cfg, cleaned_data)
        write_star_schema(cfg, schema)
        return schema


@pytest.fixture(scope="session")
def cleaning_log(cfg, cleaned_data):
    from qingfeng.clean import run_cleaning, CleaningLog

    path = cfg.paths.processed_file("cleaning_log.csv")
    if path.is_file():
        from qingfeng.clean import CleaningLog as _CL

        log = _CL()
        df = pd.read_csv(path, encoding="utf-8-sig")
        for _, r in df.iterrows():
            log.add(str(r["table"]), str(r["column"]), str(r["issue"]), str(r["action"]),
                    int(r["affected_rows"]), str(r.get("note", "")))
        return log
    _cleaned, log = run_cleaning(cfg)
    return log


@pytest.fixture(scope="session")
def kpis(cfg, star, cleaned_data) -> pd.DataFrame:
    from qingfeng.metrics import compute_kpis

    return compute_kpis(cfg, star, cleaned_data)


@pytest.fixture(scope="session")
def analysis(cfg, star) -> dict:
    from qingfeng.analyze import run_analysis

    return run_analysis(cfg, star)


@pytest.fixture(scope="session")
def anomaly_result(cfg, star, analysis) -> dict:
    from qingfeng.anomaly import run_anomaly_detection

    return run_anomaly_detection(cfg, star, analysis["monthly_panel"])
