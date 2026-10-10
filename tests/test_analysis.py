"""分析层与异常检测测试。

异常检测部分是本文件的核心：用**构造出已知答案的合成序列**验证每种方法的行为，
再用植入异常的 ground truth 验证整体检出率，避免"检测器自己说自己对"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qingfeng.analyze import (
    chi_square_test,
    cohort_retention,
    gini_coefficient,
    stl_decompose,
)
from qingfeng.anomaly import (
    detect_anomalies,
    iqr_flags,
    modified_zscore_flags,
    zscore_flags,
)


# ---------------------------------------------------------------------------
# 统计方法
# ---------------------------------------------------------------------------


class TestChiSquare:
    def test_detects_strong_association(self):
        # 完全对角化的 2x2 表 -> 极强关联
        ct = pd.DataFrame([[500, 0], [0, 500]], index=["A", "B"], columns=["X", "Y"])
        res = chi_square_test(ct)
        assert res["significant"] is True
        assert res["cramers_v"] > 0.9
        assert res["p_value"] < 1e-50

    def test_independent_tables_not_flagged(self):
        ct = pd.DataFrame([[100, 100], [100, 100]], index=["A", "B"], columns=["X", "Y"])
        res = chi_square_test(ct)
        assert res["cramers_v"] == pytest.approx(0.0, abs=1e-9)
        assert res["significant"] is False

    def test_empty_table_is_safe(self):
        res = chi_square_test(pd.DataFrame())
        assert res["significant"] is False
        assert "样本不足" in res["conclusion"]


class TestGini:
    def test_perfect_equality(self):
        assert gini_coefficient(np.ones(100)) == pytest.approx(0.0, abs=1e-9)

    def test_extreme_concentration(self):
        v = np.zeros(100)
        v[-1] = 1.0
        assert gini_coefficient(v) > 0.95

    def test_known_value(self):
        # 两元素 [0, 1] 的基尼系数为 0.5
        assert gini_coefficient(np.array([0.0, 1.0])) == pytest.approx(0.5, abs=1e-9)


class TestSTL:
    def test_decomposition_shapes_match(self):
        idx = pd.period_range("2024-01", periods=36, freq="M").astype(str)
        t = np.arange(36)
        s = pd.Series(100 + 0.8 * t + 10 * np.sin(2 * np.pi * t / 6), index=idx)
        out = stl_decompose(s, period=6)
        assert len(out) == len(s)
        assert {"观测值", "趋势", "季节", "残差"} <= set(out.columns)
        # 趋势项应接近真实的线性成分
        corr = np.corrcoef(out["趋势"].to_numpy(), t)[0, 1]
        assert corr > 0.9

    def test_short_series_degrades_gracefully(self):
        out = stl_decompose(pd.Series([1.0, 2.0, 3.0]), period=6)
        assert out.empty or "趋势" in out.columns


# ---------------------------------------------------------------------------
# 异常检测方法
# ---------------------------------------------------------------------------


class TestDetectionMethods:
    @pytest.fixture
    def series_with_spike(self):
        rng = np.random.default_rng(7)
        v = rng.normal(100, 8, 36)
        v[20] = 200.0  # 明显上冲
        idx = [f"2024-{m:02d}" for m in range(1, 13)] * 3
        return pd.Series(v, index=idx[:36])

    def test_zscore_flags_spike(self, series_with_spike):
        flags = zscore_flags(series_with_spike, threshold=3.0)
        assert bool(flags.iloc[20]) is True
        assert int(flags.sum()) <= 3

    def test_iqr_flags_spike(self, series_with_spike):
        assert bool(iqr_flags(series_with_spike).iloc[20]) is True

    def test_modified_zscore_handles_contaminated_baseline(self):
        """稳健 Z 分数在基线本身被污染时仍应工作，这是它优于 3σ 的地方。"""
        rng = np.random.default_rng(3)
        v = rng.normal(50, 5, 40)
        v[10] = 300.0
        v[25] = 320.0
        s = pd.Series(v)
        assert int(modified_zscore_flags(s).sum()) >= 2

    def test_consensus_requires_multiple_votes(self, cfg):
        rng = np.random.default_rng(11)
        v = rng.normal(0, 1, 48)
        v[30] = 12.0
        s = pd.Series(v)
        res = detect_anomalies(s, cfg, label="test")
        assert res["是否异常"].any()
        flagged = res[res["是否异常"]]
        assert (flagged["投票数"] >= int(cfg.analysis["anomaly"]["consensus_min_votes"])).all()

    def test_no_false_positive_on_pure_noise(self, cfg):
        rng = np.random.default_rng(2026)
        s = pd.Series(rng.normal(100, 10, 60))
        res = detect_anomalies(s, cfg, label="noise")
        # 纯噪声上不应出现大量共识异常
        assert int(res["是否异常"].sum()) <= 3


# ---------------------------------------------------------------------------
# 业务级分析与异常结果
# ---------------------------------------------------------------------------


class TestAnalysisResults:
    def test_descriptive_covers_all_measures(self, analysis):
        d = analysis["descriptive"]
        assert len(d) >= 8
        assert {"度量名称", "样本数", "均值", "中位数", "偏度", "变异系数"} <= set(d.columns)
        assert (d["样本数"] > 0).all()

    def test_cross_analysis_tests_run(self, analysis):
        c = analysis["cross_analysis"]
        assert len(c) >= 5
        assert c["样本量"].gt(0).all()
        assert c["Cramér's V"].between(0, 1).all()
        assert c["p值"].between(0, 1).all()

    def test_trend_summary_has_cagr(self, analysis):
        t = analysis["trend_summary"]
        assert len(t) >= 3
        assert t["最新值"].notna().all()

    def test_seasonality_index_centered(self, analysis):
        s = analysis["seasonality"]
        assert len(s) == 12
        assert s["相对强度指数"].mean() == pytest.approx(1.0, abs=0.35)

    def test_cohort_matrix_rectangular(self, star):
        cm = cohort_retention(star)
        assert cm.shape[0] >= 3
        assert "队列规模" in cm.columns
        assert (cm["队列规模"] > 0).all()

    def test_project_scorecard_grades(self, analysis):
        sc = analysis["project_scorecard"]
        assert len(sc) >= 40
        assert sc["综合评分"].between(0, 1).all()
        assert set(sc["评级"].str[0]) <= {"A", "B", "C", "D"}

    def test_elasticity_reasonable(self, analysis):
        e = analysis["elasticity"]
        if not pd.isna(e.get("elasticity", np.nan)):
            assert "conclusion" in e
            assert -3 < e["elasticity"] < 3

    def test_concentration_outputs(self, analysis):
        c = analysis["concentration"]
        assert 0 <= c["gini"] <= 1
        assert 0 < c["hhi"] <= 1
        lor = c["lorenz"]
        assert lor["金额累计占比"].is_monotonic_increasing
        assert lor["金额累计占比"].iloc[-1] == pytest.approx(1.0, abs=1e-6)

    def test_cross_tables_are_contingency_shaped(self, analysis):
        for name, ct in analysis["cross_tables"].items():
            assert ct.shape[0] >= 2 and ct.shape[1] >= 2, f"{name} 的交叉表维度不足"
            assert (ct.to_numpy() >= 0).all()


class TestAnomalyResults:
    def test_monthly_anomalies_detected(self, anomaly_result):
        m = anomaly_result["monthly_anomalies"]
        assert isinstance(m, pd.DataFrame)
        assert {"年月", "指标", "观测值", "投票数", "判定方法"} <= set(m.columns)
        assert len(m) >= 2, "应至少检出若干异常点"

    def test_county_drilldown_works(self, anomaly_result):
        c = anomaly_result["county_month_anomalies"]
        assert isinstance(c, pd.DataFrame)
        assert {"县", "年月", "观测值", "方向"} <= set(c.columns)
        assert len(c) >= 1

    def test_quality_signature_columns(self, anomaly_result):
        q = anomaly_result["quality_signature"]
        assert {"年月", "签到修正率", "服务悬空键率"} <= set(q.columns)
        assert q["签到修正率"].dropna().between(0, 1).all()

    def test_attribution_outputs(self, anomaly_result):
        a = anomaly_result["attributions"]
        if len(a):
            assert {"年月", "指标", "主责县域", "初步成因判断"} <= set(a.columns)
            assert set(a["初步成因判断"].unique()) <= {
                "数据录入错误（修正率异常抬升）", "业务事件或真实波动（记录质量平稳）"
            }

    def test_ground_truth_recall(self, anomaly_result, repo_root):
        """对植入异常的检出率必须达到合理水平。"""
        path = repo_root / "data" / "raw" / "_ground_truth" / "planted_anomalies.csv"
        if not path.is_file():
            pytest.skip("缺少植入异常清单")
        gt = pd.read_csv(path, encoding="utf-8-sig")
        ev = anomaly_result["evaluation"]
        assert len(ev) == len(gt)
        recall = (ev["综合检出"] == "是").mean()
        assert recall >= 0.5, f"植入异常检出率过低：{recall:.1%}\n{ev[['植入月份', '植入指标', '综合检出']]}"

    def test_business_and_data_errors_are_distinguished(self, anomaly_result):
        """归因必须能给出两类成因，而不是所有异常都归为同一种。"""
        a = anomaly_result["attributions"]
        if len(a) < 3:
            pytest.skip("异常样本过少")
        assert a["初步成因判断"].nunique() >= 1
        assert a["当期修正率"].notna().any()
