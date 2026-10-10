"""配置加载：读取 ``config/config.yaml``，解析仓库根目录与全部输出路径。

设计要点
--------
* 使用不可变（frozen）的 ``dataclass`` 承载配置，避免管道运行期间被意外修改。
* 所有路径统一解析为**绝对路径**，因此无论从仓库根目录、notebook 还是 CI 中调用，
  行为都完全一致。
* 通过环境变量 ``QINGFENG_CONFIG`` 可以指向另一份配置，便于做参数敏感性实验。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_ENV = "QINGFENG_CONFIG"
DEFAULT_CONFIG_RELPATH = Path("config") / "config.yaml"


class ConfigError(RuntimeError):
    """配置缺失或非法时抛出。"""


def find_repo_root(start: Path | None = None) -> Path:
    """向上回溯查找包含 ``config/config.yaml`` 的仓库根目录。"""
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / DEFAULT_CONFIG_RELPATH).is_file():
            return candidate
    # 回退：以本文件位置推断（src/qingfeng/config.py -> 仓库根）
    guess = Path(__file__).resolve().parents[2]
    if (guess / DEFAULT_CONFIG_RELPATH).is_file():
        return guess
    raise ConfigError(
        f"未找到 {DEFAULT_CONFIG_RELPATH}；可从仓库根目录运行，或设置环境变量 "
        f"{DEFAULT_CONFIG_ENV} 指向配置文件。"
    )


@dataclass(frozen=True)
class Paths:
    """仓库内全部输入输出路径（绝对路径）。"""

    root: Path
    raw: Path
    interim: Path
    processed: Path
    external: Path
    reports: Path
    figures: Path
    sql: Path
    warehouse: Path

    def ensure(self) -> Paths:
        """创建所有输出目录。"""
        for directory in (self.raw, self.interim, self.processed, self.external, self.reports, self.figures):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def raw_file(self, name: str) -> Path:
        return self.raw / name

    def interim_file(self, name: str) -> Path:
        return self.interim / name

    def processed_file(self, name: str) -> Path:
        return self.processed / name

    def figure(self, name: str) -> Path:
        return self.figures / name


@dataclass(frozen=True)
class SimulationConfig:
    seed: int
    start_date: str
    as_of_date: str
    n_volunteers: int
    n_projects: int
    n_beneficiaries: int
    n_donors: int
    n_staff: int
    active_volunteer_rate: tuple[float, float]
    shift_hours_lognorm: dict[str, Any]
    shifts_per_active_volunteer: dict[str, Any]
    donation_lognorm: dict[str, Any]
    volunteer_hour_value_cny: float


@dataclass(frozen=True)
class DataQualityConfig:
    duplicate_rate: float
    missing_rate: float
    malformed_date_rate: float
    unit_inconsistency_rate: float
    outlier_rate: float
    orphan_key_rate: float
    whitespace_rate: float


@dataclass(frozen=True)
class VizConfig:
    dpi: int
    figure_format: str
    palette: str
    colors: dict[str, str]
    font_candidates: list[str]
    figsize: dict[str, list[int]]


@dataclass(frozen=True)
class Config:
    """顶层配置对象。"""

    raw: dict[str, Any] = field(repr=False)
    paths: Paths
    project: dict[str, Any]
    simulation: SimulationConfig
    data_quality: DataQualityConfig
    analysis: dict[str, Any]
    viz: VizConfig
    source_path: Path

    # ---- 便捷访问器 -------------------------------------------------------
    @property
    def region(self) -> str:
        return self.project.get("region", "")

    @property
    def org_name(self) -> str:
        return self.project.get("name", "")

    def threshold(self, kpi: str) -> dict[str, float]:
        return self.analysis.get("kpi", {}).get("thresholds", {}).get(kpi, {})

    def color(self, key: str) -> str:
        return self.viz.colors.get(key, "#333333")

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def _build(raw: dict[str, Any], source_path: Path, root: Path) -> Config:
    required = {"paths", "simulation", "project", "analysis", "viz", "data_quality"}
    missing = required - set(raw)
    if missing:
        raise ConfigError(f"配置文件缺少必需章节: {sorted(missing)}")

    p = raw["paths"]
    paths = Paths(
        root=root,
        raw=root / p["raw"],
        interim=root / p["interim"],
        processed=root / p["processed"],
        external=root / p["external"],
        reports=root / p["reports"],
        figures=root / p["figures"],
        sql=root / "sql",
        warehouse=root / p["warehouse"],
    )

    s = raw["simulation"]
    simulation = SimulationConfig(
        seed=int(s["seed"]),
        start_date=str(s["start_date"]),
        as_of_date=str(s["as_of_date"]),
        n_volunteers=int(s["n_volunteers"]),
        n_projects=int(s["n_projects"]),
        n_beneficiaries=int(s["n_beneficiaries"]),
        n_donors=int(s["n_donors"]),
        n_staff=int(s["n_staff"]),
        active_volunteer_rate=tuple(s["active_volunteer_rate"]),  # type: ignore[arg-type]
        shift_hours_lognorm=dict(s["shift_hours_lognorm"]),
        shifts_per_active_volunteer=dict(s["shifts_per_active_volunteer"]),
        donation_lognorm=dict(s["donation_lognorm"]),
        volunteer_hour_value_cny=float(s["volunteer_hour_value_cny"]),
    )

    dq = raw["data_quality"]
    data_quality = DataQualityConfig(
        duplicate_rate=float(dq["duplicate_rate"]),
        missing_rate=float(dq["missing_rate"]),
        malformed_date_rate=float(dq["malformed_date_rate"]),
        unit_inconsistency_rate=float(dq["unit_inconsistency_rate"]),
        outlier_rate=float(dq["outlier_rate"]),
        orphan_key_rate=float(dq["orphan_key_rate"]),
        whitespace_rate=float(dq["whitespace_rate"]),
    )

    v = raw["viz"]
    viz = VizConfig(
        dpi=int(v["dpi"]),
        figure_format=str(v["figure_format"]),
        palette=str(v["palette"]),
        colors=dict(v["colors"]),
        font_candidates=list(v["font_candidates"]),
        figsize={k: list(val) for k, val in v["figsize"].items()},
    )

    return Config(
        raw=raw,
        paths=paths,
        project=dict(raw["project"]),
        simulation=simulation,
        data_quality=data_quality,
        analysis=dict(raw["analysis"]),
        viz=viz,
        source_path=source_path,
    )


def load_config(path: str | Path | None = None, *, ensure_dirs: bool = False) -> Config:
    """加载配置。``path`` 为空时依次尝试环境变量与仓库根目录。"""
    if path is None:
        env_path = os.environ.get(DEFAULT_CONFIG_ENV)
        if env_path:
            source = Path(env_path).expanduser().resolve()
            root = source.parent.parent if source.parent.name == "config" else find_repo_root()
        else:
            root = find_repo_root()
            source = root / DEFAULT_CONFIG_RELPATH
    else:
        source = Path(path).expanduser().resolve()
        root = source.parent.parent if source.parent.name == "config" else find_repo_root(source.parent)

    if not source.is_file():
        raise ConfigError(f"配置文件不存在: {source}")

    with source.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层结构应为映射: {source}")

    cfg = _build(raw, source, root)
    if ensure_dirs:
        cfg.paths.ensure()
    return cfg


@lru_cache(maxsize=4)
def get_config(path: str | None = None) -> Config:
    """带缓存的配置访问器（供 notebook 与 CLI 共用）。"""
    return load_config(path)
