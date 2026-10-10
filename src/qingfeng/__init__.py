"""迪庆清风青年公益联合会 · 公益项目数据运营分析工具包。

模块分层::

    config     -> 配置加载与路径解析
    logging_utils -> 统一日志与运行计时
    ingest     -> 多源异构数据接入
    clean      -> 数据清洗与标准化
    validate   -> 数据质量校验规则引擎
    transform  -> 清洗后数据 -> 星型模型
    metrics    -> 指标体系（KPI）计算
    analyze    -> 描述性统计 / 交叉分析 / 趋势分析
    anomaly    -> 多方法异常检测与归因
    viz        -> 可视化
    report     -> 分析报告生成
    pipeline   -> 端到端编排
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
