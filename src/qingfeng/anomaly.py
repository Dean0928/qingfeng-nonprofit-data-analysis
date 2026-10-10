"""异常检测与归因。

四问框架
--------
1. **发现**（detect）：多种统计方法并行，取"多数票"降低单一方法的假阳性。
2. **定位**（locate）：按 县 × 月、项目类别 × 月 下钻，找出是谁把整体拉偏了。
3. **归因**（attribute）：区分「业务事件」与「数据错误」——
   数据错误通常伴随 ``is_corrected``（被截断/置缺）比例飙升、或重复率异常；
   业务事件则表现为总量变化但记录质量指标平稳。
4. **验证**（validate）：与生成器写入的 ``_ground_truth/planted_anomalies.csv``
   比对，计算检出率与误报情况，使"异常排查能力"可以被量化检验。

四种检测方法
------------
* ``zscore``：经典 3σ（对重尾分布敏感，所以必须与其它方法投票）
* ``modified_zscore``：基于中位数与 MAD 的稳健 Z 分数，抗离群点
* ``iqr``：Tukey 箱线规则
* ``stl_resid``：STL 分解后残差超限（先剥离趋势与季节）
* ``iforest``：Isolation Forest，多变量（水平 + 环比 + 同比 + 波动）联合判定
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.metrics import kpi_timeseries

logger = get_logger("anomaly")

METHOD_LABELS = {
    "zscore": "3σ 准则",
    "trend_resid": "去趋势 Z 分数",
    "modified_zscore": "稳健 Z 分数(MAD)",
    "iqr": "Tukey 箱线",
    "stl_resid": "STL 残差",
    "iforest": "孤立森林",
}


# ---------------------------------------------------------------------------
# 基础检测方法
# ---------------------------------------------------------------------------


def zscore_flags(s: pd.Series, threshold: float = 3.0) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce")
    mu, sd = v.mean(), v.std(ddof=1)
    if not sd or np.isnan(sd):
        return pd.Series(False, index=s.index)
    return ((v - mu).abs() / sd) > threshold


def modified_zscore_flags(s: pd.Series, threshold: float = 3.5) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce")
    med = v.median()
    mad = (v - med).abs().median()
    if not mad or np.isnan(mad):
        return pd.Series(False, index=s.index)
    mz = 0.6745 * (v - med) / mad
    return mz.abs() > threshold


def iqr_flags(s: pd.Series, multiplier: float = 1.5) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce")
    q1, q3 = v.quantile([0.25, 0.75])
    iqr = q3 - q1
    if not iqr:
        return pd.Series(False, index=s.index)
    return (v < q1 - multiplier * iqr) | (v > q3 + multiplier * iqr)


def stl_residual_flags(s: pd.Series, period: int = 6, threshold: float = 2.5) -> pd.Series:
    from qingfeng.analyze import stl_decompose

    decomp = stl_decompose(s, period=period)
    if decomp.empty or "残差" not in decomp.columns:
        return pd.Series(False, index=s.index)
    resid = decomp["残差"]
    sd = resid.std(ddof=1)
    if not sd or np.isnan(sd):
        return pd.Series(False, index=s.index)
    return (resid.abs() / sd) > threshold


def trend_residual_flags(
    s: pd.Series, window: int = 13, threshold: float = 3.0
) -> pd.Series:
    """去趋势 Z 分数：先用居中滚动中位数拟合局部趋势，再对残差做 3σ 判定。

    为什么需要它：本组织的服务量与捐赠额在观察期内持续增长，
    **水平值**上的 3σ 与 IQR 会被趋势本身撑大标准差/四分位距，
    从而对"趋势之下的真实回落"完全失效（本项目实测漏报了 2025-12 的活跃度骤降）。
    先剥离局部趋势再做离群判定，是趋势型业务指标的标准做法。
    """
    v = pd.to_numeric(s, errors="coerce")
    if v.notna().sum() < 8:
        return pd.Series(False, index=s.index)
    trend = v.rolling(window=window, center=True, min_periods=5).median()
    resid = v - trend
    sd = resid.std(ddof=1)
    if not sd or np.isnan(sd):
        return pd.Series(False, index=s.index)
    return ((resid.abs() / sd) > threshold).fillna(False)


def iforest_flags(s: pd.Series, contamination: float = 0.08, lags: int = 3) -> pd.Series:
    """带滞后特征与环比特征的孤立森林。"""
    try:
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler
    except Exception:  # pragma: no cover
        return pd.Series(False, index=s.index)

    v = pd.to_numeric(s, errors="coerce")
    if v.notna().sum() < 12:
        return pd.Series(False, index=s.index)

    feat = pd.DataFrame({"level": v})
    for lag in range(1, lags + 1):
        feat[f"lag{lag}"] = v.shift(lag)
    with np.errstate(divide="ignore", invalid="ignore"):
        feat["mom"] = v.pct_change().replace([np.inf, -np.inf], np.nan)
        feat["ratio_ma3"] = v / v.rolling(3).mean().replace(0, np.nan)
    feat = feat.bfill().ffill().fillna(0.0)

    X = StandardScaler().fit_transform(feat.to_numpy())
    model = IsolationForest(contamination=contamination, random_state=42, n_estimators=200, n_jobs=1)
    pred = model.fit_predict(X)
    return pd.Series(pred == -1, index=s.index)


def detect_anomalies(s: pd.Series, cfg: Config, *, label: str = "") -> pd.DataFrame:
    """对单条时间序列执行多方法检测并投票。"""
    a = cfg.analysis.get("anomaly", {})
    methods = a.get("methods", ["zscore", "iqr", "modified_zscore", "iforest"])
    votes = pd.DataFrame(index=s.index)
    if "zscore" in methods:
        votes["zscore"] = zscore_flags(s, float(a.get("zscore_threshold", 3.0)))
    if "modified_zscore" in methods:
        votes["modified_zscore"] = modified_zscore_flags(s, float(a.get("modified_z_threshold", 3.5)))
    if "iqr" in methods:
        votes["iqr"] = iqr_flags(s, float(a.get("iqr_multiplier", 1.5)))
    if "trend_resid" in methods:
        votes["trend_resid"] = trend_residual_flags(
            s, window=int(a.get("trend_window", 13)), threshold=float(a.get("trend_z_threshold", 3.0))
        )
    if "stl_resid" in methods:
        votes["stl_resid"] = stl_residual_flags(s, period=int(cfg.analysis.get("trend", {}).get("stl_period", 6)))
    if "iforest" in methods:
        votes["iforest"] = iforest_flags(s, float(a.get("iforest_contamination", 0.08)))

    min_votes = int(a.get("consensus_min_votes", 2))
    out = votes.copy()
    out["投票数"] = votes.sum(axis=1).astype(int)
    out["是否异常"] = out["投票数"] >= min_votes
    out["判定方法"] = votes.apply(
        lambda r: "+".join(METHOD_LABELS[m] for m in votes.columns if bool(r[m])), axis=1
    )
    out["指标"] = label
    v = pd.to_numeric(s, errors="coerce")
    med = v.median()
    sd = v.std(ddof=1)
    out["观测值"] = v
    out["稳健Z分数"] = ((v - med) / (1.4826 * (v - med).abs().median())) if (v - med).abs().median() else np.nan
    out["偏离中位数"] = ((v - med) / med).replace([np.inf, -np.inf], np.nan) if med else np.nan
    out["方向"] = np.where(v > med, "偏高", "偏低")
    # 统一把"期间"提升为一列，便于跨指标 concat
    out.index.name = "期间"
    return out.reset_index()


# ---------------------------------------------------------------------------
# 业务级异常检测
# ---------------------------------------------------------------------------


def detect_monthly_anomalies(cfg: Config, panel: pd.DataFrame) -> pd.DataFrame:
    """对 6 个月度核心指标分别检测，汇总为一张异常清单。"""
    metrics = {
        "志愿服务时长": "service_hours",
        "活跃志愿者数": "active_volunteers",
        "服务人次": "service_visits",
        "现金捐赠额": "donation_amount",
        "捐赠笔数": "donation_count",
        "预算执行率": "execution_rate",
    }
    frames = []
    for label, col in metrics.items():
        if col not in panel.columns:
            continue
        s = panel.set_index("year_month")[col].astype(float)
        res = detect_anomalies(s, cfg, label=label)
        frames.append(res)
    if not frames:
        return pd.DataFrame()
    allres = pd.concat(frames, ignore_index=True)
    flagged = allres[allres["是否异常"]].copy()
    flagged = flagged.rename(columns={"期间": "年月"})
    return flagged.sort_values(["年月", "指标"]).reset_index(drop=True)


def detect_county_month_anomalies(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """按「县 × 月」下钻检测服务时长异常 —— 这是归因的关键一步。

    全国/全州层面的总量异常往往被"一个县的剧烈波动"掩盖，必须下钻才能定位。
    """
    svc = star["fact_volunteer_service"].copy()
    svc = svc.loc[svc["county"].astype(str) != "UNKNOWN"]
    svc["年月"] = pd.to_datetime(svc["date_key"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m")
    pivot = svc.pivot_table(index="年月", columns="county", values="hours", aggfunc="sum").fillna(0.0)
    pivot = pivot.loc[(pivot.index >= cfg.simulation.start_date[:7]) & (pivot.index <= cfg.simulation.as_of_date[:7])]

    rows = []
    for county in pivot.columns:
        res = detect_anomalies(pivot[county], cfg, label="志愿服务时长")
        res = res[res["是否异常"]]
        for _, row in res.iterrows():
            rows.append(
                {
                    "县": county,
                    "年月": row["期间"],
                    "观测值": round(float(row["观测值"]), 1),
                    "偏离中位数": round(float(row["偏离中位数"]), 3) if pd.notna(row["偏离中位数"]) else np.nan,
                    "方向": row["方向"],
                    "投票数": int(row["投票数"]),
                    "判定方法": row["判定方法"],
                }
            )
    out = pd.DataFrame(rows)
    return out.sort_values(["年月", "县"]).reset_index(drop=True) if len(out) else out


def quality_signature(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """计算「数据错误特征」月度曲线：修正记录率与重复剔除率。

    这是区分两类异常的判据：
    * 数据错误 → 修正率 / 重复率显著抬升；
    * 业务事件 → 记录质量指标平稳，只有业务量变化。
    """
    svc = star["fact_volunteer_service"].copy()
    svc["年月"] = pd.to_datetime(svc["date_key"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m")
    bs = star["fact_beneficiary_service"].copy()
    bs["年月"] = pd.to_datetime(bs["date_key"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m")

    out = pd.DataFrame(
        {
            "签到记录数": svc.groupby("年月").size(),
            "签到修正数": svc.groupby("年月")["is_corrected"].sum(),
            "服务记录数": bs.groupby("年月").size(),
            "服务记录悬空键数": bs.groupby("年月")["has_orphan_key"].sum(),
        }
    ).fillna(0.0)
    out["签到修正率"] = (out["签到修正数"] / out["签到记录数"].replace(0, np.nan)).round(4)
    out["服务悬空键率"] = (out["服务记录悬空键数"] / out["服务记录数"].replace(0, np.nan)).round(4)

    # 关键补充：从**原始数据**重算记录数，得到"被清洗剔除的比例"。
    # 被清洗修掉的异常（如整表重复导入、分钟当小时）在清洗后的数据里是看不见的，
    # 只有对比原始与清洗后的量才能发现——这是数据错误通道的核心依据。
    raw_bs = _raw_monthly_counts(cfg, "beneficiary_services", "service_date", "county")
    if raw_bs is not None:
        raw_bs = raw_bs.groupby("年月", as_index=False)["原始服务记录数"].sum()
        merged = out.reset_index().merge(raw_bs, on="年月", how="left")
        merged["服务记录剔除率"] = (
            1 - merged["服务记录数"] / merged["原始服务记录数"].replace(0, np.nan)
        ).round(4)
        merged = merged.set_index("年月")
        out = merged
    raw_sh = _raw_monthly_counts(cfg, "volunteer_shifts", "service_date", "county")
    if raw_sh is not None:
        raw_sh = raw_sh.groupby("年月", as_index=False)["原始签到记录数"].sum()
        merged = out.reset_index().merge(raw_sh, on="年月", how="left")
        merged["签到记录剔除率"] = (
            1 - merged["签到记录数"] / merged["原始签到记录数"].replace(0, np.nan)
        ).round(4)
        out = merged.set_index("年月")
    out = out[~out.index.duplicated(keep="first")]
    # 修正率的分母是当月签到记录数；若某月无签到记录则该月不参与数据错误通道
    out = out.loc[pd.to_numeric(out.get("签到记录数", 0), errors="coerce").fillna(0) > 0]
    out.index.name = "年月"
    return out.reset_index()


def _raw_monthly_counts(
    cfg: Config, table: str, date_col: str, county_col: str = "county"
) -> pd.DataFrame | None:
    """从原始 CSV 按月（可选按县）统计记录数，用于计算清洗剔除率。"""
    from qingfeng.clean import CleaningLog, parse_mixed_dates

    path = cfg.paths.raw_file(f"{table}.csv")
    if not path.is_file():
        return None
    raw = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    if date_col not in raw.columns:
        return None
    silent = CleaningLog()
    parsed, _ = parse_mixed_dates(raw[date_col], table, date_col, silent)
    raw = raw.assign(_parsed=parsed)
    raw["年月"] = raw["_parsed"].dt.strftime("%Y-%m")
    raw = raw.dropna(subset=["年月"])
    prefix = "原始服务记录数" if "service" in table else "原始签到记录数"
    if county_col in raw.columns:
        raw[county_col] = raw[county_col].astype(str).str.strip()
        out = (
            raw.groupby(["年月", county_col]).size().rename(prefix).reset_index()
            .rename(columns={county_col: "县"})
        )
        return out
    return raw.groupby("年月").size().rename(prefix).reset_index()


def detect_data_error_anomalies(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """数据错误通道：按「县 × 月」检测清洗剔除率与修正率的异常抬升。"""
    signature = quality_signature(cfg, star)
    rows: list[dict[str, Any]] = []

    for metric, label in (("签到修正率", "签到修正率"), ("服务记录剔除率", "服务记录剔除率")):
        if metric not in signature.columns:
            continue
        s = signature.set_index("年月")[metric].astype(float)
        res = detect_anomalies(s, cfg, label=label)
        # 数据错误只会表现为"修正率/剔除率异常抬升"；偏低是健康信号，不应报为异常
        for _, r in res[res["是否异常"] & (res["方向"] == "偏高")].iterrows():
            rows.append(
                {
                    "县": "全州",
                    "年月": r["期间"],
                    "指标": label,
                    "观测值": round(float(r["观测值"]), 4),
                    "偏离中位数": round(float(r["偏离中位数"]), 3) if pd.notna(r["偏离中位数"]) else np.nan,
                    "方向": r["方向"],
                    "投票数": int(r["投票数"]),
                    "判定方法": r["判定方法"],
                }
            )

    # 县层面的剔除率异常
    from qingfeng.clean import CleaningLog, parse_mixed_dates

    for table, prefix, label in (
        ("beneficiary_services", "服务", "服务记录剔除率"),
        ("volunteer_shifts", "签到", "签到记录剔除率"),
    ):
        raw = _raw_monthly_counts(cfg, table, "service_date", "county")
        if raw is None:
            continue
        count_cols = [c for c in raw.columns if str(c).startswith("原始")]
        if not count_cols:
            continue
        raw = raw.rename(columns={count_cols[0]: "原始记录数"})
        if "县" not in raw.columns:
            continue
        star_table = "fact_beneficiary_service" if "beneficiary" in table else "fact_volunteer_service"
        fact = star[star_table].copy()
        fact["年月"] = pd.to_datetime(
            fact["date_key"].astype(str), format="%Y%m%d", errors="coerce"
        ).dt.strftime("%Y-%m")
        clean_counts = (
            fact.groupby(["年月", "county"]).size().rename("清洗后记录数").reset_index()
            .rename(columns={"county": "县"})
        )
        joined = raw.merge(clean_counts, on=["年月", "县"], how="inner")
        joined = joined.loc[joined["原始记录数"] >= 15]
        joined[label] = (1 - joined["清洗后记录数"] / joined["原始记录数"]).round(4)
        for county, group in joined.groupby("县"):
            gp = group.set_index("年月")[label].astype(float)
            if gp.notna().sum() < 12:
                continue
            res = detect_anomalies(gp, cfg, label=label)
            for _, r in res[res["是否异常"] & (res["方向"] == "偏高")].iterrows():
                rows.append(
                    {
                        "县": county,
                        "年月": r["期间"],
                        "指标": label,
                        "观测值": round(float(r["观测值"]), 4),
                        "偏离中位数": round(float(r["偏离中位数"]), 3) if pd.notna(r["偏离中位数"]) else np.nan,
                        "方向": r["方向"],
                        "投票数": int(r["投票数"]),
                        "判定方法": r["判定方法"],
                    }
                )
    out = pd.DataFrame(rows)
    if len(out):
        out = out.drop_duplicates(subset=["县", "年月", "指标"]).sort_values(["年月", "县"])
    return out.reset_index(drop=True) if len(out) else out


def attribute_anomaly(
    cfg: Config, star: dict[str, pd.DataFrame], period: str, metric: str
) -> dict[str, Any]:
    """对某个「年月 + 指标」异常做多维度归因下钻。

    返回各候选维度的贡献分解（谁贡献了最大的偏离），并给出初步的成因判断。
    """
    svc = star["fact_volunteer_service"].copy()
    don = star["fact_donation"].copy()
    donor_county = star["dim_donor"][["donor_key", "county"]].rename(columns={"county": "donor_county"})
    # 项目维的 county 需改名，避免与签到事实表自带的 county 在 merge 后产生 county_x / county_y
    proj = star["dim_project"][["project_key", "category", "county"]].rename(
        columns={"county": "project_county"}
    )

    use = don if metric in {"现金捐赠额", "捐赠笔数"} else svc
    value_col = "amount_cny" if use is don else "hours"
    use["年月"] = pd.to_datetime(use["date_key"].astype(str), format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m")

    monthly = use.groupby("年月")[value_col].sum()
    baseline = monthly.median()
    observed = float(monthly.get(period, np.nan))
    deviation = observed - baseline

    merged = use.merge(proj, on="project_key", how="left")
    if metric in {"现金捐赠额", "捐赠笔数"}:
        merged = merged.merge(donor_county, on="donor_key", how="left")
    # 归属优先级：事实表自带 county -> 项目所在县 -> 捐赠人所在县 -> UNKNOWN
    if "county" not in merged.columns:
        merged["county"] = np.nan
    fallback = pd.Series(np.nan, index=merged.index, dtype=object)
    for col in ("project_county", "donor_county"):
        if col in merged.columns:
            fallback = fallback.where(fallback.notna(), merged[col])
    merged["county"] = merged["county"].fillna(fallback).fillna("UNKNOWN")
    focus = merged[merged["年月"] == period]
    rest = merged[merged["年月"] != period]

    breakdown: dict[str, pd.DataFrame] = {}
    for dim in ("county", "category"):
        f = focus.groupby(dim)[value_col].sum()
        r = rest.groupby(dim)[value_col].mean()
        cmp = pd.DataFrame({"当期": f, "历史月均": r}).fillna(0.0)
        cmp["偏离"] = cmp["当期"] - cmp["历史月均"]
        cmp["偏离占比"] = (cmp["偏离"] / cmp["偏离"].abs().sum()) if cmp["偏离"].abs().sum() else 0.0
        breakdown[dim] = cmp.sort_values("偏离", key=lambda x: x.abs(), ascending=False)

    top_county = breakdown["county"].index[0] if len(breakdown["county"]) else None
    top_category = breakdown["category"].index[0] if len(breakdown["category"]) else None
    top_county_share = (
        float(breakdown["county"]["偏离占比"].iloc[0]) if len(breakdown["county"]) else np.nan
    )

    # 数据错误判据：修正率显著高于历史水平
    qs = quality_signature(cfg, star).set_index("年月")
    def _scalar(frame: pd.DataFrame, key: str, col: str) -> float:
        if col not in frame.columns or key not in frame.index:
            return float("nan")
        v = frame.loc[key, col]
        return float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)

    corr_rate = _scalar(qs, period, "签到修正率")
    corr_base = float(qs["签到修正率"].median()) if "签到修正率" in qs.columns else float("nan")
    is_data_error = bool(pd.notna(corr_rate) and pd.notna(corr_base) and corr_rate > corr_base * 2.5)

    return {
        "年月": period,
        "指标": metric,
        "当期值": round(observed, 2) if pd.notna(observed) else np.nan,
        "历史中位月": round(float(baseline), 2),
        "偏离量": round(float(deviation), 2) if pd.notna(deviation) else np.nan,
        "偏离幅度": round(float(deviation / baseline), 4) if baseline else np.nan,
        "主责县域": top_county,
        "主责县域偏离贡献占比": round(top_county_share, 4) if pd.notna(top_county_share) else np.nan,
        "主责项目类别": top_category,
        "当期修正率": round(corr_rate, 4) if pd.notna(corr_rate) else np.nan,
        "历史修正率中位数": round(corr_base, 4) if pd.notna(corr_base) else np.nan,
        "初步成因判断": "数据录入错误（修正率异常抬升）" if is_data_error else "业务事件或真实波动（记录质量平稳）",
        "维度分解": breakdown,
    }


# ---------------------------------------------------------------------------
# 与 ground truth 比对
# ---------------------------------------------------------------------------


def load_ground_truth(cfg: Config) -> pd.DataFrame:
    path = cfg.paths.raw / "_ground_truth" / "planted_anomalies.csv"
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


def evaluate_detection(
    cfg: Config,
    monthly: pd.DataFrame,
    county_month: pd.DataFrame,
    data_errors: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """把检测结果与植入异常 ground truth 比对，计算检出率。

    这是本项目"可验证性"的核心：异常检测不是自说自话，而是能被量化打分。
    """
    gt = load_ground_truth(cfg)
    if gt.empty:
        return pd.DataFrame()

    detected = set()
    data_error_seen = set()
    for _, r in monthly.iterrows():
        detected.add((str(r["年月"]), str(r["指标"])))
    for _, r in county_month.iterrows():
        detected.add((str(r["年月"]), f"county:{r['县']}"))
    if data_errors is not None and len(data_errors):
        for _, r in data_errors.iterrows():
            data_error_seen.add((str(r["年月"]), str(r["县"])))

    metric_map = {
        "volunteer_service_hours": "志愿服务时长",
        "donation_amount": "现金捐赠额",
        "beneficiary_service_count": "服务人次",
        "active_volunteer_rate": "活跃志愿者数",
        "project_execution_rate": "预算执行率",
    }

    rows = []
    for _, g in gt.iterrows():
        label = metric_map.get(g["metric"], g["metric"])
        scope = g["scope"]
        hit_global = (g["period"], label) in detected
        hit_county = (g["period"], f"county:{scope}") in detected
        hit_quality = (g["period"], scope) in data_error_seen or (g["period"], "全州") in data_error_seen
        rows.append(
            {
                "异常编号": g["anomaly_id"],
                "植入月份": g["period"],
                "植入范围": scope,
                "植入指标": label,
                "偏离方向": g["direction"],
                "偏离幅度": g["magnitude"],
                "真实成因类型": {"business_event": "业务事件", "data_error": "数据错误"}[g["cause_type"]],
                "整体层面检出": "是" if hit_global else "否",
                "县域下钻检出": "是" if hit_county else "否",
                "数据错误通道检出": "是" if hit_quality else "否",
                "综合检出": "是" if (hit_global or hit_county or hit_quality) else "否",
                "说明": g["cause"],
            }
        )
    out = pd.DataFrame(rows)
    n = len(out)
    n_hit = int((out["综合检出"] == "是").sum())
    logger.info("异常检出率：%d/%d = %.1f%%", n_hit, n, 100 * n_hit / n if n else 0)
    return out


# ---------------------------------------------------------------------------
# 漏报解释：为什么有些真实异常检测不到
# ---------------------------------------------------------------------------


def explain_misses(
    cfg: Config, star: dict[str, pd.DataFrame], evaluation: pd.DataFrame
) -> pd.DataFrame:
    """对未被检出的植入异常，量化说明"为什么检测不到"。

    漏报本身也是结论。最常见的两种原因是：

    1. **信噪比不足**——该序列自身的月度波动（用 MAD/中位数 衡量）远大于该次偏离，
       单点偏离落在正常波动范围内，任何单变量离群点方法都无法与噪声区分。
    2. **季节混淆**——该次回落与年度季节节奏重合，STL 剥离季节成分后残差并不突出。

    与其调低阈值把假阳性一起放进来（会毁掉预警的可信度），不如把漏报原因写清楚，
    并指出需要补充什么信息（外生事件日历、更高频数据、多变量模型）才能检出。
    """
    if evaluation is None or not len(evaluation):
        return pd.DataFrame()
    misses = evaluation[evaluation["综合检出"] == "否"]
    if not len(misses):
        return pd.DataFrame()

    svc = star["fact_volunteer_service"].copy()
    svc["年月"] = pd.to_datetime(
        svc["date_key"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.strftime("%Y-%m")
    don = star["fact_donation"].copy()
    don["年月"] = pd.to_datetime(
        don["date_key"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.strftime("%Y-%m")
    panel = star.get("__panel__")

    rows: list[dict[str, Any]] = []
    for _, m in misses.iterrows():
        scope = str(m["植入范围"])
        period = str(m["植入月份"])
        label = str(m["植入指标"])

        scope_sub = svc if scope in ("全州", "未知") else svc.loc[svc["county"] == scope]
        if label == "现金捐赠额":
            series = don.groupby("年月")["amount_cny"].sum().astype(float)
        elif label == "活跃志愿者数":
            series = scope_sub.groupby("年月")["volunteer_key"].nunique().astype(float)
        elif label == "预算执行率":
            monthly = star["fact_project_monthly"].copy()
            monthly["年月"] = monthly["year_month"]
            planned = monthly.groupby("年月")["planned_cny"].sum()
            actual = monthly.groupby("年月")["actual_cny"].sum()
            series = (actual / planned.replace(0, np.nan)).astype(float)
        else:
            series = scope_sub.groupby("年月")["hours"].sum().astype(float)

        series = series.loc[
            (series.index >= cfg.simulation.start_date[:7])
            & (series.index <= cfg.simulation.as_of_date[:7])
        ].sort_index()
        if not len(series) or period not in series.index:
            continue

        observed = float(series.loc[period])
        # 基线必须用「局部趋势」而不是全序列中位数：本组织的服务量在观察期内持续增长，
        # 用全期中位数做基线会把"趋势上方的正常值"误判为偏高，也会掩盖趋势下方的回落。
        trend = series.rolling(window=13, center=True, min_periods=5).median()
        expected = float(trend.loc[period]) if pd.notna(trend.loc[period]) else float(series.median())
        deviation = (observed - expected) / expected if expected else np.nan

        resid = (series - trend).dropna()
        med_all = float(series.median())
        cv_robust = (
            1.4826 * float((resid - resid.median()).abs().median()) / med_all if med_all and len(resid) else np.nan
        )

        # 年度季节指数（相对全年月均），用于判断"回落是否只是正常的季节低谷"
        month_num = int(period[5:7])
        by_month = series.groupby(series.index.str.slice(5, 7)).mean()
        overall = float(series.mean())
        season_index = float(by_month.get(f"{month_num:02d}", overall)) / overall if overall else np.nan

        if pd.notna(cv_robust) and abs(deviation) <= 2.0 * cv_robust:
            reason = (
                f"信噪比不足：相对局部趋势的实际偏离为 {deviation:+.0%}，"
                f"而该序列残差的稳健变异系数为 {cv_robust:.2f}，"
                "单点偏离未超出序列自身的正常波动范围"
            )
            need = "引入业务事件日历（天气/交通/节庆）作为外生变量，或改用 CUSUM 等变点检测识别缓变累积偏移"
        elif pd.notna(season_index) and ((deviation < 0 and season_index < 0.9) or (deviation > 0 and season_index > 1.1)):
            reason = (
                f"季节混淆：该月本就处于年度{'低谷' if season_index < 1 else '高峰'}"
                f"（季节指数 {season_index:.2f}，1.00 为全年月均），"
                f"实际偏离 {deviation:+.0%} 与季节方向一致，STL 剥离季节成分后残差不再突出"
            )
            need = "改用去年同期作为对照（同比而非环比），或建立分月基线而非全局基线"
        else:
            reason = (
                f"偏离被相邻月份吸收：实际偏离 {deviation:+.0%}，"
                f"稳健变异系数 {cv_robust:.2f}，偏离幅度不足以在多点投票中取得共识"
            )
            need = "提高数据频率（月 → 周），或改用对单点更敏感但需人工复核的方法"

        rows.append(
            {
                "异常编号": m["异常编号"],
                "植入月份": period,
                "植入范围": scope,
                "植入指标": label,
                "真实成因类型": m["真实成因类型"],
                "当期观测值": round(observed, 1),
                "局部趋势基线": round(expected, 1),
                "实际偏离幅度": round(float(deviation), 4) if pd.notna(deviation) else np.nan,
                "序列稳健变异系数": round(float(cv_robust), 4) if pd.notna(cv_robust) else np.nan,
                "年度季节指数": round(float(season_index), 3) if pd.notna(season_index) else np.nan,
                "漏报原因": reason,
                "改进方向": need,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def run_anomaly_detection(cfg: Config, star: dict[str, pd.DataFrame], panel: pd.DataFrame | None = None) -> dict[str, Any]:
    """执行完整异常检测流程，返回检测清单、归因明细与检出评估。"""
    panel = panel if panel is not None else kpi_timeseries(cfg, star)
    logger.info("步骤 1/4：月度整体指标异常检测")
    monthly = detect_monthly_anomalies(cfg, panel)
    logger.info("  → 检出 %d 个整体层面异常点", len(monthly))

    logger.info("步骤 2/4：按县×月下钻检测")
    county_month = detect_county_month_anomalies(cfg, star)
    logger.info("  → 检出 %d 个县域层面异常点", len(county_month))

    logger.info("步骤 3/4：对每个异常做归因")
    attributions = []
    for _, r in monthly.iterrows():
        attributions.append(attribute_anomaly(cfg, star, str(r["年月"]), str(r["指标"])))
    for _, r in county_month.head(20).iterrows():
        attributions.append(attribute_anomaly(cfg, star, str(r["年月"]), "志愿服务时长"))
    attr_df = pd.DataFrame([{k: v for k, v in a.items() if k != "维度分解"} for a in attributions])

    logger.info("步骤 4/5：数据错误通道（原始 vs 清洗后）")
    data_errors = detect_data_error_anomalies(cfg, star)
    logger.info("  → 检出 %d 个数据质量异常点", len(data_errors))

    logger.info("步骤 5/5：与植入异常 ground truth 比对")
    evaluation = evaluate_detection(cfg, monthly, county_month, data_errors)
    misses = explain_misses(cfg, star, evaluation)
    if len(misses):
        logger.info("  → 有 %d 个植入异常未被检出，已在报告中给出量化归因", len(misses))

    return {
        "monthly_anomalies": monthly,
        "county_month_anomalies": county_month,
        "data_error_anomalies": data_errors,
        "attributions": attr_df,
        "attribution_detail": attributions,
        "evaluation": evaluation,
        "miss_explanations": misses,
        "quality_signature": quality_signature(cfg, star),
    }


def write_anomaly_results(cfg: Config, result: dict[str, Any]) -> None:
    out = cfg.paths.processed
    out.mkdir(parents=True, exist_ok=True)
    mapping = {
        "anomaly_monthly": result["monthly_anomalies"],
        "anomaly_county_month": result["county_month_anomalies"],
        "anomaly_attribution": result["attributions"],
        "anomaly_evaluation": result["evaluation"],
        "anomaly_miss_explanations": result["miss_explanations"],
        "anomaly_data_errors": result["data_error_anomalies"],
        "quality_signature": result["quality_signature"],
    }
    for name, df in mapping.items():
        if isinstance(df, pd.DataFrame) and len(df):
            df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
    logger.info("异常检测结果已写入 %s", out)
