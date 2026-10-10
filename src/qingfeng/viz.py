"""可视化层：把分析结论画成图。

设计原则
--------
* **每张图回答一个业务问题**，标题直接写结论而不是"图 1：xx 分布"。
* 统一的中文字体回退链（macOS / Linux / Windows 三平台可用）、统一配色与栅格样式。
* 所有图函数签名都是 ``(cfg, ...) -> Path``，返回落盘路径，便于报告层引用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # 无显示环境（CI / 服务器）也能出图

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager, rcParams  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from qingfeng.config import Config  # noqa: E402
from qingfeng.logging_utils import get_logger  # noqa: E402

logger = get_logger("viz")

_FONT_READY = False


def setup_style(cfg: Config) -> None:
    """配置中文字体、配色与全局绘图风格（幂等）。"""
    global _FONT_READY
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [f for f in cfg.viz.font_candidates if f in available]
    if not chosen:
        chosen = ["DejaVu Sans"]
        logger.warning("未找到中文字体，中文可能显示为方块。可安装 Noto Sans CJK SC。")
    rcParams["font.sans-serif"] = chosen
    rcParams["axes.unicode_minus"] = False
    rcParams["figure.dpi"] = cfg.viz.dpi
    rcParams["savefig.dpi"] = cfg.viz.dpi
    rcParams["savefig.bbox"] = "tight"
    rcParams["figure.facecolor"] = "white"
    rcParams["axes.facecolor"] = "white"
    rcParams["axes.edgecolor"] = cfg.color("grid")
    rcParams["axes.grid"] = True
    rcParams["grid.color"] = cfg.color("grid")
    rcParams["grid.linewidth"] = 0.7
    rcParams["axes.axisbelow"] = True
    rcParams["axes.titlesize"] = 13
    rcParams["axes.titleweight"] = "bold"
    rcParams["axes.labelsize"] = 11
    rcParams["legend.frameon"] = False
    _FONT_READY = True


def _palette(cfg: Config, n: int) -> list[Any]:
    base = [
        cfg.color("primary"), cfg.color("secondary"), cfg.color("accent"),
        cfg.color("danger"), cfg.color("neutral"), "#4C8C8F", "#A8843C",
        "#9B6BAB", "#8C9BA1", "#D0996B",
    ]
    return [base[i % len(base)] for i in range(n)]


def _save(cfg: Config, fig: Any, name: str) -> Path:
    path = cfg.paths.figure(f"{name}.{cfg.viz.figure_format}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    logger.info("  已输出图表 %s", path.name)
    return path


def _wan(x: float, _pos: int = 0) -> str:
    if abs(x) >= 1e8:
        return f"{x / 1e8:.1f}亿"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.0f}万"
    return f"{x:.0f}"


WAN_FMT = FuncFormatter(_wan)


# ---------------------------------------------------------------------------
# 1. KPI 记分卡
# ---------------------------------------------------------------------------


def plot_kpi_scorecard(cfg: Config, kpis: pd.DataFrame) -> Path:
    """各层级指标达标情况速览：达标/关注/预警 三色堆叠条形。"""
    setup_style(cfg)
    df = kpis.copy()
    order = ["规模层", "活跃层", "效率层", "质量层", "结构层", "数据层"]
    df["指标层级"] = pd.Categorical(df["指标层级"], categories=order, ordered=True)
    pivot = (
        df.groupby(["指标层级", "状态"], observed=False).size().unstack(fill_value=0)
        .reindex(order, fill_value=0)
    )
    for col in ("达标", "关注", "预警", "—"):
        if col not in pivot.columns:
            pivot[col] = 0
    pivot = pivot[["达标", "关注", "预警", "—"]]

    fig, ax = plt.subplots(figsize=tuple(cfg.viz.figsize["wide"]))
    left = np.zeros(len(pivot))
    colors = {"达标": cfg.color("primary"), "关注": cfg.color("secondary"),
              "预警": cfg.color("danger"), "—": cfg.color("grid")}
    for col in pivot.columns:
        ax.barh(pivot.index, pivot[col], left=left, color=colors[col], label=col, height=0.62)
        for y, (v, l) in enumerate(zip(pivot[col], left, strict=True)):
            if v:
                ax.text(l + v / 2, y, str(int(v)), ha="center", va="center",
                        color="white", fontsize=10, fontweight="bold")
        left = left + pivot[col].to_numpy()
    ax.set_xlabel("指标个数")
    n_ok = int(pivot["达标"].sum())
    n_watch = int(pivot["关注"].sum())
    n_alert = int(pivot["预警"].sum())
    ax.set_title(
        f"指标体系健康度：{int(pivot.to_numpy().sum())} 项核心指标中 "
        f"{n_ok} 项达标 / {n_watch} 项关注 / {n_alert} 项预警"
    )
    ax.legend(loc="lower right", ncols=4)
    return _save(cfg, fig, "01_kpi_scorecard")


# ---------------------------------------------------------------------------
# 2. 月度趋势
# ---------------------------------------------------------------------------


def plot_monthly_trend(cfg: Config, panel: pd.DataFrame) -> Path:
    setup_style(cfg)
    df = panel.copy()
    x = pd.to_datetime(df["month_dt"])
    fig, axes = plt.subplots(2, 1, figsize=(13, 8.5), sharex=True)

    ax = axes[0]
    ax.bar(x, df["service_hours"], width=20, color=cfg.color("primary"), alpha=0.75, label="月度服务时长")
    ax.plot(x, df["service_hours"].rolling(3, min_periods=1).mean(),
            color=cfg.color("danger"), lw=2.2, label="3 月移动平均")
    ax.set_ylabel("服务时长（小时）")
    ax.set_title("志愿服务时长：夏季支教与秋季助学形成双峰，2026 年整体高于 2024 年")
    ax.legend(loc="upper left")

    ax2 = ax.twinx()
    ax2.plot(x, df["active_volunteers"], color=cfg.color("secondary"), lw=1.8, ls="--", label="活跃志愿者数")
    ax2.set_ylabel("活跃志愿者数（人）")
    ax2.grid(False)
    ax2.legend(loc="upper right")

    ax = axes[1]
    ax.bar(x, df["donation_amount"], width=20, color=cfg.color("secondary"), alpha=0.8, label="月度捐赠额")
    ax.plot(x, df["donation_amount"].rolling(3, min_periods=1).mean(),
            color=cfg.color("danger"), lw=2.2, label="3 月移动平均")
    ax.set_ylabel("捐赠额（元）")
    ax.yaxis.set_major_formatter(WAN_FMT)
    ax.set_xlabel("年月")
    ax.set_title("现金捐赠额：笔数多但金额集中于少数月份，2026-05 出现单笔 85 万元大额定向捐赠")
    ax.legend(loc="upper left")
    return _save(cfg, fig, "02_monthly_trend")


# ---------------------------------------------------------------------------
# 3. STL 分解
# ---------------------------------------------------------------------------


def plot_stl(cfg: Config, trends: dict[str, pd.DataFrame], metric: str = "志愿服务时长") -> Path:
    setup_style(cfg)
    df = trends.get(metric)
    if df is None or df.empty or "季节" not in df.columns:
        df = pd.DataFrame()
    fig, axes = plt.subplots(4, 1, figsize=(13, 10), sharex=True)
    if df.empty:
        for ax in axes:
            ax.text(0.5, 0.5, "数据不足，无法分解", ha="center", transform=ax.transAxes)
        return _save(cfg, fig, "03_stl_decomposition")

    idx = pd.to_datetime(df.index + "-01")
    for ax, col, color, title in zip(
        axes,
        ["观测值", "趋势", "季节", "残差"],
        [cfg.color("neutral"), cfg.color("primary"), cfg.color("secondary"), cfg.color("danger")],
        ["观测值", "趋势项（长期方向）", "季节项（年度内固定节奏）", "残差项（去除趋势与季节后的异常信号）"],
        strict=True,
    ):
        if col in df.columns:
            ax.plot(idx, df[col], color=color, lw=1.9)
            ax.fill_between(idx, 0, df[col], color=color, alpha=0.15)
        ax.set_ylabel(col)
        ax.set_title(title, fontsize=11)
    axes[1].set_title(f"{metric} 的 STL 分解：剥离季节节奏后再判断趋势，避免把开学季误读为业务改善", fontsize=13)
    return _save(cfg, fig, "03_stl_decomposition")


# ---------------------------------------------------------------------------
# 4. 季节性热力图
# ---------------------------------------------------------------------------


def plot_seasonality_heatmap(cfg: Config, panel: pd.DataFrame, metric: str = "service_hours") -> Path:
    setup_style(cfg)
    df = panel.copy()
    df["date"] = pd.to_datetime(df["month_dt"])
    df["年"] = df["date"].dt.year.astype(str)
    df["月"] = df["date"].dt.month
    pivot = df.pivot_table(index="年", columns="月", values=metric, aggfunc="sum").fillna(0.0)
    pivot = pivot.reindex(columns=range(1, 13), fill_value=0.0)

    fig, ax = plt.subplots(figsize=(12, 4.2))
    im = ax.imshow(pivot.to_numpy(), aspect="auto", cmap="YlGnBu")
    ax.set_xticks(range(12), [f"{m}月" for m in range(1, 13)])
    ax.set_yticks(range(len(pivot)), pivot.index)
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.iloc[i, j]
            if v > 0:
                ax.text(j, i, f"{v:,.0f}", ha="center", va="center", fontsize=8.5,
                        color="white" if v > pivot.to_numpy().max() * 0.6 else "#333333")
    ax.set_title("服务时长的年度节奏：6 月与 10-11 月为高峰，1-2 月高原严寒期最低")
    ax.grid(False)
    fig.colorbar(im, ax=ax, label="服务时长（小时）", shrink=0.85)
    return _save(cfg, fig, "04_seasonality_heatmap")


# ---------------------------------------------------------------------------
# 5. 交叉分析热力图
# ---------------------------------------------------------------------------


def plot_crosstab_heatmap(
    cfg: Config, cross_tables: dict[str, pd.DataFrame], key: str, name: str = "05_crosstab_heatmap"
) -> Path:
    setup_style(cfg)
    ct = cross_tables.get(key)
    if ct is None or ct.empty:
        ct = pd.DataFrame({"无数据": [0]})
    share = ct.div(ct.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    fig, ax = plt.subplots(figsize=(12, max(4.5, 0.55 * len(share) + 2)))
    im = ax.imshow(share.to_numpy(), aspect="auto", cmap="RdYlBu_r", vmin=0, vmax=max(0.35, share.to_numpy().max()))
    ax.set_xticks(range(len(share.columns)), [str(c) for c in share.columns], rotation=35, ha="right")
    ax.set_yticks(range(len(share.index)), [str(i) for i in share.index])
    for i in range(share.shape[0]):
        for j in range(share.shape[1]):
            v = share.iloc[i, j]
            if v >= 0.01:
                ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=8.5,
                        color="white" if v > 0.28 else "#333333")
    ax.set_title(f"{key}（行内占比）\n卡方检验提示维度间是否存在统计显著的结构差异")
    ax.grid(False)
    fig.colorbar(im, ax=ax, label="行内占比", shrink=0.85)
    return _save(cfg, fig, name)


# ---------------------------------------------------------------------------
# 6. 志愿者结构
# ---------------------------------------------------------------------------


def plot_volunteer_structure(cfg: Config, star: dict[str, pd.DataFrame]) -> Path:
    setup_style(cfg)
    vol = star["dim_volunteer"]
    vol = vol.loc[vol["volunteer_key"] != -1]
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    colors = _palette(cfg, 12)

    ax = axes[0, 0]
    vc = vol["ethnicity"].value_counts().head(6)
    ax.barh(vc.index[::-1], vc.to_numpy()[::-1], color=colors[: len(vc)][::-1])
    ax.set_title("志愿者民族结构：少数民族占比超过 85%，与州内人口结构一致")
    ax.set_xlabel("人数")

    ax = axes[0, 1]
    vc = vol["age_group"].value_counts()
    ax.pie(vc.to_numpy(), labels=vc.index, autopct="%1.1f%%", colors=colors,
           textprops={"fontsize": 9}, wedgeprops={"width": 0.45, "edgecolor": "white"})
    ax.set_title("年龄结构：18-34 岁青年志愿者占多数")

    ax = axes[1, 0]
    vc = vol["education"].value_counts()
    ax.bar(vc.index, vc.to_numpy(), color=colors[: len(vc)])
    ax.set_title("学历结构：本科及以上占比过半")
    ax.set_ylabel("人数")
    ax.tick_params(axis="x", rotation=20)

    ax = axes[1, 1]
    vol = vol.assign(双语标记=pd.to_numeric(vol["has_bilingual_skill"], errors="coerce").fillna(0))
    vc = vol.groupby("county").agg(注册=("volunteer_key", "size"), 双语=("双语标记", "sum"))
    vc["双语占比"] = (vc["双语"].astype(float) / vc["注册"].astype(float)).round(3)
    x = np.arange(len(vc))
    ax.bar(x - 0.2, vc["注册"], width=0.4, color=cfg.color("primary"), label="注册志愿者")
    ax.set_xticks(x, vc.index, rotation=12)
    ax.set_ylabel("注册人数")
    ax2 = ax.twinx()
    ax2.plot(x, vc["双语占比"], "o--", color=cfg.color("secondary"), lw=2, label="双语志愿者占比")
    ax2.set_ylabel("双语志愿者占比")
    ax2.grid(False)
    ax.set_title("县域分布与双语能力：德钦县双语志愿者占比最高（服务可达性关键）")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right")
    fig.suptitle("志愿者人力资本画像", fontsize=15, fontweight="bold")
    fig.tight_layout()
    return _save(cfg, fig, "06_volunteer_structure")


# ---------------------------------------------------------------------------
# 7. 捐赠结构 + 洛伦兹曲线
# ---------------------------------------------------------------------------


def plot_donation_structure(cfg: Config, star: dict[str, pd.DataFrame], concentration: dict[str, Any]) -> Path:
    setup_style(cfg)
    don = star["fact_donation"]
    don = don.loc[don["donor_key"] != -1]          # 排除 UNKNOWN 维度成员，避免污染结构占比
    donor = star["dim_donor"]
    merged = don.merge(donor[["donor_key", "donor_type"]], on="donor_key", how="left")
    colors = _palette(cfg, 8)
    personal_share = float(
        merged.loc[merged["donor_type"] == "个人", "amount_cny"].sum() / merged["amount_cny"].sum()
    )

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))

    ax = axes[0, 0]
    amt = merged.groupby("donor_type")["amount_cny"].sum().sort_values(ascending=False)
    ax.pie(amt.to_numpy(), labels=amt.index, autopct=lambda p: f"{p:.1f}%",
           colors=colors, textprops={"fontsize": 9}, wedgeprops={"width": 0.45, "edgecolor": "white"})
    ax.set_title(f"捐赠金额来源：个人小额捐赠占 {personal_share:.0%}，机构捐赠占 {1 - personal_share:.0%}")

    ax = axes[0, 1]
    cnt = merged.groupby("donor_type").size().sort_values(ascending=False)
    ax.bar(cnt.index, cnt.to_numpy(), color=colors[: len(cnt)])
    ax.set_title("捐赠笔数来源：个人笔数占比极高，是筹资的基本盘")
    ax.set_ylabel("笔数")
    ax.tick_params(axis="x", rotation=15)

    ax = axes[1, 0]
    band = merged.groupby("amount_band")["amount_cny"].agg(["size", "sum"])
    band = band.reindex(["100元以下", "100-1000元", "1000-1万元", "1-10万元", "10万元以上"]).dropna()
    x = np.arange(len(band))
    ax.bar(x - 0.2, band["size"], width=0.4, color=cfg.color("primary"), label="笔数")
    ax.set_xticks(x, band.index, rotation=18)
    ax.set_ylabel("笔数")
    ax2 = ax.twinx()
    ax2.bar(x + 0.2, band["sum"], width=0.4, color=cfg.color("secondary"), label="金额")
    ax2.yaxis.set_major_formatter(WAN_FMT)
    ax2.set_ylabel("金额（元）")
    ax2.grid(False)
    ax.set_title("金额区间：笔数与金额严重错位，<1000 元捐赠笔数最多、贡献金额最少")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right")

    ax = axes[1, 1]
    lor = concentration["lorenz"]
    ax.plot([0, 1], [0, 1], "--", color=cfg.color("neutral"), lw=1.2, label="绝对平均线")
    step = max(1, len(lor) // 400)
    ax.plot(lor["捐赠人累计占比"].to_numpy()[::step], lor["金额累计占比"].to_numpy()[::step],
            color=cfg.color("primary"), lw=2.4, label="实际洛伦兹曲线")
    ax.fill_between(lor["捐赠人累计占比"].to_numpy()[::step], lor["金额累计占比"].to_numpy()[::step],
                    lor["捐赠人累计占比"].to_numpy()[::step], color=cfg.color("secondary"), alpha=0.2)
    ax.set_xlabel("捐赠人累计占比")
    ax.set_ylabel("金额累计占比")
    ax.set_title(f"捐赠集中度：基尼系数 {concentration['gini']:.3f}，收入高度依赖少数大额捐赠人")
    ax.legend(loc="upper left")
    fig.suptitle("捐赠收入结构与集中度风险", fontsize=15, fontweight="bold")
    fig.tight_layout()
    return _save(cfg, fig, "07_donation_structure")


# ---------------------------------------------------------------------------
# 8. 异常检测
# ---------------------------------------------------------------------------


def plot_anomalies(cfg: Config, panel: pd.DataFrame, anomalies: pd.DataFrame) -> Path:
    setup_style(cfg)
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)

    for ax, col, label, color in zip(
        axes,
        ["service_hours", "donation_amount"],
        ["志愿服务时长（小时）", "现金捐赠额（元）"],
        [cfg.color("primary"), cfg.color("secondary")],
        strict=True,
    ):
        df = panel.copy()
        x = pd.to_datetime(df["month_dt"])
        ax.plot(x, df[col], "-o", ms=3.2, color=color, lw=1.6)
        med = df[col].median()
        ax.axhline(med, color=cfg.color("neutral"), ls=":", lw=1.2, label=f"中位数 {med:,.0f}")
        sub = anomalies[anomalies["指标"] == ("志愿服务时长" if col == "service_hours" else "现金捐赠额")]
        if len(sub):
            mask = pd.to_datetime(df["year_month"] + "-01").isin(pd.to_datetime(sub["年月"] + "-01"))
            ax.scatter(x[mask], df.loc[mask, col], s=95, facecolor="none",
                       edgecolor=cfg.color("danger"), lw=2.2, zorder=5, label="检出异常")
        ax.set_ylabel(label)
        ax.yaxis.set_major_formatter(WAN_FMT if col == "donation_amount" else FuncFormatter(lambda v, p: f"{v:,.0f}"))
        ax.legend(loc="upper left")
    axes[0].set_title("异常检测：多方法投票（3σ / 稳健Z / IQR / STL残差 / 孤立森林）标记出的偏离月份")
    axes[1].set_xlabel("年月")
    return _save(cfg, fig, "08_anomaly_detection")


def plot_quality_signature(cfg: Config, quality: pd.DataFrame) -> Path:
    setup_style(cfg)
    df = quality.copy()
    x = pd.to_datetime(df["年月"] + "-01")
    fig, ax = plt.subplots(figsize=(13, 4.6))
    ax.plot(x, df["签到修正率"], "-o", ms=3.5, color=cfg.color("danger"), label="签到记录修正率")
    ax.plot(x, df["服务悬空键率"], "-s", ms=3.5, color=cfg.color("accent"), label="服务记录悬空外键率")
    ax.axhline(df["签到修正率"].median(), ls=":", color=cfg.color("neutral"), lw=1.1, label="修正率中位数")
    ax.set_ylabel("比例")
    ax.set_xlabel("年月")
    ax.set_title("数据错误特征曲线：修正率突增的月份＝数据录入问题，而非业务变化")
    ax.legend(loc="upper left")
    return _save(cfg, fig, "09_quality_signature")


# ---------------------------------------------------------------------------
# 9. 项目评分卡
# ---------------------------------------------------------------------------


def plot_project_scorecard(cfg: Config, scorecard: pd.DataFrame) -> Path:
    setup_style(cfg)
    df = scorecard.dropna(subset=["execution_rate", "satisfaction_mean", "actual_cny"]).copy()
    fig, ax = plt.subplots(figsize=(12, 7))
    cats = df["category"].unique()
    colors = dict(zip(cats, _palette(cfg, len(cats)), strict=False))
    size = 40 + 620 * (df["actual_cny"] / df["actual_cny"].max()).fillna(0)
    for cat in cats:
        sub = df[df["category"] == cat]
        ax.scatter(sub["execution_rate"], sub["satisfaction_mean"], s=size[sub.index],
                   color=colors[cat], alpha=0.72, edgecolor="white", lw=1.2, label=cat)
    ax.axvline(1.0, color=cfg.color("neutral"), ls="--", lw=1.2)
    ax.axhline(df["satisfaction_mean"].median(), color=cfg.color("neutral"), ls=":", lw=1.2)
    ax.axvline(float(cfg.threshold("project_execution_rate").get("warn_below", 0.85)),
               color=cfg.color("danger"), ls=":", lw=1.2)
    ax.set_xlabel("预算执行率（实际支出 / 计划支出）")
    ax.set_ylabel("服务满意度均值（分）")
    ax.set_title("项目评分卡：气泡大小＝实际支出\n左上＝高质量低消耗；右下＝超支且满意度偏低，需重点复盘")
    ax.legend(loc="lower right", ncols=2, fontsize=9)

    for _, r in df.nlargest(5, "综合评分").iterrows():
        ax.annotate(str(r["project_name"])[:16], (r["execution_rate"], r["satisfaction_mean"]),
                    textcoords="offset points", xytext=(6, 5), fontsize=8, color="#333333")
    for _, r in df.nsmallest(3, "综合评分").iterrows():
        ax.annotate(str(r["project_name"])[:16], (r["execution_rate"], r["satisfaction_mean"]),
                    textcoords="offset points", xytext=(6, -12), fontsize=8, color=cfg.color("danger"))
    return _save(cfg, fig, "10_project_scorecard")


# ---------------------------------------------------------------------------
# 10. 队列留存 / 服务时长分布
# ---------------------------------------------------------------------------


def plot_cohort_retention(cfg: Config, cohort: pd.DataFrame) -> Path:
    setup_style(cfg)
    if cohort.empty:
        cohort = pd.DataFrame({"队列规模": [0]})
    rates = cohort.drop(columns=["队列规模"]).div(cohort["队列规模"].replace(0, np.nan), axis=0).fillna(0.0)
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    im = ax.imshow(rates.to_numpy(), aspect="auto", cmap="BuGn", vmin=0, vmax=max(0.5, rates.to_numpy().max()))
    ax.set_xticks(range(len(rates.columns)), [str(c) for c in rates.columns])
    ax.set_yticks(range(len(rates.index)), [str(i) for i in rates.index])
    for i in range(rates.shape[0]):
        for j in range(rates.shape[1]):
            v = rates.iloc[i, j]
            if v > 0:
                ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=9,
                        color="white" if v > 0.28 else "#333333")
    ax.set_xlabel("活动年份")
    ax.set_ylabel("注册年份队列")
    ax.set_title("志愿者留存队列：各年注册队列的年度活跃留存率")
    ax.grid(False)
    fig.colorbar(im, ax=ax, label="留存率", shrink=0.85)
    return _save(cfg, fig, "11_cohort_retention")


def plot_distributions(cfg: Config, star: dict[str, pd.DataFrame]) -> Path:
    setup_style(cfg)
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.6))

    ax = axes[0, 0]
    h = pd.to_numeric(star["fact_volunteer_service"]["hours"], errors="coerce").dropna()
    ax.hist(h, bins=45, color=cfg.color("primary"), alpha=0.85)
    ax.axvline(h.median(), color=cfg.color("danger"), lw=2, label=f"中位数 {h.median():.2f} 小时")
    ax.axvline(h.mean(), color=cfg.color("secondary"), lw=2, ls="--", label=f"均值 {h.mean():.2f} 小时")
    ax.set_xlabel("单次服务时长（小时）")
    ax.set_ylabel("人次")
    ax.set_title("单次服务时长分布：右偏，长尾来自全天候巡护与支教")
    ax.legend()

    ax = axes[0, 1]
    a = pd.to_numeric(star["fact_donation"]["amount_cny"], errors="coerce").dropna()
    ax.hist(np.log10(a[a > 0]), bins=45, color=cfg.color("secondary"), alpha=0.85)
    ax.set_xlabel("单笔捐赠金额（log10 元）")
    ax.set_ylabel("笔数")
    ax.set_title("捐赠金额对数分布：近似对数正态，跨越 4 个数量级")

    ax = axes[1, 0]
    s = pd.to_numeric(star["fact_beneficiary_service"]["satisfaction_score"], errors="coerce").dropna()
    vc = s.round().value_counts().sort_index()
    ax.bar(vc.index.astype(int).astype(str), vc.to_numpy(), color=cfg.color("primary"), alpha=0.9)
    ax.set_xlabel("满意度评分")
    ax.set_ylabel("记录数")
    ax.set_title(f"满意度分布：均值 {s.mean():.2f} 分，≥4 分占 {(s >= 4).mean():.0%}")

    ax = axes[1, 1]
    box = []
    labels = []
    bene = star["dim_beneficiary"]
    bs = star["fact_beneficiary_service"].merge(
        bene[["beneficiary_key", "household_type"]], on="beneficiary_key", how="left"
    )
    for household, g in bs.groupby("household_type"):
        vals = pd.to_numeric(g["satisfaction_score"], errors="coerce").dropna()
        if len(vals) > 30:
            box.append(vals.to_numpy())
            labels.append(str(household))
    if box:
        bp = ax.boxplot(box, tick_labels=labels, patch_artist=True, showfliers=False)
        for patch, color in zip(bp["boxes"], _palette(cfg, len(box)), strict=False):
            patch.set_facecolor(color)
            patch.set_alpha(0.75)
        ax.tick_params(axis="x", rotation=25)
    ax.set_ylabel("满意度评分")
    ax.set_title("满意度公平性：家庭越脆弱的服务对象，满意度系统性偏低")
    fig.suptitle("关键变量分布与公平性检验", fontsize=15, fontweight="bold")
    fig.tight_layout()
    return _save(cfg, fig, "12_distributions")


# ---------------------------------------------------------------------------
# 11. 弹性
# ---------------------------------------------------------------------------


def plot_elasticity(cfg: Config, panel: pd.DataFrame, elasticity: dict[str, Any]) -> Path:
    setup_style(cfg)
    df = panel.copy()
    df = df[(df["service_hours"] > 0) & (df["donation_amount"] > 0)]
    fig, ax = plt.subplots(figsize=(9, 6.4))
    x = np.log(df["donation_amount"].to_numpy())
    y = np.log(df["service_hours"].to_numpy())
    ax.scatter(x, y, s=55, color=cfg.color("primary"), alpha=0.8, edgecolor="white", lw=1.1)
    if np.isfinite(elasticity.get("elasticity", np.nan)):
        xs = np.linspace(x.min(), x.max(), 50)
        ys = elasticity["elasticity"] * xs + elasticity["intercept"]
        ax.plot(xs, ys, color=cfg.color("danger"), lw=2.4,
                label=f"拟合斜率（弹性）= {elasticity['elasticity']:.3f}\nR2 = {elasticity['r2']:.3f}")
        ax.legend(loc="upper left")
    ax.set_xlabel("log(月度现金捐赠额)")
    ax.set_ylabel("log(月度志愿服务时长)")
    ax.set_title("捐赠 - 服务弹性：双对数回归\n弹性 > 0 说明现金捐赠收缩会直接压缩服务供给，缺乏替代缓冲")
    return _save(cfg, fig, "13_elasticity")


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def render_all(
    cfg: Config,
    star: dict[str, pd.DataFrame],
    kpis: pd.DataFrame,
    analysis: dict[str, Any],
    anomalies: dict[str, Any],
) -> dict[str, Path]:
    """生成全部图表，返回 {图名: 路径}。"""
    logger.info("开始生成图表")
    cfg.paths.figures.mkdir(parents=True, exist_ok=True)
    figures: dict[str, Path] = {}
    figures["01_kpi_scorecard"] = plot_kpi_scorecard(cfg, kpis)
    figures["02_monthly_trend"] = plot_monthly_trend(cfg, analysis["monthly_panel"])
    figures["03_stl_decomposition"] = plot_stl(cfg, analysis["trends"])
    figures["04_seasonality_heatmap"] = plot_seasonality_heatmap(cfg, analysis["monthly_panel"])
    figures["05_crosstab_heatmap"] = plot_crosstab_heatmap(
        cfg, analysis["cross_tables"], "志愿者民族×项目类别"
    )
    figures["05b_crosstab_channel"] = plot_crosstab_heatmap(
        cfg, analysis["cross_tables"], "捐赠渠道×捐赠人类型", name="05b_crosstab_channel"
    )
    figures["06_volunteer_structure"] = plot_volunteer_structure(cfg, star)
    figures["07_donation_structure"] = plot_donation_structure(cfg, star, analysis["concentration"])
    figures["08_anomaly_detection"] = plot_anomalies(
        cfg, analysis["monthly_panel"], anomalies["monthly_anomalies"]
    )
    figures["09_quality_signature"] = plot_quality_signature(cfg, anomalies["quality_signature"])
    figures["10_project_scorecard"] = plot_project_scorecard(cfg, analysis["project_scorecard"])
    figures["11_cohort_retention"] = plot_cohort_retention(cfg, analysis["cohort"])
    figures["12_distributions"] = plot_distributions(cfg, star)
    figures["13_elasticity"] = plot_elasticity(cfg, analysis["monthly_panel"], analysis["elasticity"])
    logger.info("共生成 %d 张图表", len(figures))
    return figures
