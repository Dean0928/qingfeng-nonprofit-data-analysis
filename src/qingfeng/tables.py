"""数据表规格登记表（Single Source of Truth）。

清洗、校验、建模、建库、报告等模块都从这里读取表的**主键、业务键、受控词表、
日期列、度量列**，避免同一份口径散落在多个文件里——这是保证管道可维护性的关键。
"""

from __future__ import annotations

from typing import Any, Final

# 受控词表（与 scripts/generate_synthetic_data.py 中的取值域保持一致）
CATEGORIES: Final = (
    "助学支教", "助老敬老", "助残扶弱", "生态环保", "文化传承", "乡村振兴", "应急救灾",
)
COUNTIES: Final = ("香格里拉市", "维西傈僳族自治县", "德钦县")
ETHNICITIES: Final = ("藏族", "傈僳族", "纳西族", "彝族", "白族", "普米族", "苗族", "回族", "汉族")
EDUCATION: Final = ("初中及以下", "高中/中专", "大专", "本科", "硕士及以上")
HOUSEHOLD_TYPES: Final = (
    "低保户", "脱贫户", "防返贫监测户", "留守儿童家庭", "残障家庭", "事实无人抚养儿童", "一般农户",
)
SERVICE_ROLES: Final = (
    "教学辅导", "陪伴关怀", "双语翻译", "物资发放", "环境清理", "活动组织", "专业支持", "入户走访",
)
CHECKIN_METHODS: Final = ("小程序签到", "纸质签到表", "社工代录")
DONOR_TYPES: Final = ("个人", "企业", "基金会", "政府", "社会组织")
DONATION_CHANNELS: Final = ("微信支付", "支付宝", "银行转账", "现场捐赠", "公益平台", "企业直汇")
BENEFICIARY_SERVICE_TYPES: Final = (
    "课业辅导", "生活照料", "心理陪伴", "物资发放", "健康检查", "技能培训", "文化活动", "政策宣讲",
)
BUDGET_CATEGORIES: Final = ("人力成本", "物资采购", "差旅交通", "场地租赁", "培训费", "宣传传播", "其他")
PROJECT_STATUS: Final = ("筹备中", "进行中", "已完成", "已结项", "暂停")
VOLUNTEER_STATUS: Final = ("活跃", "沉睡", "退出", "新增")

