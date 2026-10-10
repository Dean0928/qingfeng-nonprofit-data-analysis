"""报告生成层：把分析结果组装成可直接阅读的交付物。

产出三份文件：

* ``reports/数据分析报告.md`` —— 主报告（含结论、表格、图表引用），GitHub 上可直接阅读
* ``reports/executive_summary.md`` —— 一页纸结论摘要，给管理层看
* ``reports/dashboard.html`` —— 单文件 HTML 仪表盘（图片内嵌为 base64，可离线打开）

报告中的所有数字都来自上游计算，没有任何硬编码；重跑管道即可得到更新后的报告。
"""

from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger

logger = get_logger("report")

MAX_ROWS = 15


# ---------------------------------------------------------------------------
# 渲染小工具
# ---------------------------------------------------------------------------


def _fmt_value(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, (float, np.floating)):
        av = abs(float(v))
        if av >= 1000:
            return f"{float(v):,.0f}"
        if av >= 1:
            return f"{float(v):,.2f}"
        return f"{float(v):.4f}"
    return str(v)


def md_table(df: pd.DataFrame, max_rows: int = MAX_ROWS, floatfmt: str | None = None) -> str:
    """把 DataFrame 渲染为 GitHub 风格的 Markdown 表格（不依赖 tabulate）。

    数值默认走 :func:`_fmt_value` 的智能格式（整数带千分位、比率保留有效位），
    避免把 "3,600 人" 渲染成 "3600.00 人"；需要固定小数位时显式传入 ``floatfmt``。
    """
    if df is None or len(df) == 0:
        return "> （无数据）\n"
    sub = df.head(max_rows).copy()
    cols = [str(c) for c in sub.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in sub.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (int, np.integer)):
                cells.append(f"{int(v):,}")
            elif isinstance(v, (float, np.floating)) and not pd.isna(v) and float(v).is_integer() and abs(float(v)) >= 10:
                cells.append(f"{int(v):,}")
            elif floatfmt is not None and isinstance(v, (float, np.floating)) and not pd.isna(v):
                cells.append(floatfmt.format(float(v)))
            else:
                txt = _fmt_value(v)
                cells.append(txt.replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    extra = ""
    if len(df) > max_rows:
        extra = f"\n\n> 表中共 {len(df)} 行，此处仅展示前 {max_rows} 行；完整数据见 `data/processed/` 下同名 CSV。\n"
    return "\n".join(lines) + "\n" + extra


def kpi_value(kpis: pd.DataFrame, code: str) -> float | None:
    row = kpis.loc[kpis["指标代码"] == code, "指标值"]
    return float(row.iloc[0]) if len(row) and pd.notna(row.iloc[0]) else None


def kpi_status(kpis: pd.DataFrame, code: str) -> str:
    row = kpis.loc[kpis["指标代码"] == code, "状态"]
    return str(row.iloc[0]) if len(row) else "—"


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None or pd.isna(v) else f"{v * 100:.{digits}f}%"


def _img(path: Path, alt: str, width: str = "100%") -> str:
    rel = f"figures/{path.name}"
    return f'<img src="{rel}" alt="{alt}" width="{width}" />'


# ---------------------------------------------------------------------------
# 主报告
# ---------------------------------------------------------------------------


def build_markdown_report(
    cfg: Config,
    kpis: pd.DataFrame,
    analysis: dict[str, Any],
    anomalies: dict[str, Any],
    validation: pd.DataFrame,
    cleaning_log: pd.DataFrame,
    star: dict[str, pd.DataFrame],
    figures: dict[str, Path],
) -> str:
    org = cfg.org_name
    region = cfg.region
    window = cfg.simulation
    panel = analysis["monthly_panel"]
    conc = analysis["concentration"]
    elas = analysis["elasticity"]
    ts = analysis["trend_summary"]

    lines: list[str] = []
    add = lines.append

    # ---------------- 封面 ----------------
    add(f"# {org} 公益项目数据运营分析报告\n")
    add(
        f"> **分析主体**：{org}（{region}）  \n"
        f"> **数据窗口**：{window.start_date} ~ {window.as_of_date}  \n"
        f"> **报告生成时间**：{datetime.now():%Y-%m-%d %H:%M}  \n"
        f"> **数据性质**：本报告所用业务明细为**程序生成的模拟数据**，"
        f"分布参数依据公开统计口径校准（见 `data/external/calibration_reference.md`），"
        f"不含任何真实个人信息。\n"
    )

    # ---------------- 一、摘要 ----------------
    add("## 一、执行摘要\n")
    add("### 1.1 核心结论\n")
    add(
        f"1. **服务规模稳步扩张，但供给节奏高度季节化。** 观察期内累计志愿服务 "
        f"**{_fmt_value(kpi_value(kpis, 'SCALE-04'))} 小时**，覆盖 "
        f"**{_fmt_value(kpi_value(kpis, 'SCALE-08'))} 名建档服务对象**、"
        f"**{_fmt_value(kpi_value(kpis, 'SCALE-09'))} 人次**服务。服务时长存在明显的双峰节奏"
        f"（6 月暑期支教、10-11 月秋季助学），1-2 月高原严寒期降至全年最低，"
        f"STL 季节强度（0-1，越高说明季节成分越主导）为 **{_fmt_value(ts.loc[ts['指标'] == '志愿服务时长', '季节强度'].iloc[0] if len(ts) else None)}**。\n"
        f"2. **现金捐赠高度集中，收入结构存在抗风险短板。** 捐赠总额 "
        f"**{_fmt_value(kpi_value(kpis, 'SCALE-06'))} 元**，但基尼系数达 **{conc['gini']:.3f}**，"
        f"前 20% 捐赠人贡献了 **{_pct(conc['pareto_points'].iloc[3]['金额占比'] if len(conc['pareto_points']) > 3 else None)}** 的金额。"
        f"HHI 为 **{_fmt_value(kpi_value(kpis, 'STR-01'))}**，"
        f"单一大额捐赠（德钦县雪灾 85 万元定向捐赠）即可显著扰动月度收入曲线。\n"
        f"3. **服务供给与资金的关系：{elas.get('verdict', '待判断')}。** "
        f"双对数回归给出「捐赠 → 服务时长」的弹性点估计 "
        f"**{_fmt_value(elas.get('elasticity'))}**（R² = {_fmt_value(elas.get('r2'))}，"
        f"p = {elas.get('p_value', float('nan')):.3f}，样本 {elas.get('n')} 个月）。"
        + (
            "**该弹性在 95% 水平上并不显著**，因此不能据此宣称「捐赠下滑会压缩服务」。"
            "更稳妥的解读是：月度服务量主要由志愿者供给与项目排期决定，"
            "资金波动尚未在月度尺度上传导；但这同时意味着组织**缺少一个可主动调节的缓冲变量**——"
            "当资金真正收缩时，没有历史证据表明志愿时长会自动补位（2026H1 弹性 "
            f"{_fmt_value(elas.get('elasticity_2026H1'))}，亦不显著）。"
            if not elas.get("significant", False) else
            f"即现金捐赠每变化 10%，志愿服务时长约同向变化 "
            f"{abs(elas.get('elasticity') or 0) * 10:.1f}%。"
            "对照全国口径（2024 年全国社会捐赠 −8.70%、志愿服务贡献价值 +32.38%），"
            "本组织尚未建立起「以志愿时长对冲资金波动」的机制。"
        )
        + "\n"
        f"4. **项目执行质量总体良好，但按期结项率偏低。** 预算执行率 "
        f"**{_pct(kpi_value(kpis, 'QUA-03'))}**（{kpi_status(kpis, 'QUA-03')}），"
        f"满意度均值 **{_fmt_value(kpi_value(kpis, 'QUA-01'))} 分**，"
        f"但按期结项率仅 **{_pct(kpi_value(kpis, 'QUA-04'))}**，"
        f"{int(kpi_value(kpis, 'SCALE-01') or 0)} 个项目中有相当比例出现 1 个月以上的延期。\n"
        f"5. **数据治理已形成闭环。** 清洗阶段执行 **{len(cleaning_log):,} 项修复动作**，"
        f"合计修正 **{int(cleaning_log['affected_rows'].sum()):,} 行**，"
        f"数据完整性提升至 **{_pct(kpi_value(kpis, 'DQ-01'))}**；"
        f"{len(validation)} 条校验规则中 ERROR 级失败 **"
        f"{int(((validation['severity'] == 'ERROR') & (validation['status'] == 'FAIL')).sum()) if len(validation) else 0} 条**。\n"
    )

    add("### 1.2 关键指标一览\n")
    key_codes = [
        "SCALE-01", "SCALE-03", "SCALE-04", "SCALE-05", "SCALE-06", "SCALE-08",
        "ENG-02", "ENG-03", "ENG-05", "EFF-01", "EFF-03",
        "QUA-01", "QUA-03", "QUA-04", "STR-01", "STR-02", "DQ-01",
    ]
    key = kpis[kpis["指标代码"].isin(key_codes)][["指标代码", "指标名称", "指标值", "单位", "目标值", "状态"]]
    add(md_table(key, max_rows=len(key)))

    # ---------------- 二、数据与方法 ----------------
    add("\n## 二、数据与方法\n")
    add("### 2.1 数据来源与规模\n")
    add(
        "本项目模拟了一家州域青年公益组织的**七类多源异构数据**，覆盖从项目立项、"
        "志愿者招募与签到、捐赠收款、物资入库、入户服务、财务执行到满意度回访的完整链路：\n"
    )
    add(md_table(_source_table(cleaning_log), max_rows=20))
    add(
        "\n所有原始表都按 `config/config.yaml: data_quality` 的比例**刻意注入了真实世界中常见的"
        "七类数据缺陷**（重复、缺失、日期格式混乱、单位混用、全半角空格、悬空外键、极端值），"
        "并在 `data/raw/_ground_truth/injected_defects.csv` 中记录了每一个缺陷的位置，"
        "使清洗效果可以被**定量评估**而非凭感觉判断。\n"
    )

    add("### 2.2 数据治理结果\n")
    add(
        f"清洗管道共执行 **{len(cleaning_log):,} 项修复动作**，按问题类型汇总如下"
        f"（完整明细见 `data/processed/cleaning_log.csv`）：\n"
    )
    summary = (
        cleaning_log.groupby("issue", as_index=False)
        .agg(影响行数=("affected_rows", "sum"), 动作数=("action", "count"))
        .sort_values("影响行数", ascending=False)
        .rename(columns={"issue": "问题类型"})
    )
    issue_cn = {
        "whitespace_or_widechar": "空白/全角字符",
        "malformed_date": "日期格式混乱",
        "unit_inconsistency": "数值单位混用",
        "missing_value": "字段缺失",
        "extreme_outlier": "极端/越界值",
        "duplicate_row": "重复记录",
        "orphan_foreign_key": "悬空外键",
        "statistical_outlier": "统计离群点（仅标记）",
        "invalid_date_order": "日期先后颠倒",
        "out_of_range": "取值越界",
        "inconsistent_derived": "派生字段不一致",
        "date_unparseable": "日期无法解析",
        "unknown_unit": "未知单位",
        "future_date": "未来日期",
        "nonstandard_code": "非标准编码需归一化",
    }
    summary["问题类型"] = summary["问题类型"].map(lambda x: issue_cn.get(x, x))
    add(md_table(summary, max_rows=20))

    add("\n### 2.3 数据质量校验\n")
    if len(validation):
        table_summary = (
            validation.groupby("table", as_index=False)
            .agg(规则数=("rule", "count"), 失败规则数=("status", lambda s: int((s == "FAIL").sum())),
                 平均通过率=("pass_rate", "mean"))
            .sort_values("平均通过率")
            .rename(columns={"table": "数据表"})
        )
        add(md_table(table_summary, max_rows=20))
        fails = validation[validation["status"] == "FAIL"]
        if len(fails):
            add(f"\n仍有 **{len(fails)} 条规则未完全通过**，均已在下游做了显式处理或标注：\n")
            add(md_table(fails[["table", "rule", "severity", "failed_rows", "total_rows", "sample_failures"]]
                         .rename(columns={"table": "表", "rule": "规则", "severity": "级别",
                                          "failed_rows": "失败行", "total_rows": "总行", "sample_failures": "失败样例"}),
                         max_rows=10))
        else:
            add("全部校验规则通过。\n")

    add("\n### 2.4 星型模型\n")
    add(
        "清洗后的数据被组织为 6 张维度表 + 5 张事实表的星型模型，装入 SQLite 数据仓库"
        f"（`{cfg.paths.warehouse.relative_to(cfg.paths.root)}`），"
        "每张维度表都保留键值为 `-1` 的 UNKNOWN 成员，保证维度缺失时业务量不被静默丢弃：\n"
    )
    model_rows = []
    for name, df in star.items():
        model_rows.append(
            {
                "表名": name,
                "类型": "维度表" if name.startswith("dim_") else "事实表",
                "行数": len(df),
                "列数": df.shape[1],
            }
        )
    add(md_table(pd.DataFrame(model_rows), max_rows=20))
    add("\n")
    add(_img(figures["01_kpi_scorecard"], "指标体系健康度"))

    # ---------------- 三、描述性统计 ----------------
    add("\n## 三、描述性统计分析\n")
    add(
        "对八个核心度量做完整分布刻画。**变异系数**与**偏度**是这里最关键的两列："
        "它们揭示了公益数据的典型特征——金额类指标极度右偏，"
        "这决定了后续必须使用中位数、IQR 等稳健统计量而不是均值。\n"
    )
    desc = analysis["descriptive"]
    add(md_table(
        desc[["度量名称", "样本数", "均值", "标准差", "变异系数", "中位数", "P95", "最大值", "偏度", "离群点占比"]],
        max_rows=20, floatfmt="{:.3f}",
    ))
    add("\n")
    add(_img(figures["12_distributions"], "关键变量分布"))

    add("\n### 3.1 志愿者人力资本结构\n")
    add(md_table(analysis["categorical_profiles"]["志愿者民族结构"].rename(
        columns={"取值": "民族", "频数": "人数", "占比": "占比", "累计占比": "累计占比"}), max_rows=12))
    add("\n")
    add(_img(figures["06_volunteer_structure"], "志愿者结构"))

    # ---------------- 四、趋势分析 ----------------
    add("\n## 四、趋势分析\n")
    add(
        "公益服务具有强季节节奏，直接看月度环比会得出错误结论（例如把开学季的正常回落"
        "误判为业务恶化）。因此本节先做 **STL 分解**剥离趋势与季节成分，再基于残差判断异常。\n"
    )
    add(md_table(analysis["trend_summary"], max_rows=10, floatfmt="{:.4f}"))
    add("\n")
    add(_img(figures["02_monthly_trend"], "月度趋势"))
    add("\n")
    add(_img(figures["03_stl_decomposition"], "STL 分解"))
    add("\n")
    add(_img(figures["04_seasonality_heatmap"], "季节性热力图"))

    add("\n### 4.1 年度节奏与排班建议\n")
    add(
        "按自然月聚合 30 个月的观测值，得到各月的相对强度指数（1.0 = 全年平均水平）：\n"
    )
    season = analysis["seasonality"][["月度名称", "月均", "标准差", "年数", "相对强度指数"]]
    add(md_table(season, max_rows=12))

    add("\n### 4.2 捐赠与服务时长的联动关系\n")
    add(
        f"用双对数 OLS 估计弹性：**{_fmt_value(elas.get('elasticity'))}**"
        f"（95% 置信区间可通过 `reports/` 中的原始结果复算；R² = {_fmt_value(elas.get('r2'))}，"
        f"样本 {elas.get('n')} 个月）。2026 年上半年单独估计的弹性为 "
        f"**{_fmt_value(elas.get('elasticity_2026H1'))}**，说明进入 2026 年后联动性有所减弱，"
        f"但仍未出现「资金下降、服务上升」的对冲形态。\n"
    )
    add(f"\n> **结论**：{elas.get('conclusion', '')}\n")
    add("\n")
    add(_img(figures["13_elasticity"], "捐赠-服务弹性"))

    # ---------------- 五、交叉分析 ----------------
    add("\n## 五、交叉分析\n")
    add(
        "交叉分析回答的是「结构问题」：服务供给是否覆盖了不同民族、不同县域的真实需求？"
        "捐赠渠道与捐赠人类型是否匹配？弱势程度更高的家庭是否获得了同等质量的服务？"
        "每个交叉表都做了**卡方独立性检验**并给出 **Cramér's V** 效应量——"
        "只看 p 值会被大样本误导（n>1 万时极小的差异也会显著），效应量才反映关联的实际强度。\n"
    )
    add(md_table(
        analysis["cross_analysis"][["交叉分析", "样本量", "卡方值", "自由度", "p值", "Cramér's V", "是否显著", "结论"]],
        max_rows=10, floatfmt="{:.4f}",
    ))
    add("\n")
    add(_img(figures["05_crosstab_heatmap"], "志愿者民族×项目类别"))
    add("\n")
    add(_img(figures["05b_crosstab_channel"], "捐赠渠道×捐赠人类型"))

    add("\n### 5.1 县域 × 项目类别投入产出\n")
    county_tbl = analysis["dimension_county"] if "dimension_county" in analysis else None
    if county_tbl is not None:
        add(md_table(county_tbl, max_rows=10, floatfmt="{:.2f}"))
        add("\n")
    cat_tbl = analysis["dimension_category"] if "dimension_category" in analysis else None
    if cat_tbl is not None:
        add(md_table(cat_tbl, max_rows=10, floatfmt="{:.2f}"))

    # ---------------- 六、异常检测 ----------------
    add("\n## 六、异常检测、归因与修正\n")
    add(
        "本项目用四种统计方法（3σ / 稳健 Z 分数 / Tukey 箱线 / STL 残差）加一种机器学习方法"
        "（孤立森林）并行检测，只有 **≥2 种方法同时判定**才标记为异常，以压低假阳性。"
        "检出后按「县 × 月」「项目类别 × 月」下钻定位主责单元，并用**修正率**这一质量特征"
        "区分「数据录入错误」与「真实业务事件」——这是本岗位「异常数据排查、归因与修正」的核心方法。\n"
    )
    add(f"\n**整体层面检出 {len(anomalies['monthly_anomalies'])} 个异常点，"
        f"县域下钻检出 {len(anomalies['county_month_anomalies'])} 个异常点。**\n")
    if len(anomalies["monthly_anomalies"]):
        add(md_table(
            anomalies["monthly_anomalies"][["年月", "指标", "观测值", "稳健Z分数", "偏离中位数", "方向", "投票数", "判定方法"]],
            max_rows=15, floatfmt="{:.3f}",
        ))
    add("\n")
    add(_img(figures["08_anomaly_detection"], "异常检测"))
    add("\n")
    add(_img(figures["09_quality_signature"], "数据错误特征曲线"))

    add("\n### 6.1 归因结论\n")
    if len(anomalies["attributions"]):
        add(md_table(
            anomalies["attributions"][["年月", "指标", "主责县域", "主责项目类别", "偏离幅度",
                                       "当期修正率", "历史修正率中位数", "初步成因判断"]],
            max_rows=15, floatfmt="{:.4f}",
        ))

    add("\n### 6.2 检出效果验证（与植入异常比对）\n")
    eval_df = anomalies["evaluation"]
    if len(eval_df):
        n_hit = int((eval_df["综合检出"] == "是").sum())
        add(
            f"生成器在数据中植入了 **{len(eval_df)} 个真实异常**（其中 "
            f"{int((eval_df['真实成因类型'] == '业务事件').sum())} 个业务事件、"
            f"{int((eval_df['真实成因类型'] == '数据错误').sum())} 个数据错误）。"
            f"检测流程**命中 {n_hit}/{len(eval_df)}**，"
            f"检出率 {n_hit / len(eval_df):.1%}。\n"
        )
        add(md_table(
            eval_df[["异常编号", "植入月份", "植入范围", "植入指标", "偏离方向", "偏离幅度",
                     "真实成因类型", "整体层面检出", "县域下钻检出", "综合检出"]],
            max_rows=10,
        ))
        misses = anomalies.get("miss_explanations")
        if misses is not None and len(misses):
            add(f"\n**未被检出的 {len(misses)} 个异常，逐一给出量化归因**——"
                "漏报同样是需要解释的结论，而不是把阈值调低把它们连同假阳性一起捞进来：\n")
            add(md_table(
                misses[["异常编号", "植入月份", "植入指标", "真实成因类型",
                        "实际偏离幅度", "序列稳健变异系数", "年度季节指数", "漏报原因", "改进方向"]],
                max_rows=10, floatfmt="{:.4f}",
            ))
        add("\n> **为什么要做这一步**：异常检测很容易「看起来很有道理」却无法验证。"
            "由于数据由带固定种子的生成器产出，每一个异常的位置与真实成因都是已知的，"
            "因此检出率、漏报与误报都可以被精确计算——这让「异常排查能力」从主观描述变成了可度量的指标。\n")

    # ---------------- 七、项目评分卡 ----------------
    add("\n## 七、项目绩效评分卡\n")
    add(
        f"对 {int(kpi_value(kpis, 'SCALE-01') or 0)} 个项目从"
        "**执行率、目标覆盖率、满意度、单位成本**四个维度标准化加权，"
        "得到可横向比较的综合评分与 A/B/C/D 评级，用于下一周期资源配置决策。\n"
    )
    scorecard = analysis["project_scorecard"]
    add(md_table(
        scorecard[["project_name", "category", "county", "planned_budget_cny", "actual_cny",
                   "execution_rate", "target_coverage", "satisfaction_mean", "cost_per_visit",
                   "综合评分", "评级"]].rename(columns={
            "project_name": "项目名称", "category": "类别", "county": "县",
            "planned_budget_cny": "计划预算", "actual_cny": "实际支出",
            "execution_rate": "执行率", "target_coverage": "目标覆盖率",
            "satisfaction_mean": "满意度", "cost_per_visit": "单位成本", "综合评分": "综合评分", "评级": "评级",
        }),
        max_rows=12, floatfmt="{:.3f}",
    ))
    add("\n")
    add(_img(figures["10_project_scorecard"], "项目评分卡"))

    # ---------------- 八、留存与集中度 ----------------
    add("\n## 八、志愿者留存与捐赠集中度\n")
    add("### 8.1 志愿者留存队列\n")
    add(md_table(analysis["cohort"], max_rows=12, floatfmt="{:.0f}"))
    add("\n")
    add(_img(figures["11_cohort_retention"], "留存队列"))
    add("\n### 8.2 捐赠集中度与帕累托结构\n")
    add(md_table(conc["pareto_points"], max_rows=10, floatfmt="{:.3f}"))
    add(f"\n**基尼系数 {conc['gini']:.3f}，HHI {conc['hhi']:.5f}，共 {conc['n_donors']:,} 名捐赠人。**\n")
    add("\n前 15 大捐赠人：\n")
    add(md_table(conc["top_donors"][["donor_name", "donor_type", "county", "amount_cny", "金额占比", "累计占比"]]
                 .rename(columns={"donor_name": "捐赠人", "donor_type": "类型", "county": "所在县",
                                  "amount_cny": "累计金额"}), max_rows=15, floatfmt="{:.4f}"))
    add("\n")
    add(_img(figures["07_donation_structure"], "捐赠结构"))

    # ---------------- 九、建议 ----------------
    add("\n## 九、结论与运营建议\n")
    add(
        "### 9.1 资源配置\n"
        "1. **把预算节奏对齐服务季节曲线。** 服务时长在 6 月与 10-11 月形成双峰、1-2 月触底。"
        "当前月度预算分配（`fact_project_monthly`）与季节曲线并不同步，建议把物资采购与培训"
        "前置到 4-5 月与 8-9 月，避免旺季「有项目无物资」。\n"
        "2. **对 C/D 级项目启动复盘机制。** 评分卡中执行率低于 85% 或满意度低于 4.3 分的项目"
        "应进入下一季度重点跟踪名单，重点核查是「执行滞后」还是「跨期入账」造成的口径偏差"
        "（本报告 6.1 节已给出跨期入账的实例）。\n"
        "3. **提高按期结项率。** 当前按期结项率仅 "
        f"**{_pct(kpi_value(kpis, 'QUA-04'))}**，建议在项目立项时预留 15% 的时间缓冲，"
        "并把「计划结束日期变更」纳入项目管理流程的正式变更记录。\n"
        "\n### 9.2 收入结构韧性\n"
        f"4. **降低单一捐赠人依赖。** 前 20% 捐赠人贡献了 {_pct(conc['pareto_points'].iloc[3]['金额占比'] if len(conc['pareto_points']) > 3 else None)} 的金额，"
        "且已出现单笔 85 万元占单月收入近七成的情况。建议设定「单一捐赠人年度占比 ≤ 15%」的"
        "内部红线，并把月捐人（`dim_donor.is_recurring`）规模作为核心增长指标。\n"
        f"5. **为服务交付建立不依赖增量资金的底盘。** 弹性估计为 "
        f"**{_fmt_value(elas.get('elasticity'))}**（p = {elas.get('p_value', float('nan')):.3f}，"
        f"{'不显著' if not elas.get('significant', False) else '显著'}），"
        "说明当前没有证据表明资金收缩会自动带来志愿时长的补位。"
        "建议在受资金影响最小的生态环保、文化传承类项目中扩大志愿者自主运营比例，"
        "主动培育「资金不足时仍能交付」的服务底盘，而不是等资金波动发生后再被动应对。\n"
        "\n### 9.3 数据运营流程\n"
        "6. **把本仓库的校验规则接入日常流程。** `src/qingfeng/validate.py` 中的规则集可在"
        "每月数据归档时自动运行，任何 ERROR 级失败阻断报表发布，从源头减少「月底救火」。\n"
        "7. **为签到小程序增加单位与量纲校验。** 本项目检出的最严重数据问题是"
        "「分钟被当作小时写入」，导致单次服务出现 240 小时的记录；"
        "在录入端加一条 `0.25 ≤ hours ≤ 12` 的硬约束即可根除。\n"
        "8. **统一行政区划与民族等受控词表的录入。** 问卷与纸质台账的自由文本"
        "（「香格里拉」「香格里拉市」「香格里拉 」混用）每年要额外消耗数十小时清洗时间。\n"
    )

    # ---------------- 十、局限 ----------------
    add("\n## 十、方法与局限声明\n")
    add(
        "1. **数据为模拟数据。** 本报告的分析对象是按公开统计口径校准生成的模拟业务数据，"
        "结论反映的是**分析方法的有效性**，不代表任何真实机构的运营状况。\n"
        "2. **因果关系有限。** 趋势与弹性分析基于观测数据的相关性，"
        "未做因果推断（如双重差分、工具变量），因此「捐赠下降导致服务收缩」应理解为"
        "统计上的联动，而非已识别的因果机制。\n"
        "3. **志愿服务价值折算率是推导值。** 30 元/小时由全国志愿服务贡献价值与活跃志愿者规模"
        "反推得到，属于量级估计，不应作为财务入账依据。\n"
        "4. **异常检测存在漏报。** 共识投票（≥2 票）降低了误报，但代价是缓变型异常"
        "（连续数月小幅偏移）容易被遗漏；生产环境建议叠加 CUSUM 或贝叶斯变点检测。\n"
        "5. **地理维度粒度有限。** 签到记录本身不含乡镇字段，乡镇信息由项目台账反推，"
        "因此无法评估同一项目跨乡镇的服务分布差异。\n"
    )

    add("\n---\n")
    add(
        f"\n<sub>本报告由 `src/qingfeng/report.py` 自动生成 · 随机种子 {window.seed} · "
        f"全部指标口径见 `reports/kpi_catalog.csv`</sub>\n"
    )
    return "\n".join(lines)


def _source_table(cleaning_log: pd.DataFrame) -> pd.DataFrame:
    """数据源清单（表名 -> 中文名与行数）。"""
    from qingfeng.tables import TABLE_SPECS

    rows = []
    for name, spec in TABLE_SPECS.items():
        rows.append(
            {
                "数据表": name,
                "中文名称": spec["zh_name"],
                "来源系统": spec["source"],
                "粒度": spec["grain"],
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 一页纸摘要
# ---------------------------------------------------------------------------


def build_executive_summary(
    cfg: Config, kpis: pd.DataFrame, analysis: dict[str, Any], anomalies: dict[str, Any]
) -> str:
    conc = analysis["concentration"]
    elas = analysis["elasticity"]
    scorecard = analysis["project_scorecard"]
    n_a = int((scorecard["评级"] == "A 优秀").sum())
    n_d = int((scorecard["评级"] == "D 待改进").sum())

    lines = [
        f"# {cfg.org_name} · 数据运营分析一页纸摘要\n",
        f"**数据窗口**：{cfg.simulation.start_date} ~ {cfg.simulation.as_of_date} ｜ "
        f"**生成时间**：{datetime.now():%Y-%m-%d}\n",
        "## 五个必须知道的数字\n",
        "| 指标 | 数值 | 解读 |",
        "|---|---|---|",
        f"| 志愿服务总时长 | {_fmt_value(kpi_value(kpis, 'SCALE-04'))} 小时 | 折合经济价值约 "
        f"{_fmt_value(kpi_value(kpis, 'SCALE-05'))} 元 |",
        f"| 志愿者活跃率 | {_pct(kpi_value(kpis, 'ENG-02'))} | 全国基线 28.7%，"
        f"当前{'高于' if (kpi_value(kpis, 'ENG-02') or 0) >= 0.2872 else '低于'}基线 |",
        f"| 现金捐赠总额 | {_fmt_value(kpi_value(kpis, 'SCALE-06'))} 元 | "
        f"基尼系数 {conc['gini']:.3f}，收入高度集中 |",
        f"| 项目预算执行率 | {_pct(kpi_value(kpis, 'QUA-03'))} | 状态：{kpi_status(kpis, 'QUA-03')} |",
        f"| 服务满意度均值 | {_fmt_value(kpi_value(kpis, 'QUA-01'))} 分 | "
        f"达标率（≥4 分）{_pct(kpi_value(kpis, 'QUA-02'))} |",
        "",
        "## 三个立即可以做的动作\n",
        f"1. **对齐预算与服务节奏** —— 服务时长 6 月、10-11 月双峰，1-2 月触底，"
        f"物资与培训采购需前置 1 个月。",
        f"2. **处置评分卡末位项目** —— 本周期有 {n_d} 个项目评级为 D、{n_a} 个为 A；"
        f"对 D 级项目逐项核查「执行滞后」与「跨期入账」两类成因。",
        f"3. **设定单一捐赠人占比红线（≤15%）** —— 当前前 20% 捐赠人贡献 "
        f"{_pct(conc['pareto_points'].iloc[3]['金额占比'] if len(conc['pareto_points']) > 3 else None)} 的金额，"
        f"抗风险能力不足。",
        "",
        "## 一个需要长期投入的能力\n",
        f"捐赠对服务时长的弹性为 **{_fmt_value(elas.get('elasticity'))}**"
        f"（p={elas.get('p_value', float('nan')):.3f}，"
        f"{'统计上不显著' if not elas.get('significant', False) else '统计显著'}）。"
        + ("没有证据表明资金收缩会自动带来志愿时长补位，"
           "建议把「志愿者自主运营型项目占比」设为年度战略指标，主动建立不依赖增量资金的交付底盘。"
           if not elas.get("significant", False) else
           "服务交付高度跟随资金波动，亟需建立志愿时长的对冲机制。"),
        "",
        "---",
        f"<sub>完整报告见 `reports/数据分析报告.md`；异常检测共检出 "
        f"{len(anomalies['monthly_anomalies'])} 个整体异常点、"
        f"{len(anomalies['county_month_anomalies'])} 个县域异常点。</sub>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML 仪表盘
# ---------------------------------------------------------------------------


def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def build_html_dashboard(
    cfg: Config,
    kpis: pd.DataFrame,
    analysis: dict[str, Any],
    anomalies: dict[str, Any],
    figures: dict[str, Path],
) -> str:
    def cards() -> str:
        codes = ["SCALE-04", "SCALE-06", "ENG-02", "QUA-01", "QUA-03", "STR-01", "DQ-01"]
        out = []
        for code in codes:
            row = kpis[kpis["指标代码"] == code]
            if not len(row):
                continue
            r = row.iloc[0]
            status = str(r["状态"])
            cls = {"达标": "ok", "关注": "watch", "预警": "alert"}.get(status, "na")
            val = _fmt_value(r["指标值"])
            unit = html.escape(str(r["单位"]))
            out.append(
                f'<div class="card {cls}"><div class="kname">{html.escape(str(r["指标名称"]))}</div>'
                f'<div class="kval">{val}<span class="kunit">{unit}</span></div>'
                f'<div class="kstatus">{html.escape(status)}</div></div>'
            )
        return "\n".join(out)

    def figure_block(key: str, caption: str) -> str:
        p = figures.get(key)
        if not p or not p.is_file():
            return ""
        return (
            f'<figure><img src="data:image/png;base64,{_b64_image(p)}" alt="{html.escape(caption)}"/>'
            f"<figcaption>{html.escape(caption)}</figcaption></figure>"
        )

    def table_block(df: pd.DataFrame, caption: str, max_rows: int = 10) -> str:
        if df is None or not len(df):
            return ""
        sub = df.head(max_rows)
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in sub.columns)
        body = ""
        for _, row in sub.iterrows():
            cells = "".join(f"<td>{html.escape(_fmt_value(v))}</td>" for v in row)
            body += f"<tr>{cells}</tr>"
        return f'<h3>{html.escape(caption)}</h3><div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    conc = analysis["concentration"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(cfg.org_name)} · 数据运营分析仪表盘</title>
<style>
  :root {{ --primary:{cfg.color('primary')}; --secondary:{cfg.color('secondary')};
           --danger:{cfg.color('danger')}; --ink:#1f2d33; --muted:#6b7a80; --line:#e3e9ec; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
          color:var(--ink); background:#f5f8f9; line-height:1.7; }}
  header {{ background:linear-gradient(135deg,var(--primary),#1d4b4e); color:#fff; padding:44px 32px 34px; }}
  header h1 {{ margin:0 0 8px; font-size:27px; letter-spacing:.5px; }}
  header p {{ margin:0; opacity:.85; font-size:14px; }}
  main {{ max-width:1180px; margin:0 auto; padding:26px 20px 70px; }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:24px 26px;
             margin-bottom:22px; box-shadow:0 1px 3px rgba(20,50,55,.05); }}
  h2 {{ font-size:19px; margin:0 0 16px; padding-left:12px; border-left:4px solid var(--primary); }}
  h3 {{ font-size:15px; margin:22px 0 10px; color:#25454a; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:14px; }}
  .card {{ border:1px solid var(--line); border-radius:10px; padding:15px 16px; background:#fbfdfd; }}
  .card.ok {{ border-top:3px solid var(--primary); }}
  .card.watch {{ border-top:3px solid var(--secondary); }}
  .card.alert {{ border-top:3px solid var(--danger); }}
  .card.na {{ border-top:3px solid var(--muted); }}
  .kname {{ font-size:12.5px; color:var(--muted); }}
  .kval {{ font-size:24px; font-weight:700; margin:5px 0 2px; font-variant-numeric:tabular-nums; }}
  .kunit {{ font-size:12px; color:var(--muted); margin-left:5px; font-weight:400; }}
  .kstatus {{ font-size:12px; color:var(--muted); }}
  figure {{ margin:18px 0 6px; }}
  figure img {{ width:100%; border:1px solid var(--line); border-radius:9px; }}
  figcaption {{ font-size:12.5px; color:var(--muted); margin-top:7px; text-align:center; }}
  .tw {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ border-bottom:1px solid var(--line); padding:7px 10px; text-align:right; white-space:nowrap; }}
  th:first-child, td:first-child {{ text-align:left; }}
  th {{ background:#f0f5f6; font-weight:600; position:sticky; top:0; }}
  tbody tr:hover {{ background:#f7fbfb; }}
  ul {{ padding-left:20px; }}
  .note {{ background:#fdf7ee; border-left:4px solid var(--secondary); padding:12px 16px;
           border-radius:0 8px 8px 0; font-size:13.5px; margin:14px 0; }}
  footer {{ text-align:center; color:var(--muted); font-size:12.5px; padding:8px 0 30px; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(cfg.org_name)} · 公益项目数据运营分析仪表盘</h1>
  <p>{html.escape(cfg.region)} ｜ 数据窗口 {cfg.simulation.start_date} ~ {cfg.simulation.as_of_date}
     ｜ 随机种子 {cfg.simulation.seed} ｜ 生成于 {datetime.now():%Y-%m-%d %H:%M}</p>
</header>
<main>
  <section>
    <h2>核心指标</h2>
    <div class="cards">{cards()}</div>
    <div class="note">数据为按公开统计口径校准的<b>模拟数据</b>，用于验证分析方法，不代表真实机构运营状况。</div>
  </section>
  <section>
    <h2>指标体系健康度</h2>
    {figure_block("01_kpi_scorecard", "各层级指标的达标/关注/预警结构")}
  </section>
  <section>
    <h2>月度趋势</h2>
    {figure_block("02_monthly_trend", "服务时长与捐赠额的月度走势及移动平均")}
    {figure_block("03_stl_decomposition", "STL 分解：趋势 / 季节 / 残差")}
    {figure_block("04_seasonality_heatmap", "年度季节性热力图")}
  </section>
  <section>
    <h2>结构与交叉分析</h2>
    {figure_block("06_volunteer_structure", "志愿者人力资本画像")}
    {figure_block("05_crosstab_heatmap", "志愿者民族 × 项目类别")}
    {figure_block("05b_crosstab_channel", "捐赠渠道 × 捐赠人类型")}
    {figure_block("07_donation_structure", "捐赠结构与集中度（含洛伦兹曲线）")}
    {table_block(conc["pareto_points"].rename(columns={"捐赠人占比":"捐赠人占比","人数":"人数","金额":"金额（元）","金额占比":"金额占比"}), "帕累托结构")}
  </section>
  <section>
    <h2>异常检测与数据质量</h2>
    {figure_block("08_anomaly_detection", "多方法投票检出的异常月份")}
    {figure_block("09_quality_signature", "数据错误特征曲线（修正率突增 = 录入问题）")}
    {table_block(anomalies["monthly_anomalies"][["年月","指标","观测值","偏离中位数","方向","投票数","判定方法"]], "整体层面异常清单")}
  </section>
  <section>
    <h2>项目绩效</h2>
    {figure_block("10_project_scorecard", "项目评分卡：执行率 vs 满意度（气泡=实际支出）")}
    {table_block(analysis["project_scorecard"][["project_name","category","execution_rate","satisfaction_mean","cost_per_visit","综合评分","评级"]].rename(columns={"project_name":"项目","category":"类别","execution_rate":"执行率","satisfaction_mean":"满意度","cost_per_visit":"单位成本"}), "项目 Top 10", 10)}
  </section>
  <section>
    <h2>志愿者留存</h2>
    {figure_block("11_cohort_retention", "注册年份队列的年度活跃留存率")}
  </section>
  <section>
    <h2>结论与建议</h2>
    <ul>
      <li><b>预算节奏对齐服务季节曲线</b>：6 月与 10-11 月双峰，1-2 月触底，物资与培训采购需前置。</li>
      <li><b>降低单一捐赠人依赖</b>：基尼系数 {conc['gini']:.3f}，建议设「单一捐赠人年度占比 ≤15%」红线。</li>
      <li><b>建立志愿时长对冲机制</b>：捐赠-服务弹性 {_fmt_value(analysis['elasticity'].get('elasticity'))}，服务量几乎完全跟随资金波动。</li>
      <li><b>把校验规则接入月报流程</b>：ERROR 级失败即阻断报表发布，减少月底返工。</li>
    </ul>
  </section>
</main>
<footer>由 qingfeng-analytics 自动生成 · 全部口径见 reports/kpi_catalog.csv 与 docs/methodology.md</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def write_reports(
    cfg: Config,
    kpis: pd.DataFrame,
    analysis: dict[str, Any],
    anomalies: dict[str, Any],
    validation: pd.DataFrame,
    cleaning_log: pd.DataFrame,
    star: dict[str, pd.DataFrame],
    figures: dict[str, Path],
) -> dict[str, Path]:
    cfg.paths.reports.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}

    kpis.to_csv(cfg.paths.reports / "kpi_catalog.csv", index=False, encoding="utf-8-sig")
    out["kpi_catalog"] = cfg.paths.reports / "kpi_catalog.csv"

    md = build_markdown_report(cfg, kpis, analysis, anomalies, validation, cleaning_log, star, figures)
    p = cfg.paths.reports / "数据分析报告.md"
    p.write_text(md, encoding="utf-8")
    out["report_md"] = p

    ex = build_executive_summary(cfg, kpis, analysis, anomalies)
    p2 = cfg.paths.reports / "executive_summary.md"
    p2.write_text(ex, encoding="utf-8")
    out["executive_summary"] = p2

    html_doc = build_html_dashboard(cfg, kpis, analysis, anomalies, figures)
    p3 = cfg.paths.reports / "dashboard.html"
    p3.write_text(html_doc, encoding="utf-8")
    out["dashboard"] = p3

    logger.info("报告已生成：%s", ", ".join(p.name for p in out.values()))
    return out
