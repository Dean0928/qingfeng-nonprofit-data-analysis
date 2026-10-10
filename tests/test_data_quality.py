"""数据质量层测试：清洗函数的单元行为 + 基于 ground truth 的修复效果评估。

这一组测试是本项目"可验证性"的关键：生成器记录了每一个被注入的数据缺陷，
因此清洗模块的效果可以被**定量打分**，而不是靠肉眼观察几张表。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qingfeng.clean import (
    DATE_FORMATS,
    UNKNOWN,
    CleaningLog,
    clip_to_physical_range,
    drop_duplicates,
    normalize_text,
    parse_mixed_dates,
    parse_quantity_with_unit,
    resolve_orphan_keys,
)
from qingfeng.tables import ALL_TABLES


# ---------------------------------------------------------------------------
# 单元行为
# ---------------------------------------------------------------------------


class TestNormalizeText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  香格里拉市  ", "香格里拉市"),
            ("（维西傈僳族自治县）", "(维西傈僳族自治县)"),
            ("维西-中路乡", "维西-中路乡"),
            ("全角１２３", "全角123"),
            ("多   空格", "多 空格"),
            (None, None),
            (123, 123),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_text(raw) == expected


class TestParseMixedDates:
    @pytest.mark.parametrize("fmt", DATE_FORMATS)
    def test_each_supported_format(self, fmt):
        target = pd.Timestamp("2026-06-15")
        s = pd.Series([target.strftime(fmt)])
        log = CleaningLog()
        parsed, matched = parse_mixed_dates(s, "t", "c", log)
        assert parsed.iloc[0] == target, f"格式 {fmt} 解析失败"
        assert matched.iloc[0] == fmt

    def test_mixed_column(self):
        s = pd.Series(["2026-06-15", "2026/06/16", "2026年6月17日", "2026.06.18", "", None])
        log = CleaningLog()
        parsed, _ = parse_mixed_dates(s, "t", "c", log)
        assert parsed.iloc[:4].notna().all()
        assert parsed.iloc[0].day == 15
        assert parsed.iloc[3].day == 18

    def test_logs_action(self):
        log = CleaningLog()
        parse_mixed_dates(pd.Series(["2026/06/15"]), "t", "c", log)
        assert any(a.issue == "malformed_date" for a in log.actions)


class TestParseQuantityWithUnit:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3.5", 3.5),
            ("3.5h", 3.5),
            ("3.5 小时", 3.5),
            ("210min", 3.5),
            ("210分钟", 3.5),
            ("12600s", 3.5),
            (3.5, 3.5),
        ],
    )
    def test_hours(self, raw, expected):
        log = CleaningLog()
        values, _units = parse_quantity_with_unit(pd.Series([raw]), "t", "hours", log, kind="hours")
        assert values.iloc[0] == pytest.approx(expected, abs=1e-6)

    def test_quantity_strips_unit(self):
        log = CleaningLog()
        values, units = parse_quantity_with_unit(
            pd.Series(["5件", "12箱", 7]), "t", "quantity", log, kind="quantity"
        )
        assert values.tolist() == [5.0, 12.0, 7.0]
        assert units.iloc[0] == "件"

    def test_unknown_unit_is_preserved_and_flagged(self):
        log = CleaningLog()
        values, units = parse_quantity_with_unit(
            pd.Series(["3石头"]), "t", "quantity", log, kind="hours"
        )
        assert values.iloc[0] == 3.0
        assert units.iloc[0].startswith("?")


class TestDuplicatesAndKeys:
    def test_business_key_dedup(self):
        df = pd.DataFrame(
            {
                "shift_id": ["A", "A", "B"],
                "volunteer_id": ["V1", "V1", "V2"],
                "service_date": ["2026-01-01"] * 3,
            }
        )
        log = CleaningLog()
        out = drop_duplicates(df, "t", log, business_keys=["shift_id"])
        assert len(out) == 2
        assert log.to_frame()["issue"].tolist() == ["duplicate_row"]

    def test_orphan_to_unknown(self):
        df = pd.DataFrame({"volunteer_id": ["V1", "V9", "V2"]})
        log = CleaningLog()
        out, flags = resolve_orphan_keys(df, "volunteer_id", ["V1", "V2"], "t", log)
        assert out["volunteer_id"].tolist() == ["V1", UNKNOWN, "V2"]
        assert int(flags.sum()) == 1
        assert any(a.issue == "orphan_foreign_key" for a in log.actions)

    def test_clip_physical_range(self):
        df = pd.DataFrame({"hours": [3.0, -5.0, 240.0]})
        log = CleaningLog()
        out, flags = clip_to_physical_range(df, "hours", 0.25, 12.0, "t", log)
        assert out["hours"].tolist() == [3.0, 0.25, 12.0]
        assert int(flags.sum()) == 2


# ---------------------------------------------------------------------------
# 基于 ground truth 的修复效果评估
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ground_truth(repo_root):
    path = repo_root / "data" / "raw" / "_ground_truth" / "injected_defects.csv"
    if not path.is_file():
        pytest.skip("缺少注入缺陷清单，请先运行 `make data`")
    return pd.read_csv(path, encoding="utf-8-sig")


class TestRepairEffectiveness:
    """对每一类注入缺陷，断言清洗后的数据满足业务约束。"""

    def test_duplicates_fully_removed(self, cleaned_data, ground_truth, raw_data):
        n_injected = int((ground_truth["defect_type"] == "duplicate_row").sum())
        assert n_injected > 0, "生成器未注入重复记录，测试无意义"
        # 所有表的主键 / 业务键在清洗后必须唯一
        from qingfeng.tables import business_keys, primary_key

        for name in ALL_TABLES:
            df = cleaned_data[name]
            pk = primary_key(name)
            if pk:
                assert not df[pk].duplicated().any(), f"{name}.{pk} 仍存在重复"
            bk = [k for k in business_keys(name) if k in df.columns]
            if bk:
                assert not df.duplicated(subset=bk).any(), f"{name} 业务键 {bk} 仍存在重复"

    def test_dates_all_parseable(self, cleaned_data):
        from qingfeng.tables import date_columns

        for name in ALL_TABLES:
            for col in date_columns(name):
                df = cleaned_data[name]
                if col not in df.columns:
                    continue
                parsed = pd.to_datetime(df[col], errors="coerce")
                unparsed = int((parsed.isna() & df[col].notna()).sum())
                assert unparsed == 0, f"{name}.{col} 仍有 {unparsed} 个无法解析的日期"

    def test_units_normalized_within_physical_range(self, cleaned_data):
        hours = pd.to_numeric(cleaned_data["volunteer_shifts"]["hours"], errors="coerce")
        assert hours.between(0.25, 12.0).all()
        qty = pd.to_numeric(cleaned_data["in_kind_donations"]["quantity"], errors="coerce")
        assert qty.between(1, 5000).all()

    def test_orphan_foreign_keys_resolved(self, cleaned_data):
        """悬空外键必须被归入 UNKNOWN 成员，不允许残留未处理的非法键。"""
        vol_ids = set(cleaned_data["volunteers"]["volunteer_id"])
        sh = cleaned_data["volunteer_shifts"]
        bad = ~sh["volunteer_id"].isin(vol_ids | {UNKNOWN})
        assert int(bad.sum()) == 0, "签到记录仍存在未被归入 UNKNOWN 的悬空志愿者外键"

        proj_ids = set(cleaned_data["projects"]["project_id"])
        assert cleaned_data["donations"]["project_id"].isin(proj_ids | {UNKNOWN, ""}).all()

    def test_extreme_values_handled(self, cleaned_data):
        amounts = pd.to_numeric(cleaned_data["donations"]["amount_cny"], errors="coerce")
        assert amounts.notna().all()
        assert (amounts >= 0).all()
        assert (amounts <= 1_000_000).all()
        ages = pd.to_numeric(cleaned_data["beneficiaries"]["age"], errors="coerce")
        assert ages.between(0, 120).all()

    def test_missing_dimension_values_filled(self, cleaned_data):
        for name, cols in (
            ("volunteer_shifts", ["county", "service_role", "checkin_method"]),
            ("volunteers", ["county", "township", "occupation", "education"]),
            ("beneficiaries", ["township", "village", "household_type"]),
            ("donations", ["channel", "designation"]),
        ):
            df = cleaned_data[name]
            for col in cols:
                assert not df[col].isna().any(), f"{name}.{col} 仍有缺失值"

    def test_overall_repair_rate(self, cleaned_data, ground_truth, cfg):
        """综合修复率：能通过业务约束检验的缺陷类型占比。"""
        checks: dict[str, bool] = {}

        hours = pd.to_numeric(cleaned_data["volunteer_shifts"]["hours"], errors="coerce")
        checks["unit_inconsistency"] = bool(hours.between(0.25, 12.0).all())
        checks["duplicate_row"] = bool(
            not cleaned_data["volunteer_shifts"]["shift_id"].duplicated().any()
        )
        checks["orphan_foreign_key"] = bool(
            cleaned_data["volunteer_shifts"]["volunteer_id"]
            .isin(set(cleaned_data["volunteers"]["volunteer_id"]) | {UNKNOWN})
            .all()
        )
        checks["extreme_outlier"] = bool(
            pd.to_numeric(cleaned_data["donations"]["amount_cny"], errors="coerce").between(0, 1e6).all()
        )
        checks["malformed_date"] = bool(
            cleaned_data["in_kind_donations"]["received_date"].notna().all()
        )
        checks["missing_value"] = bool(
            not cleaned_data["volunteer_shifts"]["county"].isna().any()
        )
        checks["whitespace_or_widechar"] = bool(
            (cleaned_data["volunteer_shifts"]["county"].astype(str)
             == cleaned_data["volunteer_shifts"]["county"].astype(str).str.strip()).all()
        )

        injected_types = set(ground_truth["defect_type"].unique())
        relevant = {k: v for k, v in checks.items() if k in injected_types}
        assert relevant, "没有可检验的缺陷类型"
        rate = sum(relevant.values()) / len(relevant)
        assert rate >= 0.85, f"综合修复率过低：{rate:.1%}，明细 {relevant}"


# ---------------------------------------------------------------------------
# 规则引擎
# ---------------------------------------------------------------------------


class TestValidationEngine:
    def test_rules_run_and_report(self, cfg, cleaned_data):
        from qingfeng.validate import run_validation

        report = run_validation(cfg, cleaned_data)
        assert len(report.results) >= 40
        frame = report.to_frame()
        assert set(frame.columns) >= {"table", "rule", "severity", "status", "pass_rate"}
        # 清洗之后不应再有 ERROR 级失败
        assert report.ok, f"仍存在 ERROR 级失败：{[r.rule for r in report.failed_errors]}"

    def test_quality_scores_within_range(self, cfg, cleaned_data):
        from qingfeng.validate import run_validation

        scores = run_validation(cfg, cleaned_data).quality_scores()
        assert len(scores) == len(cleaned_data)
        assert scores["weighted_pass_rate"].between(0, 1).all()

    def test_rules_catch_deliberately_broken_data(self, cfg, cleaned_data):
        """把干净数据故意改坏，规则引擎必须报错（防止规则形同虚设）。"""
        from qingfeng.validate import run_validation

        broken = {k: v.copy() for k, v in cleaned_data.items()}
        broken["volunteer_shifts"].loc[broken["volunteer_shifts"].index[:5], "hours"] = 999.0
        broken["projects"].loc[broken["projects"].index[0], "planned_budget_cny"] = -1.0
        report = run_validation(cfg, broken)
        assert not report.ok
        failed_rules = {r.rule for r in report.failed_errors}
        assert any("hours" in r for r in failed_rules)
        assert any("planned_budget_cny" in r for r in failed_rules)