TABLE_SPECS: Final[dict[str, dict[str, Any]]] = {
    "projects": {
        "zh_name": "公益项目台账",
        "source": "项目组 Excel 台账（人工维护）",
        "primary_key": "project_id",
        "business_keys": ["project_id"],
        "date_columns": ["planned_start_date", "planned_end_date", "actual_end_date"],
        "measure_columns": ["planned_budget_cny", "target_beneficiaries"],
        "controlled": {
            "category": CATEGORIES,
            "county": COUNTIES,
            "status": PROJECT_STATUS,
        },
        "grain": "一行 = 一个在册公益项目",
    },
    "staff": {
        "zh_name": "机构工作人员",
        "source": "机构人事花名册",
        "primary_key": "staff_id",
        "business_keys": ["staff_id"],
        "date_columns": ["hire_date"],
        "measure_columns": [],
        "controlled": {},
        "grain": "一行 = 一名专职/兼职工作人员",
    },
    "volunteers": {
        "zh_name": "志愿者花名册",
        "source": "志愿服务信息系统导出",
        "primary_key": "volunteer_id",
        "business_keys": ["volunteer_id"],
        "date_columns": ["join_date"],
        "measure_columns": ["birth_year"],
        "controlled": {
            "county": COUNTIES,
            "ethnicity": ETHNICITIES,
            "education": EDUCATION,
            "status": VOLUNTEER_STATUS,
        },
        "grain": "一行 = 一名注册志愿者",
    },
    "volunteer_shifts": {
        "zh_name": "志愿服务签到记录",
        "source": "签到小程序导出 + 纸质签到表补录",
        "primary_key": "shift_id",
        "business_keys": ["volunteer_id", "project_id", "service_date", "service_role"],
        "date_columns": ["service_date"],
        "measure_columns": ["hours"],
        "controlled": {
            "county": COUNTIES,
            "service_role": SERVICE_ROLES,
            "checkin_method": CHECKIN_METHODS,
        },
        "grain": "一行 = 一名志愿者在一个项目上的一次服务",
    },
    "donors": {
        "zh_name": "捐赠人档案",
        "source": "捐赠平台 + 财务台账",
        "primary_key": "donor_id",
        "business_keys": ["donor_id"],
        "date_columns": ["first_donation_date"],
        "measure_columns": [],
        "controlled": {"donor_type": DONOR_TYPES, "county": (*COUNTIES, "州外")},
        "grain": "一行 = 一个捐赠人（个人或机构）",
    },
    "donations": {
        "zh_name": "现金捐赠流水",
        "source": "捐赠平台导出 + 银行流水",
        "primary_key": "donation_id",
        "business_keys": ["donation_id"],
        "date_columns": ["donation_date"],
        "measure_columns": ["amount_cny"],
        "controlled": {"channel": DONATION_CHANNELS, "designation": ("定向", "非定向")},
        "grain": "一行 = 一笔现金捐赠",
    },
    "in_kind_donations": {
        "zh_name": "物资捐赠入库单",
        "source": "仓库入库单（纸质转录）",
        "primary_key": "inkind_id",
        "business_keys": ["inkind_id"],
        "date_columns": ["received_date"],
        "measure_columns": ["quantity", "est_unit_value_cny", "est_total_value_cny"],
        "controlled": {},
        "grain": "一行 = 一笔物资捐赠入库",
    },
    "beneficiaries": {
        "zh_name": "服务对象档案",
        "source": "社工入户走访登记表",
        "primary_key": "beneficiary_id",
        "business_keys": ["beneficiary_id"],
        "date_columns": ["enroll_date"],
        "measure_columns": ["age", "risk_score"],
        "controlled": {"county": COUNTIES, "household_type": HOUSEHOLD_TYPES, "ethnicity": ETHNICITIES},
        "grain": "一行 = 一名建档服务对象",
    },
    "beneficiary_services": {
        "zh_name": "服务提供记录",
        "source": "服务工单系统 + 社工台账",
        "primary_key": "service_id",
        "business_keys": ["beneficiary_id", "project_id", "service_date", "service_type"],
        "date_columns": ["service_date"],
        "measure_columns": ["duration_hours", "satisfaction_score"],
        "controlled": {"county": COUNTIES, "service_type": BENEFICIARY_SERVICE_TYPES},
        "grain": "一行 = 对一名服务对象的一次服务",
    },
    "monthly_budget_execution": {
        "zh_name": "项目月度预算执行",
        "source": "财务月度报表",
        "primary_key": None,
        "business_keys": ["month", "project_id", "budget_category"],
        "date_columns": [],
        "measure_columns": ["planned_cny", "actual_cny"],
        "controlled": {"budget_category": BUDGET_CATEGORIES},
        "grain": "一行 = 一个项目在一个月份上一个预算科目的计划/实际支出",
    },
    "satisfaction_survey_raw": {
        "zh_name": "满意度问卷（原始回收）",
        "source": "问卷星导出",
        "primary_key": "response_id",
        "business_keys": ["response_id"],
        "date_columns": ["submit_time"],
        "measure_columns": ["Q1_总体满意度_score", "Q2_志愿者服务态度_score"],
        "controlled": {},
        "grain": "一行 = 一份问卷回复（含大量未结构化的自由文本）",
    },
}

# 生成器产出的全部表（顺序即依赖顺序：维度在前，事实在后）
ALL_TABLES: Final[tuple[str, ...]] = tuple(TABLE_SPECS)

# 维度表 / 事实表划分
DIMENSION_TABLES: Final[tuple[str, ...]] = (
    "projects", "staff", "volunteers", "donors", "beneficiaries",
)
FACT_TABLES: Final[tuple[str, ...]] = (
    "volunteer_shifts", "donations", "in_kind_donations",
    "beneficiary_services", "monthly_budget_execution",
)


def cleaning_tables() -> tuple[str, ...]:
    """参与清洗的全部表名。"""
    return ALL_TABLES


def primary_key(table: str) -> str | None:
    return TABLE_SPECS[table].get("primary_key")


def business_keys(table: str) -> list[str]:
    return list(TABLE_SPECS[table].get("business_keys", []))


def measure_columns(table: str) -> list[str]:
    return list(TABLE_SPECS[table].get("measure_columns", []))


def date_columns(table: str) -> list[str]:
    return list(TABLE_SPECS[table].get("date_columns", []))


def grain(table: str) -> str:
    return str(TABLE_SPECS[table].get("grain", ""))


def controlled_values(table: str, column: str) -> tuple[str, ...]:
    return tuple(TABLE_SPECS.get(table, {}).get("controlled", {}).get(column, ()))
