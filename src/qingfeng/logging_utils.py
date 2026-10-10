"""统一日志与运行计时工具。

管道中每个阶段都通过 :func:`stage` 上下文管理器输出结构化日志，
便于在 CI 日志里快速定位是哪一步失败、耗时多少。
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

LOGGER_NAME = "qingfeng"
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"


def setup_logging(level: int | str = logging.INFO, *, quiet: bool = False) -> logging.Logger:
    """初始化（幂等的）根日志器。"""
    logger = logging.getLogger(LOGGER_NAME)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT))
        logger.addHandler(handler)
    if quiet:
        logger.setLevel(logging.WARNING)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


@dataclass
class RunTimer:
    """记录各阶段耗时，最终汇总为一份可打印的运行报告。"""

    records: list[tuple[str, float, str]] = field(default_factory=list)

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        logger = get_logger(name)
        logger.info("▶ 开始：%s", name)
        t0 = time.perf_counter()
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - 需要记录后重新抛出
            elapsed = time.perf_counter() - t0
            self.records.append((name, elapsed, f"FAILED: {type(exc).__name__}"))
            logger.error("✖ 失败：%s（%.2fs）%s: %s", name, elapsed, type(exc).__name__, exc)
            raise
        else:
            elapsed = time.perf_counter() - t0
            self.records.append((name, elapsed, "ok"))
            logger.info("✔ 完成：%s（%.2fs）", name, elapsed)

    @property
    def total_seconds(self) -> float:
        return sum(r[1] for r in self.records)

    def summary(self) -> str:
        width = max((len(r[0]) for r in self.records), default=10)
        lines = ["", "=" * (width + 26), f"{'阶段'.ljust(width)}   耗时(s)   状态", "-" * (width + 26)]
        for name, elapsed, status in self.records:
            lines.append(f"{name.ljust(width)}   {elapsed:8.2f}   {status}")
        lines.append("-" * (width + 26))
        lines.append(f"{'合计'.ljust(width)}   {self.total_seconds:8.2f}")
        lines.append("=" * (width + 26))
        return "\n".join(lines)


def log_dataframe(logger: logging.Logger, title: str, df: Any, n: int = 5) -> None:
    """以紧凑形式把 DataFrame 摘要写入日志。"""
    try:
        shape = getattr(df, "shape", None)
        logger.info("%s | shape=%s", title, shape)
        if shape and shape[0]:
            logger.info("\n%s", df.head(n).to_string(max_cols=12))
    except Exception:  # pragma: no cover - 仅日志兜底
        logger.info("%s | <无法渲染>", title)
