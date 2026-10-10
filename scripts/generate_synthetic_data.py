#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多源异构业务数据生成器 —— 迪庆清风青年公益联合会 公益项目数据运营分析。

为什么需要这个脚本
------------------
公益机构的业务数据属于内部资料且含大量个人敏感信息，无法公开。为了让本项目的
分析管道**完全可复现、可审计**，我们用一个带固定随机种子的生成器，按真实公益机构
的业务形态产出多源数据：

    项目组 Excel 台账 / 志愿服务系统导出 / 财务月度执行表 / 捐赠平台流水 /
    物资入库单 / 入户走访档案 / 问卷星回收表

数据分布由 ``data/external/calibration_reference.md`` 中的公开统计口径校准
（迪庆州人口与民族结构、《慈善蓝皮书(2025)》全国志愿服务与捐赠基准）。

三个关键设计
------------
1. **业务一致性**：签到记录只出现在"项目在期 ∩ 志愿者已注册 ∩ ≤ 截止日"的日期上；
   物资捐赠金额与数量、财务执行率、满意度分布相互约束。
2. **Ground Truth 必须与数据一致**：``PLANTED_ANOMALIES`` 中声明的每一个异常都必须在
   生成阶段真实落地；若某条异常只是"写在清单里"而没有进入数据，
   下游的检出率评估就会被系统性低估（这是本项目实际踩过并修复的坑）。
3. **刻意注入的数据缺陷**：按 ``config.data_quality`` 注入重复、缺失、日期格式混乱、
   单位不统一、极端值、悬空外键、全半角空格等七类问题，用于检验下游清洗模块。
3. **Ground Truth 落盘**：每一个注入的缺陷、每一个刻意植入的业务异常都被写入
   ``data/raw/_ground_truth/``，使清洗与异常检测模块可以做**定量的精确率/召回率评估**，
   而不是只靠肉眼观察。

用法::

    python scripts/generate_synthetic_data.py            # 使用 config/config.yaml
    python scripts/generate_synthetic_data.py --seed 42  # 覆盖随机种子
    python scripts/generate_synthetic_data.py --out data/raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# 允许直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qingfeng.config import Config, load_config  # noqa: E402
from qingfeng.logging_utils import RunTimer, get_logger  # noqa: E402

logger = get_logger("generate")

CSV_KWARGS: dict[str, Any] = {"index": False, "encoding": "utf-8-sig"}

# =============================================================================
# 一、校准后的业务分布常量（来源见 data/external/calibration_reference.md）
# =============================================================================

COUNTIES: list[str] = ["香格里拉市", "维西傈僳族自治县", "德钦县"]
# 州府经济活跃度最高，项目与服务量按此权重分布
COUNTY_WEIGHTS: list[float] = [0.52, 0.27, 0.21]

TOWNSHIPS: dict[str, list[str]] = {
    "香格里拉市": ["建塘镇", "小中甸镇", "三坝乡", "格咱乡", "尼西乡", "东旺乡", "五境乡", "上江乡", "金江镇", "虎跳峡镇", "洛吉乡"],
    "维西傈僳族自治县": ["保和镇", "叶枝镇", "塔城镇", "永春乡", "攀天阁乡", "白济汛乡", "康普乡", "中路乡", "维登乡", "巴迪乡"],
    "德钦县": ["升平镇", "奔子栏镇", "燕门乡", "云岭乡", "佛山乡", "拖顶傈僳族乡", "霞若傈僳族乡", "羊拉乡"],
}

# 民族结构：藏族 36.3% / 傈僳族 30.2% / 纳西族 12.5% / 其他少数民族 / 汉族
ETHNIC_WEIGHTS: dict[str, float] = {
    "藏族": 0.363,
    "傈僳族": 0.302,
    "纳西族": 0.125,
    "彝族": 0.047,
    "白族": 0.041,
    "普米族": 0.007,
    "苗族": 0.004,
    "回族": 0.003,
    "汉族": 0.108,
}

# 各类项目的业务形态：代码、名称词干、季节性、单次服务时长倍数、目标受益人量级
PROJECT_CATEGORIES: dict[str, dict[str, Any]] = {
    "助学支教": {
        "code": "EDU",
        "themes": ["格桑花助学计划", "高原雏鹰课堂", "雪域书香伴读", "青苗暑期支教营", "一帮一助学行动"],
        "seasonality": [0.9, 0.7, 1.4, 1.1, 1.0, 1.1, 1.8, 1.9, 1.5, 1.0, 0.8, 0.7],
        "hours_multiplier": 1.25,
        "beneficiary_scale": (60, 320),
    },
    "助老敬老": {
        "code": "ELD",
        "themes": ["暖冬助老行动", "银龄陪伴计划", "夕阳红健康巡诊", "敬老院暖心厨房", "独居老人关爱探访"],
        "seasonality": [1.4, 1.3, 1.0, 0.9, 0.9, 0.9, 0.8, 0.8, 1.0, 1.5, 1.2, 1.4],
        "hours_multiplier": 0.95,
        "beneficiary_scale": (40, 200),
    },
    "助残扶弱": {
        "code": "DIS",
        "themes": ["阳光助残服务", "无障碍出行倡导", "残疾人技能培训", "康复辅具适配", "特殊儿童融合课堂"],
        "seasonality": [1.1, 1.0, 1.0, 1.0, 1.1, 1.0, 0.9, 0.9, 1.0, 1.1, 1.1, 1.0],
        "hours_multiplier": 1.05,
        "beneficiary_scale": (25, 120),
    },
    "生态环保": {
        "code": "ENV",
        "themes": ["梅里雪山生态守护", "金沙江净滩行动", "高原湖泊水质监测", "草原垃圾清零", "滇金丝猴栖息地巡护"],
        "seasonality": [0.6, 0.7, 1.0, 1.5, 1.7, 1.6, 1.3, 1.1, 1.4, 1.2, 0.8, 0.6],
        "hours_multiplier": 1.15,
        "beneficiary_scale": (0, 40),
    },
    "文化传承": {
        "code": "CUL",
        "themes": ["藏文书法传承班", "傈僳族阿尺木刮传习", "纳西东巴文化讲堂", "弦子舞进校园", "非遗手工艺助农"],
        "seasonality": [1.3, 1.2, 0.9, 0.9, 1.0, 1.1, 1.3, 1.4, 1.0, 1.0, 1.0, 1.2],
        "hours_multiplier": 1.0,
        "beneficiary_scale": (30, 160),
    },
    "乡村振兴": {
        "code": "RUR",
        "themes": ["高原藜麦助销计划", "乡村电商人才培育", "松茸采摘技能培训", "农村饮水安全改善", "村寨微景观改造"],
        "seasonality": [0.7, 0.8, 1.2, 1.3, 1.3, 1.2, 1.2, 1.4, 1.5, 1.3, 0.9, 0.7],
        "hours_multiplier": 1.1,
        "beneficiary_scale": (50, 260),
    },
    "应急救灾": {
        "code": "EMG",
        "themes": ["雪灾应急物资投送", "地震应急演练", "汛期防灾宣传", "灾后心理援助", "山区道路抢险支援"],
        "seasonality": [1.3, 1.4, 0.8, 0.7, 0.8, 1.0, 1.2, 1.3, 1.0, 0.8, 0.9, 1.2],
        "hours_multiplier": 1.45,
        "beneficiary_scale": (20, 300),
    },
}

FUNDING_SOURCES = ["政府购买服务", "基金会资助", "企业定向捐赠", "社会众筹", "机构自有资金"]
FUNDING_WEIGHTS = [0.22, 0.24, 0.26, 0.18, 0.10]

STAFF_ROLES = ["总干事", "项目专员", "项目专员", "财务专员", "传播专员", "社工", "社工", "实习生"]
OCCUPATIONS = [
    "在校大学生", "中小学教师", "机关事业单位职工", "企业职员", "个体经营者",
    "医护人员", "自由职业", "退休人员", "农牧民", "待业青年",
]
OCCUPATION_WEIGHTS = [0.28, 0.14, 0.12, 0.10, 0.08, 0.06, 0.07, 0.04, 0.06, 0.05]
# 职业 -> 合理年龄区间（建模一致性约束：避免出现"17 岁中小学教师"这类矛盾记录）
OCCUPATION_AGE_RANGE: dict[str, tuple[int, int]] = {
    "在校大学生": (18, 26),
    "中小学教师": (23, 58),
    "机关事业单位职工": (23, 58),
    "企业职员": (22, 58),
    "个体经营者": (22, 60),
    "医护人员": (23, 58),
    "自由职业": (19, 60),
    "退休人员": (55, 72),
    "农牧民": (20, 65),
    "待业青年": (18, 35),
}
EDUCATION_LEVELS = ["初中及以下", "高中/中专", "大专", "本科", "硕士及以上"]
EDUCATION_WEIGHTS = [0.08, 0.15, 0.26, 0.44, 0.07]
SKILLS_POOL = [
    "藏汉双语", "傈僳语翻译", "急救证", "心理疏导", "摄影摄像", "新媒体运营",
    "课业辅导", "医疗护理", "驾驶", "厨艺", "手工艺", "数据分析", "活动策划", "户外向导",
]
SERVICE_ROLES = ["教学辅导", "陪伴关怀", "双语翻译", "物资发放", "环境清理", "活动组织", "专业支持", "入户走访"]

# 渠道选择与捐赠人类型强相关：机构走银行/直汇，个人走移动支付与公益平台。
# 这条关联让"捐赠渠道 × 捐赠人类型"交叉分析具备真实业务含义。
CHANNEL_BY_DONOR_TYPE: dict[str, tuple[list[str], list[float]]] = {
    "个人": (["微信支付", "支付宝", "公益平台", "现场捐赠"], [0.46, 0.19, 0.26, 0.09]),
    "企业": (["银行转账", "企业直汇", "现场捐赠"], [0.55, 0.35, 0.10]),
    "基金会": (["银行转账", "公益平台"], [0.88, 0.12]),
    "政府": (["银行转账"], [1.0]),
    "社会组织": (["银行转账", "公益平台", "现场捐赠"], [0.50, 0.30, 0.20]),
}

# 服务类型由项目类别决定：助学项目不可能主要提供"生活照料"。
SERVICE_TYPE_BY_CATEGORY: dict[str, tuple[list[str], list[float]]] = {
    "助学支教": (["课业辅导", "心理陪伴", "物资发放", "文化活动"], [0.50, 0.20, 0.20, 0.10]),
    "助老敬老": (["生活照料", "健康检查", "心理陪伴", "物资发放"], [0.38, 0.25, 0.20, 0.17]),
    "助残扶弱": (["技能培训", "生活照料", "心理陪伴", "政策宣讲"], [0.35, 0.30, 0.20, 0.15]),
    "生态环保": (["政策宣讲", "文化活动"], [0.60, 0.40]),
    "文化传承": (["文化活动", "技能培训"], [0.70, 0.30]),
    "乡村振兴": (["技能培训", "政策宣讲", "物资发放"], [0.45, 0.30, 0.25]),
    "应急救灾": (["物资发放", "生活照料", "政策宣讲"], [0.50, 0.30, 0.20]),
}

# 家庭脆弱程度越高，满意度越低（服务质量公平性分析的核心假设）
SATISFACTION_BY_HOUSEHOLD: dict[str, float] = {
    "事实无人抚养儿童": 4.05,
    "残障家庭": 4.14,
    "低保户": 4.30,
    "防返贫监测户": 4.33,
    "留守儿童家庭": 4.38,
    "脱贫户": 4.46,
    "一般农户": 4.54,
}
CHECKIN_METHODS = ["小程序签到", "小程序签到", "小程序签到", "纸质签到表", "社工代录"]
HOUSEHOLD_TYPES = ["低保户", "脱贫户", "防返贫监测户", "留守儿童家庭", "残障家庭", "事实无人抚养儿童", "一般农户"]
HOUSEHOLD_WEIGHTS = [0.20, 0.24, 0.13, 0.16, 0.10, 0.05, 0.12]
BENEFICIARY_SERVICE_TYPES = ["课业辅导", "生活照料", "心理陪伴", "物资发放", "健康检查", "技能培训", "文化活动", "政策宣讲"]
BUDGET_CATEGORIES = ["人力成本", "物资采购", "差旅交通", "场地租赁", "培训费", "宣传传播", "其他"]
BUDGET_CATEGORY_WEIGHTS = [0.30, 0.26, 0.16, 0.08, 0.09, 0.07, 0.04]
DONATION_CHANNELS = ["微信支付", "支付宝", "银行转账", "现场捐赠", "公益平台", "企业直汇"]
DONATION_CHANNEL_WEIGHTS = [0.34, 0.14, 0.16, 0.11, 0.19, 0.06]
IN_KIND_ITEMS: dict[str, tuple[str, str, float]] = {
    # 物品 -> (类别, 单位, 参考单价 元)
    "棉被": ("御寒物资", "床", 220.0),
    "保暖衣物": ("御寒物资", "件", 130.0),
    "大米": ("食品", "袋", 95.0),
    "食用油": ("食品", "桶", 78.0),
    "学习用品套装": ("教育物资", "套", 65.0),
    "图书": ("教育物资", "册", 28.0),
    "体育器材": ("教育物资", "件", 180.0),
    "常用药品": ("医疗物资", "箱", 460.0),
    "消毒用品": ("医疗物资", "箱", 210.0),
    "取暖炉": ("御寒物资", "台", 380.0),
}

# 民族 -> 姓名用字池（生成虚构姓名，避免与真实个人重名）
NAME_POOLS: dict[str, tuple[list[str], list[str]]] = {
    "藏族": (
        ["扎西", "次仁", "洛桑", "格桑", "尼玛", "巴桑", "达瓦", "阿旺", "普布", "桑杰", "白玛", "向巴", "泽仁", "斯那", "丁增", "鲁茸"],
        ["卓玛", "央金", "拉姆", "德吉", "曲珍", "玉珍", "梅朵", "拉珍", "宗吉", "拥青", "白玛", "普珍", "格桑", "索朗"],
    ),
    "傈僳族": (
        ["阿普", "蜂建", "木生", "余志", "和顺", "雀文", "普学", "阿此", "汉明", "余建"],
        ["蜂花", "木英", "余兰", "和秀", "阿娜", "雀梅", "普花", "余香", "木珍", "阿芝"],
    ),
    "纳西族": (
        ["和文", "木正", "和建", "杨世", "木向东", "和志", "李永", "王学"],
        ["和秀", "木春", "杨丽", "和梅", "木英", "李佳", "王芳", "和香"],
    ),
    "彝族": (["吉克", "阿牛", "沙马", "阿西", "曲木"], ["阿依", "吉伍", "俄底", "沙英", "阿芝"]),
    "白族": (["杨绍", "赵文", "李德", "张建"], ["杨美", "赵花", "李秀", "张丽"]),
    "普米族": (["熊建", "胡文", "马学"], ["熊梅", "胡秀", "马兰"]),
    "苗族": (["吴建", "龙文"], ["吴花", "龙梅"]),
    "回族": (["马文", "马俊"], ["马兰", "马梅"]),
    "汉族": (
        ["张伟", "王强", "李军", "刘洋", "陈浩", "杨帆", "周涛", "赵鹏", "孙磊", "吴迪", "郑凯", "冯亮"],
        ["张敏", "王静", "李娜", "刘婷", "陈雪", "杨柳", "周悦", "赵琳", "孙倩", "吴瑕", "郑颖", "冯洁"],
    ),
}

SURVEY_SATISFACTION_TEXT = {
    5: ["非常满意", "很满意", "非常满意！", "5"],
    4: ["满意", "比较满意", "4"],
    3: ["一般", "还行", "3"],
    2: ["不太满意", "2"],
    1: ["不满意", "1"],
}

# =============================================================================
# 二、刻意植入的业务异常（不是脏数据，而是"真实发生但偏离常态"的事件）
#     下游 anomaly 模块应当检出它们，并区分「业务事件」与「数据录入错误」。
# =============================================================================

PLANTED_ANOMALIES: list[dict[str, Any]] = [
    {
        "anomaly_id": "ANOM-01",
        "metric": "volunteer_service_hours",
        "scope": "香格里拉市",
        "period": "2026-04",
        "direction": "spike",
        "magnitude": "+186%",
        "cause_type": "data_error",
        "cause": "签到小程序改版后，将「分钟」误按「小时」写入，个别记录出现 240 小时/次的极端值。",
        "expected_handling": "在清洗阶段按物理上限（单次 ≤ 12h）截断并回填，同时记录修正确认单。",
    },
    {
        "anomaly_id": "ANOM-02",
        "metric": "volunteer_service_hours",
        "scope": "德钦县",
        "period": "2026-05",
        "direction": "drop",
        "magnitude": "-63%",
        "cause_type": "business_event",
        "cause": "G214 白马雪山段连续强降雪导致封路，德钦县 5 月两批入户走访与巡护活动整体取消/延期。",
        "expected_handling": "属于不可抗力的真实业务波动，不应修改数据；应在报告中说明并对年度目标做情景调整。",
    },
    {
        "anomaly_id": "ANOM-03",
        "metric": "donation_amount",
        "scope": "全州",
        "period": "2026-05",
        "direction": "spike",
        "magnitude": "+412%",
        "cause_type": "business_event",
        "cause": "某省外企业通过基金会渠道定向捐赠 85 万元，用于德钦县雪灾应急物资采购，为一次性大额捐赠。",
        "expected_handling": "真实事件；需单独披露并计算剔除该笔后的可比口径，避免抬高基线预期。",
    },
    {
        "anomaly_id": "ANOM-04",
        "metric": "beneficiary_service_count",
        "scope": "维西傈僳族自治县",
        "period": "2026-06",
        "direction": "spike",
        "magnitude": "+204%",
        "cause_type": "data_error",
        "cause": "维西片区社工在纸质台账补录时整表批量重复导入，导致 6 月服务记录翻倍。",
        "expected_handling": "在清洗阶段按业务主键去重；这是重复率远高于基线的典型场景。",
    },
    {
        "anomaly_id": "ANOM-05",
        "metric": "active_volunteer_rate",
        "scope": "全州",
        "period": "2025-12",
        "direction": "drop",
        "magnitude": "-41%",
        "cause_type": "business_event",
        "cause": "冬季严寒叠加虫草/松茸季结束后的外出务工潮，志愿者返乡与流动导致活跃度骤降。",
        "expected_handling": "真实季节性；趋势分析应使用 STL 分解剥离季节成分后再判断。",
    },
    {
        "anomaly_id": "ANOM-06",
        "metric": "project_execution_rate",
        "scope": "维西傈僳族自治县",
        "period": "2026-03",
        "direction": "drop",
        "magnitude": "-34%",
        "cause_type": "data_error",
        "cause": "「乡村电商人才培育」项目 3 月报销单据跨月入账，导致当月实际支出被显著低估。",
        "expected_handling": "跨期入账属财务口径问题，应在归因中标注为口径异常而非执行滞后。",
    },
]


# =============================================================================
# 三、工具函数
# =============================================================================


@dataclass
class Generator:
    """封装随机数状态与配置，保证同一 seed 下结果逐字节可复现。"""

    cfg: Config
    seed: int
    rng: np.random.Generator
    py_rng: Any  # random.Random，用于需要 python 原生语义的场景

    @classmethod
    def create(cls, cfg: Config, seed: int | None = None) -> Generator:
        import random as _random

        seed = int(seed if seed is not None else cfg.simulation.seed)
        return cls(
            cfg=cfg,
            seed=seed,
            rng=np.random.default_rng(seed),
            py_rng=_random.Random(seed),
        )

    # ---- 基础采样 ----
    @property
    def start(self) -> date:
        return date.fromisoformat(self.cfg.simulation.start_date)

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.cfg.simulation.as_of_date)

    def months(self) -> list[pd.Timestamp]:
        return list(pd.date_range(self.start, self.as_of, freq="MS"))

    def choice(self, seq, size=None, p=None):
        return self.rng.choice(np.array(seq, dtype=object), size=size, p=p)

    def lognormal(self, mean: float, sigma: float, size=None) -> np.ndarray:
        return self.rng.lognormal(mean=mean, sigma=sigma, size=size)

    def dates_between(self, lo: date, hi: date, size: int) -> list[date]:
        if hi <= lo:
            return [lo] * size
        span = (hi - lo).days
        offsets = self.rng.integers(0, span + 1, size=size)
        return [lo + timedelta(days=int(o)) for o in offsets]

    def weighted_date(self, lo: date, hi: date, size: int, recency: float = 1.6) -> list[date]:
        """在 [lo, hi] 上按偏向近期的幂律分布采样日期。"""
        if hi <= lo:
            return [lo] * size
        u = self.rng.random(size) ** (1.0 / recency)
        span = (hi - lo).days
        return [lo + timedelta(days=int(round(float(x) * span))) for x in u]


def pick_service_role(g: Generator, ethnicity: str, occupation: str, category: str) -> str:
    """按志愿者背景与项目类别分配服务角色。

    这里刻意建立起现实中的关联，使交叉分析能检验出**统计显著**的结构差异：
    双语能力的少数民族志愿者更多承担翻译与入户走访，在校大学生更多承担教学辅导，
    医护人员更多承担专业支持。
    """
    if ethnicity in {"藏族", "傈僳族"} and g.rng.random() < 0.40:
        return str(g.choice(["双语翻译", "入户走访"], p=[0.35, 0.65]))
    if occupation == "在校大学生" and category in {"助学支教", "文化传承"} and g.rng.random() < 0.55:
        return "教学辅导"
    if occupation == "医护人员" and g.rng.random() < 0.50:
        return "专业支持"
    if occupation == "农牧民" and category in {"生态环保", "乡村振兴"} and g.rng.random() < 0.45:
        return "环境清理"
    return str(g.choice(SERVICE_ROLES))


def make_person_name(g: Generator, ethnicity: str, gender: str) -> str:
    male, female = NAME_POOLS.get(ethnicity, NAME_POOLS["汉族"])
    pool = male if gender == "男" else female
    base = str(g.choice(pool))
    # 约 35% 的概率拼接一个后缀字，制造姓名多样性
    if g.rng.random() < 0.35:
        suffix = str(g.choice(["才让", "加措", "此里", "农布", "拉初", "小英", "文华", "晓东"]))
        if len(base) + len(suffix) <= 5:
            base = base + suffix
    return base


def mask_phone(g: Generator) -> str:
    prefix = str(g.choice(["138", "139", "152", "156", "177", "182", "187", "198", "133"]))
    return f"{prefix}****{int(g.rng.integers(1000, 9999))}"


def as_naive_date(ts: Any) -> date:
    return pd.Timestamp(ts).date()


# =============================================================================
# 四、各数据源的生成
# =============================================================================


def build_staff(g: Generator) -> pd.DataFrame:
    n = g.cfg.simulation.n_staff
    roles = [STAFF_ROLES[i % len(STAFF_ROLES)] for i in range(n)]
    g.py_rng.shuffle(roles)
    rows = []
    for i in range(n):
        role = roles[i]
        ethnicity = str(g.choice(list(ETHNIC_WEIGHTS), p=np.array(list(ETHNIC_WEIGHTS.values()))))
        gender = str(g.choice(["男", "女"], p=[0.42, 0.58]))
        hire = g.weighted_date(date(2018, 1, 1), g.as_of, 1, recency=0.8)[0]
        rows.append(
            {
                "staff_id": f"ST-{i + 1:03d}",
                "staff_name": make_person_name(g, ethnicity, gender),
                "role": role,
                "gender": gender,
                "ethnicity": ethnicity,
                "hire_date": hire.isoformat(),
                "office_county": str(g.choice(COUNTIES, p=[0.62, 0.2, 0.18])),
                "email": f"staff{i + 1:03d}@qingfeng-youth.org.cn",
                "is_full_time": role != "实习生",
            }
        )
    return pd.DataFrame(rows)


def build_projects(g: Generator, staff: pd.DataFrame) -> pd.DataFrame:
    n = g.cfg.simulation.n_projects
    categories = list(PROJECT_CATEGORIES)
    cat_weights = np.array([0.22, 0.16, 0.11, 0.14, 0.10, 0.19, 0.08])
    staff_ids = staff.loc[staff["role"].isin(["总干事", "项目专员", "社工"]), "staff_id"].tolist()

    rows = []
    for i in range(n):
        category = str(g.choice(categories, p=cat_weights))
        meta = PROJECT_CATEGORIES[category]
        county = str(g.choice(COUNTIES, p=COUNTY_WEIGHTS))
        township = str(g.choice(TOWNSHIPS[county]))

        start = g.weighted_date(g.start, date(g.as_of.year, max(1, g.as_of.month - 2), 1), 1, recency=0.9)[0]
        planned_months = int(g.rng.integers(4, 19))
        planned_end = start + timedelta(days=int(planned_months * 30.4))

        if planned_end <= g.as_of:
            status = "已结项" if g.rng.random() < 0.86 else "暂停"
            actual_end: date | None = planned_end + timedelta(days=int(g.rng.integers(-55, 32)))
            if actual_end > g.as_of:
                actual_end = None
                status = "进行中"
        else:
            status = str(g.choice(["进行中", "进行中", "筹备中"], p=[0.80, 0.12, 0.08]))
            actual_end = None

        lo, hi = meta["beneficiary_scale"]
        target = int(g.rng.integers(lo + 1, hi + 1)) if hi > lo else int(lo)
        # 预算与项目规模、类别相关：助学/乡村振兴单项目预算更高
        base_budget = {"EDU": 420_000, "RUR": 480_000, "ELD": 260_000, "DIS": 240_000,
                       "ENV": 190_000, "CUL": 200_000, "EMG": 300_000}[meta["code"]]
        planned_budget = round(base_budget * float(g.lognormal(0.0, 0.42, 1)[0]) / 1000) * 1000

        rows.append(
            {
                "project_id": f"PJ-{start.year}-{i + 1:03d}",
                "project_name": f"{county.replace('傈僳族自治县', '').replace('市', '')}·{str(g.choice(meta['themes']))}",
                "category": category,
                "category_code": meta["code"],
                "county": county,
                "township": township,
                "manager_staff_id": str(g.choice(staff_ids)),
                "planned_start_date": start.isoformat(),
                "planned_end_date": planned_end.isoformat(),
                "actual_end_date": actual_end.isoformat() if actual_end else "",
                "status": status,
                "planned_budget_cny": planned_budget,
                "funding_source": str(g.choice(FUNDING_SOURCES, p=FUNDING_WEIGHTS)),
                "target_beneficiaries": target,
                "risk_level": str(g.choice(["低", "中", "高"], p=[0.55, 0.33, 0.12])),
                "seasonality": json.dumps(meta["seasonality"]),
            }
        )
    df = pd.DataFrame(rows)
    # 项目名去重（重名时追加序号）
    dup = df["project_name"].duplicated(keep=False)
    if dup.any():
        suffix = df.groupby("project_name").cumcount() + 1
        df.loc[dup, "project_name"] = df.loc[dup, "project_name"] + "（" + suffix[dup].astype(str) + "期）"
    return df


def build_volunteers(g: Generator) -> pd.DataFrame:
    n = g.cfg.simulation.n_volunteers
    eth_names = list(ETHNIC_WEIGHTS)
    eth_p = np.array(list(ETHNIC_WEIGHTS.values()))
    educ_p = np.array(EDUCATION_WEIGHTS)

    ethnicities = g.rng.choice(eth_names, size=n, p=eth_p)
    genders = g.rng.choice(["男", "女"], size=n, p=[0.43, 0.57])
    counties = g.rng.choice(COUNTIES, size=n, p=COUNTY_WEIGHTS)
    educations = g.rng.choice(EDUCATION_LEVELS, size=n, p=educ_p)
    occupations = g.rng.choice(OCCUPATIONS, size=n, p=np.array(OCCUPATION_WEIGHTS))
    joins = g.weighted_date(date(2019, 1, 1), g.as_of, n, recency=1.5)

    # 年龄结构：以 18-40 岁为主，且必须与职业自洽（在校大学生不会 45 岁，退休人员不会 30 岁）
    ages = np.array(
        [
            int(
                np.clip(
                    g.rng.normal(
                        sum(OCCUPATION_AGE_RANGE[str(occ)]) / 2.0,
                        (OCCUPATION_AGE_RANGE[str(occ)][1] - OCCUPATION_AGE_RANGE[str(occ)][0]) / 4.0,
                    ),
                    *OCCUPATION_AGE_RANGE[str(occ)],
                ).round()
            )
            for occ in occupations
        ]
    )
    birth_years = [g.as_of.year - int(a) for a in ages]
    # 注册时须年满 16 周岁，且注册时间不得晚于截止日前 20 天
    joins = [
        min(
            max(joins[i], date(int(birth_years[i]) + 16, joins[i].month, min(joins[i].day, 28))),
            g.as_of - timedelta(days=20),
        )
        for i in range(n)
    ]

    skills = []
    for _ in range(n):
        k = int(g.rng.integers(0, 4))
        skills.append("|".join(sorted({str(s) for s in g.rng.choice(SKILLS_POOL, size=k, replace=False)})))

    # 注册状态：长期未参与者标记为沉睡
    last_active_gap = g.rng.exponential(scale=210, size=n).astype(int)
    statuses = []
    for i in range(n):
        if (g.as_of - joins[i]).days < 30:
            statuses.append("新增")
        elif last_active_gap[i] > 365:
            statuses.append("沉睡")
        elif g.rng.random() < 0.045:
            statuses.append("退出")
        else:
            statuses.append("活跃")
    statuses = np.array(statuses)

    rows = []
    for i in range(n):
        rows.append(
            {
                "volunteer_id": f"VN-{i + 1:05d}",
                "volunteer_name": make_person_name(g, str(ethnicities[i]), str(genders[i])),
                "gender": str(genders[i]),
                "birth_year": int(birth_years[i]),
                "ethnicity": str(ethnicities[i]),
                "county": str(counties[i]),
                "township": str(g.choice(TOWNSHIPS[str(counties[i])])),
                "occupation": str(occupations[i]),
                "education": str(educations[i]),
                "join_date": joins[i].isoformat(),
                "skills": skills[i],
                "phone_masked": mask_phone(g),
                "status": str(statuses[i]),
            }
        )
    return pd.DataFrame(rows)


def build_shifts(g: Generator, projects: pd.DataFrame, volunteers: pd.DataFrame) -> pd.DataFrame:
    """生成志愿服务签到记录（人次级明细）。

    流程：先按目标活跃率抽活跃志愿者，再为每人按项目在期与季节权重分配人次。
    """
    rate = float(g.rng.uniform(*g.cfg.simulation.active_volunteer_rate))
    vol = volunteers.copy()
    vol["join_d"] = pd.to_datetime(vol["join_date"]).dt.date

    # 项目在期区间
    proj = projects.copy()
    proj["s"] = pd.to_datetime(proj["planned_start_date"]).dt.date
    proj["e"] = pd.to_datetime(
        proj["actual_end_date"].where(proj["actual_end_date"] != "", proj["planned_end_date"])
    ).dt.date
    proj["e"] = proj["e"].clip(upper=g.as_of)
    proj = proj[proj["e"] > proj["s"]]

    eligible = vol[vol["join_d"] < g.as_of - timedelta(days=20)]
    n_active = max(1, int(len(eligible) * rate))
    active_idx = g.rng.choice(eligible.index.to_numpy(), size=n_active, replace=False)
    active = eligible.loc[active_idx]

    sp = g.cfg.simulation.shifts_per_active_volunteer
    counts = np.clip(
        g.rng.normal(sp["mean"], sp["sigma"], len(active)).round().astype(int),
        sp["min"],
        sp["max"],
    )

    hours_cfg = g.cfg.simulation.shift_hours_lognorm
    rows: list[dict[str, Any]] = []
    shift_no = 0

    for (vid, join_d, county, occupation, ethnicity), k in zip(
        zip(active["volunteer_id"], active["join_d"], active["county"],
            active["occupation"], active["ethnicity"]),
        counts,
        strict=True,
    ):
        # 优先参与本县项目（80%），其余跨县支援
        local = proj[(proj["county"] == county) & (proj["e"] > join_d)]
        pool = local if len(local) and g.rng.random() < 0.8 else proj[proj["e"] > join_d]
        if not len(pool):
            pool = proj[proj["e"] > join_d]
        if not len(pool):
            continue

        picks = pool.sample(n=int(k), replace=True, random_state=int(g.rng.integers(0, 2**31 - 1)))
        for _, p in picks.iterrows():
            lo = max(join_d, p["s"])
            hi = min(p["e"], g.as_of)
            if hi <= lo:
                continue
            season = json.loads(p["seasonality"])
            # 先在月份上按季节权重采样，再在月内均匀取日
            month_cursor = pd.date_range(lo, hi, freq="MS")
            if len(month_cursor) == 0:
                d = g.dates_between(lo, hi, 1)[0]
            else:
                w = np.array([season[ts.month - 1] for ts in month_cursor], dtype=float)
                w = w / w.sum()
                ts = month_cursor[int(g.rng.choice(len(month_cursor), p=w))]
                lo_m = max(lo, ts.date())
                hi_m = min(hi, (ts + pd.offsets.MonthEnd(0)).date())
                d = g.dates_between(lo_m, hi_m, 1)[0]

            role = pick_service_role(g, ethnicity, occupation, p["category"])
            mult = PROJECT_CATEGORIES[p["category"]]["hours_multiplier"]
            h = float(
                np.clip(
                    g.lognormal(hours_cfg["mean"], hours_cfg["sigma"], 1)[0] * mult,
                    hours_cfg["clip"][0],
                    hours_cfg["clip"][1],
                )
            )
            shift_no += 1
            rows.append(
                {
                    "shift_id": f"SH-{shift_no:07d}",
                    "volunteer_id": vid,
                    "project_id": p["project_id"],
                    "service_date": d.isoformat(),
                    "hours": round(h, 2),
                    "service_role": role,
                    "checkin_method": str(g.choice(CHECKIN_METHODS, p=[0.4, 0.25, 0.15, 0.12, 0.08])),
                    "county": p["county"],
                }
            )

    df = pd.DataFrame(rows).sort_values("service_date").reset_index(drop=True)
    return apply_planted_shift_anomalies(g, df)


def apply_planted_shift_anomalies(g: Generator, df: pd.DataFrame) -> pd.DataFrame:
    """把配置中声明的服务时长异常真正写进数据。"""
    df = df.copy()
    d = pd.to_datetime(df["service_date"])
    df["_ym"] = d.dt.to_period("M").astype(str)

    for a in PLANTED_ANOMALIES:
        metric = a["metric"]
        if metric not in {"volunteer_service_hours", "active_volunteer_rate"}:
            continue

        # 活跃志愿者率异常是全州层面的"活跃度骤降"：直接抽掉该月相当比例的签到记录，
        # 使当月去重后的志愿者人数显著低于趋势。必须在生成阶段真实落地，
        # 否则 ground truth 会声称存在一个数据里根本没有的异常。
        if metric == "active_volunteer_rate":
            idx = df.index[df["_ym"] == a["period"]]
            if not len(idx):
                continue
            drop_n = int(len(idx) * 0.45)
            drop = g.rng.choice(idx.to_numpy(), size=drop_n, replace=False)
            df = df.drop(index=drop)
            continue

        mask = (df["_ym"] == a["period"]) & (df["county"] == a["scope"])
        if not mask.any():
            continue
        idx = df.index[mask]

        if a["cause_type"] == "data_error" and a["direction"] == "spike":
            # ANOM-01：分钟误按小时，制造 60~240h 的极端值
            k = max(3, int(len(idx) * 0.06))
            target = g.rng.choice(idx.to_numpy(), size=min(k, len(idx)), replace=False)
            df.loc[target, "hours"] = g.rng.choice([120.0, 180.0, 240.0, 60.0], size=len(target))
        elif a["direction"] == "drop":
            # ANOM-02：真实业务事件 -> 直接抽掉大部分记录
            keep_ratio = 0.37
            drop_n = int(len(idx) * (1 - keep_ratio))
            drop = g.rng.choice(idx.to_numpy(), size=drop_n, replace=False)
            df = df.drop(index=drop)
        elif a["direction"] == "spike":
            # 一般性上冲：把现有记录复制一份并小幅调整
            dup = df.loc[idx].copy()
            dup["hours"] = (dup["hours"] * 1.25).round(2)
            df = pd.concat([df, dup], ignore_index=True)

    return df.drop(columns=["_ym"])


def build_donors(g: Generator) -> pd.DataFrame:
    n = g.cfg.simulation.n_donors
    types = g.rng.choice(
        ["个人", "企业", "基金会", "政府", "社会组织"],
        size=n,
        # 个人小额捐赠人数众多、机构捐赠人数量少但单笔金额大
        p=[0.9357, 0.0500, 0.0077, 0.0016, 0.0050],
    )
    rows = []
    for i in range(n):
        dt = str(types[i])
        if dt == "个人":
            ethnicity = str(g.choice(list(ETHNIC_WEIGHTS), p=np.array(list(ETHNIC_WEIGHTS.values()))))
            gender = str(g.choice(["男", "女"], p=[0.48, 0.52]))
            name = make_person_name(g, ethnicity, gender)
            county = str(g.choice(COUNTIES, p=COUNTY_WEIGHTS))
            recurring = bool(g.rng.random() < 0.18)  # 月捐人
        else:
            prefix = {
                "企业": ["香格里拉", "梅里", "金沙江", "高原", "云岭", "碧融", "雪域"],
                "基金会": ["明德", "同心", "山海", "益路", "青禾"],
                "政府": ["迪庆州", "云南省"],
                "社会组织": ["雪域之光", "格桑花", "高原之心"],
            }[dt]
            suffix = {
                "企业": ["文旅发展有限公司", "生物科技有限责任公司", "商贸有限公司", "建筑工程有限公司"],
                "基金会": ["公益基金会", "慈善基金会", "教育基金会"],
                "政府": ["民政局", "乡村振兴局", "教育体育局"],
                "社会组织": ["社会工作服务中心", "志愿者协会", "公益服务中心"],
            }[dt]
            name = str(g.choice(prefix)) + str(g.choice(suffix))
            county = str(g.choice(COUNTIES + ["州外"], p=[0.34, 0.16, 0.12, 0.38]))
            recurring = bool(g.rng.random() < 0.42)
        # 捐赠人获取节奏：全年大致匀速、略有增长（recency≈1 即接近均匀分布）。
        # 若过度偏向近期，会造成"捐赠额在后 20% 时间里指数级飙升"的失真曲线。
        # 捐赠人存量池：机构在数据窗口之前就已积累了一批捐赠人（2019 年起）。
        # 若只从窗口起点开始获取捐赠人，收入曲线会呈现失真的指数级增长；
        # 成熟组织应当是「存量池持续捐赠 + 新捐赠人稳步补充」的平稳形态。
        first = g.weighted_date(date(2019, 1, 1), g.as_of, 1, recency=0.95)[0]
        rows.append(
            {
                "donor_id": f"DN-{i + 1:05d}",
                "donor_name": name,
                "donor_type": dt,
                "county": county,
                "first_donation_date": first.isoformat(),
                "is_recurring": recurring,
                "contact_masked": mask_phone(g) if dt == "个人" else f"0{int(g.rng.integers(887, 889))}-{int(g.rng.integers(1000000, 9999999))}",
            }
        )
    return pd.DataFrame(rows)


def build_donations(g: Generator, donors: pd.DataFrame, projects: pd.DataFrame) -> pd.DataFrame:
    """捐赠流水：按捐赠人类型决定笔数与金额分布，并叠加全国捐赠收缩趋势。"""
    params = g.cfg.simulation.donation_lognorm
    donations_per_year = g.cfg.raw["simulation"].get(
        "donations_per_year",
        {"individual": 1.6, "corporate": 2.2, "foundation": 2.5,
         "government": 2.5, "social": 2.5},
    )
    proj = projects.copy()
    proj["s"] = pd.to_datetime(proj["planned_start_date"]).dt.date
    proj["e"] = pd.to_datetime(
        proj["actual_end_date"].where(proj["actual_end_date"] != "", proj["planned_end_date"])
    ).dt.date.clip(upper=g.as_of)

    # 月度宏观系数：2024 全国捐赠 -8.7%，2025 弱复苏，2026 上半年再度承压
    macro = {
        2024: [1.02, 0.94, 0.98, 1.00, 1.01, 0.97, 0.95, 1.03, 1.06, 1.00, 0.99, 1.05],
        2025: [0.96, 0.92, 1.02, 1.00, 1.03, 0.98, 0.97, 1.05, 1.08, 1.02, 1.01, 1.06],
        2026: [0.97, 0.90, 0.98, 0.95, 1.00, 0.96],
    }

    rows = []
    do_no = 0
    for _, d in donors.iterrows():
        dt = d["donor_type"]
        # 笔数 = 年化捐赠频次 x 有效在职时长。这样不同时期加入的捐赠人
        # 贡献相同的年化金额，不会让晚期加入者被系统性高估。
        first_d0 = pd.Timestamp(d["first_donation_date"]).date()
        eff_start = max(g.start, first_d0)
        tenure_years = max(0.25, (g.as_of - eff_start).days / 365.25)
        rate_key = {"个人": "individual", "企业": "corporate", "基金会": "foundation",
                    "政府": "government", "社会组织": "social"}[dt]
        annual_rate = float(donations_per_year[rate_key])
        if dt == "个人" and d["is_recurring"]:
            annual_rate *= 1.8
        n_don = int(np.clip(round(annual_rate * tenure_years), 1, 90))

        mu, sigma = params[{"个人": "individual", "企业": "corporate", "基金会": "foundation",
                            "政府": "government", "社会组织": "corporate"}[dt]].values()

        for _ in range(n_don):
            first_d = pd.Timestamp(d["first_donation_date"]).date()
            lo = min(max(g.start, first_d), g.as_of)
            day = g.weighted_date(lo, g.as_of, 1, recency=1.0)[0]
            # 约束：任何一笔捐赠都不可能早于该捐赠人的首次捐赠日期
            day = max(day, min(first_d, g.as_of))
            amount = float(g.lognormal(mu, sigma, 1)[0]) * macro.get(day.year, macro[2026])[day.month - 1]
            # 捐赠通常定向到某个在期项目；30% 为非定向
            designated = g.rng.random() > 0.30
            if designated:
                cand = proj[(proj["s"] <= day) & (proj["e"] >= day)]
                if not len(cand):
                    cand = proj
                pid = str(cand.sample(n=1, random_state=int(g.rng.integers(0, 2**31 - 1))).iloc[0]["project_id"])
            else:
                pid = ""
            do_no += 1
            channel_choices, channel_weights = CHANNEL_BY_DONOR_TYPE[dt]
            rows.append(
                {
                    "donation_id": f"DO-{do_no:06d}",
                    "donor_id": d["donor_id"],
                    "project_id": pid,
                    "donation_date": day.isoformat(),
                    "amount_cny": round(max(amount, 5.0), 2),
                    "channel": str(g.choice(channel_choices, p=channel_weights)),
                    "designation": "定向" if designated else "非定向",
                    "receipt_issued": bool(g.rng.random() < (0.72 if dt == "个人" else 0.97)),
                }
            )

    df = pd.DataFrame(rows).sort_values("donation_date").reset_index(drop=True)
    return apply_planted_donation_anomalies(g, df)


def apply_planted_donation_anomalies(g: Generator, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for a in PLANTED_ANOMALIES:
        if a["metric"] != "donation_amount":
            continue
        ym = a["period"]
        mask = pd.to_datetime(df["donation_date"]).dt.to_period("M").astype(str) == ym
        if not mask.any():
            continue
        idx = df.index[mask]
        pick = int(g.rng.choice(idx.to_numpy()))
        # 85 万元一次性大额定向捐赠
        corp = g.rng.choice(idx.to_numpy(), size=min(6, len(idx)), replace=False)
        df.loc[pick, ["amount_cny", "channel", "designation", "receipt_issued"]] = [
            850_000.0, "银行转账", "定向", True,
        ]
        df.loc[[i for i in corp if i != pick], "amount_cny"] *= 1.9
    return df


def build_in_kind(g: Generator, donors: pd.DataFrame, projects: pd.DataFrame) -> pd.DataFrame:
    """物资捐赠入库单。"""
    institutional = donors[donors["donor_type"] != "个人"]
    items = list(IN_KIND_ITEMS)
    item_w = np.array([0.16, 0.15, 0.13, 0.11, 0.13, 0.10, 0.05, 0.06, 0.05, 0.06])
    rows = []
    n = 620
    for i in range(n):
        donor = institutional.sample(n=1, random_state=int(g.rng.integers(0, 2**31 - 1))).iloc[0]
        item = str(g.choice(items, p=item_w))
        cat, unit, price = IN_KIND_ITEMS[item]
        day = g.weighted_date(g.start, g.as_of, 1, recency=1.1)[0]
        qty = int(np.clip(g.lognormal(4.0, 0.9, 1)[0], 5, 4000))
        unit_price = round(price * float(g.lognormal(0.0, 0.18, 1)[0]), 2)
        rows.append(
            {
                "inkind_id": f"IK-{i + 1:05d}",
                "donor_id": donor["donor_id"],
                "project_id": str(projects.sample(n=1, random_state=int(g.rng.integers(0, 2**31 - 1))).iloc[0]["project_id"]),
                "item_name": item,
                "item_category": cat,
                "quantity": qty,
                "unit": unit,
                "est_unit_value_cny": unit_price,
                "est_total_value_cny": round(qty * unit_price, 2),
                "received_date": day.isoformat(),
                "warehouse": str(g.choice(["香格里拉中心仓", "德钦分仓", "维西分仓"], p=[0.58, 0.2, 0.22])),
            }
        )
    return pd.DataFrame(rows)


def build_beneficiaries(g: Generator, projects: pd.DataFrame) -> pd.DataFrame:
    n = g.cfg.simulation.n_beneficiaries
    rows = []
    # 让服务对象按项目规模分配到项目
    pw = projects["target_beneficiaries"].clip(lower=1).astype(float)
    pw = (pw / pw.sum()).to_numpy()
    proj_idx = g.rng.choice(projects.index.to_numpy(), size=n, p=pw)

    for i in range(n):
        p = projects.loc[proj_idx[i]]
        county = p["county"]
        # 服务对象年龄必须与项目类别自洽：助学面向学龄儿童，助老面向 60+ 老人
        cat = p["category"]
        if cat == "助学支教":
            age = int(np.clip(g.rng.normal(11.5, 3.4), 6, 19))
        elif cat == "助老敬老":
            age = int(np.clip(g.rng.normal(72.0, 8.5), 58, 98))
        elif cat == "助残扶弱":
            age = int(np.clip(g.rng.normal(33.0, 19.0), 5, 88))
        elif cat == "文化传承":
            age = int(np.clip(g.rng.normal(16.5, 8.0), 7, 70))
        else:
            age = int(np.clip(g.rng.normal(41.0, 13.0), 18, 78))
        ethnicity = str(g.choice(list(ETHNIC_WEIGHTS), p=np.array(list(ETHNIC_WEIGHTS.values()))))
        gender = str(g.choice(["男", "女"], p=[0.51, 0.49]))
        enroll = max(
            g.start,
            min(g.as_of - timedelta(days=15), pd.Timestamp(p["planned_start_date"]).date() + timedelta(days=int(g.rng.integers(0, 90)))),
        )
        rows.append(
            {
                "beneficiary_id": f"BN-{i + 1:06d}",
                "project_id": p["project_id"],
                "county": county,
                "township": p["township"],
                "village": f"{p['township'].replace('镇', '').replace('乡', '')}{int(g.rng.integers(1, 13))}村",
                "age": age,
                "gender": gender,
                "ethnicity": ethnicity,
                "household_type": str(g.choice(HOUSEHOLD_TYPES, p=HOUSEHOLD_WEIGHTS)),
                "enroll_date": enroll.isoformat(),
                "risk_score": round(float(np.clip(g.rng.normal(52, 17), 1, 99)), 1),
            }
        )
    return pd.DataFrame(rows)


def build_beneficiary_services(g: Generator, bene: pd.DataFrame, projects: pd.DataFrame) -> pd.DataFrame:
    proj_map = projects.set_index("project_id")
    rows = []
    sv_no = 0
    # 服务频次：高风险对象被更频繁跟进
    for _, b in bene.iterrows():
        base = 1.4 + (b["risk_score"] / 100.0) * 3.2
        k = int(np.clip(g.rng.poisson(base), 1, 14))
        start = max(g.start, pd.Timestamp(b["enroll_date"]).date())
        if start >= g.as_of:
            continue
        p = proj_map.loc[b["project_id"]]
        p_end = pd.Timestamp(
            p["actual_end_date"] if isinstance(p["actual_end_date"], str) and p["actual_end_date"] else p["planned_end_date"]
        ).date()
        hi = min(g.as_of, max(start + timedelta(days=1), p_end))
        types, type_p = SERVICE_TYPE_BY_CATEGORY.get(
            str(p["category"]), (BENEFICIARY_SERVICE_TYPES, None)
        )
        sat_mu = SATISFACTION_BY_HOUSEHOLD.get(str(b["household_type"]), 4.42)
        for _ in range(k):
            d = g.dates_between(start, hi, 1)[0]
            sv_no += 1
            sat = float(np.clip(g.rng.normal(sat_mu, 0.72), 1, 5))
            rows.append(
                {
                    "service_id": f"SV-{sv_no:07d}",
                    "beneficiary_id": b["beneficiary_id"],
                    "project_id": b["project_id"],
                    "service_date": d.isoformat(),
                    "service_type": str(g.choice(types, p=type_p)),
                    "duration_hours": round(float(np.clip(g.lognormal(0.9, 0.55, 1)[0], 0.3, 10)), 2),
                    "provider_id": f"VN-{int(g.rng.integers(1, g.cfg.simulation.n_volunteers + 1)):05d}",
                    "satisfaction_score": round(sat, 1),
                    "followup_needed": bool(sat < 4.0),
                    "county": b["county"],
                }
            )
    df = pd.DataFrame(rows).sort_values("service_date").reset_index(drop=True)
    return apply_planted_service_anomaly(g, df)


def apply_planted_service_anomaly(g: Generator, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for a in PLANTED_ANOMALIES:
        if a["metric"] != "beneficiary_service_count" or a["cause_type"] != "data_error":
            continue
        ym = pd.to_datetime(df["service_date"]).dt.to_period("M").astype(str)
        mask = (ym == a["period"]) & (df["county"] == a["scope"])
        if not mask.any():
            continue
        idx = df.index[mask]
        # 整表重复导入：把该片区 6 月记录复制两遍
        dup = pd.concat([df.loc[idx]] * 2, ignore_index=True)
        dup["service_id"] = [f"SV-DUP{int(g.rng.integers(10**8, 10**9))}" for _ in range(len(dup))]
        df = pd.concat([df, dup], ignore_index=True)
    return df


def build_budget_execution(g: Generator, projects: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, p in projects.iterrows():
        p_start = pd.Timestamp(p["planned_start_date"])
        p_end = pd.Timestamp(
            p["actual_end_date"] if isinstance(p["actual_end_date"], str) and p["actual_end_date"] else p["planned_end_date"]
        )
        p_end = min(p_end, pd.Timestamp(g.as_of))
        months = pd.date_range(p_start.to_period("M").to_timestamp(), p_end.to_period("M").to_timestamp(), freq="MS")
        if not len(months):
            continue
        total_budget = float(p["planned_budget_cny"])
        # 各月预算权重：项目前期低、中期高
        x = np.linspace(-2.2, 2.2, len(months))
        weights = 1.0 / (1.0 + np.exp(-x))
        weights = weights / weights.sum() * float(g.lognormal(0.0, 0.12, 1)[0])
        weights = weights / weights.sum()

        # 执行率：整体约 0.93，个别项目偏低；维西 2026-03 存在跨期入账异常
        base_rate = float(np.clip(g.rng.normal(0.93, 0.09), 0.55, 1.12))
        for ts, w in zip(months, weights):
            for cat in BUDGET_CATEGORIES:
                cat_w = BUDGET_CATEGORY_WEIGHTS[BUDGET_CATEGORIES.index(cat)]
                planned = total_budget * w * cat_w
                if planned < 1:
                    continue
                actual = planned * base_rate * float(g.lognormal(0.0, 0.22, 1)[0])
                ym = ts.strftime("%Y-%m")
                if (
                    p["county"] == "维西傈僳族自治县"
                    and ym == "2026-03"
                    and cat in {"人力成本", "物资采购"}
                ):
                    actual *= 0.35  # ANOM-06 跨期入账
                rows.append(
                    {
                        "month": ym,
                        "project_id": p["project_id"],
                        "budget_category": cat,
                        "planned_cny": round(planned, 2),
                        "actual_cny": round(max(actual, 0.0), 2),
                    }
                )
    return pd.DataFrame(rows)


def build_survey(g: Generator, projects: pd.DataFrame) -> pd.DataFrame:
    """问卷星导出的满意度原始回收表 —— 全仓库最"脏"的一张表。"""
    rows = []
    n = 1450
    for i in range(n):
        p = projects.sample(n=1, random_state=int(g.rng.integers(0, 2**31 - 1))).iloc[0]
        county = p["county"]
        submit = g.weighted_date(g.start, g.as_of, 1, recency=1.3)[0]
        score = int(np.clip(round(g.rng.normal(4.4, 0.85)), 1, 5))
        county_raw = str(g.choice([county, county.replace("傈僳族自治县", ""), county.replace("市", ""), f" {county} "]))
        age_raw = str(g.choice([str(int(np.clip(g.rng.normal(41, 15), 15, 88))), f"{int(np.clip(g.rng.normal(41, 15), 15, 88))}岁"]))
        gender_raw = str(g.choice(["男", "女", "M", "F", "1", "2"]))
        rows.append(
            {
                "response_id": f"R{i + 1:05d}",
                "submit_time": str(g.choice([
                    submit.isoformat(),
                    submit.strftime("%Y/%m/%d"),
                    f"{submit.year}年{submit.month}月{submit.day}日",
                ])),
                "project_name_raw": p["project_name"] if g.rng.random() > 0.12 else p["project_name"].replace("·", "-"),
                "county_raw": county_raw,
                "gender_raw": gender_raw,
                "age_raw": age_raw,
                "Q1_总体满意度": str(g.choice(SURVEY_SATISFACTION_TEXT[score])),
                "Q2_志愿者服务态度": str(g.choice(SURVEY_SATISFACTION_TEXT[int(np.clip(score + g.rng.integers(-1, 2), 1, 5))])),
                "Q3_是否愿意再次参与": str(g.choice(["愿意", "非常愿意", "不确定", "不愿意"], p=[0.42, 0.44, 0.11, 0.03])),
                "Q4_服务频次是否足够": str(g.choice(["足够", "基本足够", "偏少", "太少"], p=[0.38, 0.34, 0.20, 0.08])),
                "open_comment": str(g.choice([
                    "", "", "", "希望多来几次", "志愿者很耐心，孩子很喜欢", "路太远，来回不方便",
                    "物资发放很及时", "希望能增加技能培训", "感谢党和政府", "老师教得好",
                ])),
            }
        )
    return pd.DataFrame(rows)


# =============================================================================
# 五、数据缺陷注入（并记录 ground truth）
# =============================================================================

DATE_FORMATS = ["%Y/%m/%d", "%Y年%m月%d日", "%d-%m-%Y", "%Y.%m.%d"]


@dataclass
class DefectInjector:
    """按配置比例向指定列注入缺陷，同时产出可用于评估清洗效果的 ground truth。"""

    g: Generator
    cfg: Config
    records: list[dict[str, Any]]

    @classmethod
    def create(cls, g: Generator) -> DefectInjector:
        return cls(g=g, cfg=g.cfg, records=[])

    def _record(self, table: str, pos: int, column: str, defect: str, original: Any, injected: Any, key: Any) -> None:
        self.records.append(
            {
                "table": table,
                "row_position": int(pos),
                "record_key": key,
                "column": column,
                "defect_type": defect,
                "original_value": "" if original is None else str(original),
                "injected_value": "" if injected is None else str(injected),
            }
        )

    def inject(
        self,
        df: pd.DataFrame,
        table: str,
        key_col: str,
        *,
        date_cols: list[str] | None = None,
        numeric_cols: list[str] | None = None,
        unit_col: str | None = None,
        unit_choices: list[str] | None = None,
        orphan_spec: tuple[str, str, list[Any]] | None = None,
        outlier_spec: list[tuple[str, Any]] | None = None,
        text_cols: list[str] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """返回 (dirty_df, defect_rows_of_this_table)。

        ``orphan_spec``: (column, orphan_value, pool_of_valid_values)
        ``outlier_spec``: [(column, value), ...] 用于注入极端值
        """
        dq = self.cfg.data_quality
        rng = self.g.rng
        df = df.copy().reset_index(drop=True)
        n = len(df)
        before = len(self.records)
        date_cols = date_cols or []
        numeric_cols = numeric_cols or []
        text_cols = text_cols or []
        unit_choices = unit_choices or ["件", "箱", "套"]

        def pick(rate: float, cap: int | None = None) -> np.ndarray:
            k = int(round(n * rate))
            if cap is not None:
                k = min(k, cap)
            if k <= 0:
                return np.array([], dtype=int)
            return rng.choice(n, size=k, replace=False)

        # ---- 1) 重复行 ----
        dup_idx = pick(dq.duplicate_rate)
        if len(dup_idx):
            dups = df.loc[dup_idx].copy()
            start_pos = n
            df = pd.concat([df, dups], ignore_index=True)
            for j, src in enumerate(dup_idx):
                self._record(
                    table, start_pos + j, "*", "duplicate_row",
                    f"row#{src}", "duplicate_copy", df.loc[start_pos + j, key_col],
                )

        # ---- 2) 关键字段缺失 ----
        preferred_missing = {
            "volunteer_shifts": ["checkin_method", "service_role", "county", "project_id"],
            "beneficiary_services": ["service_type", "provider_id", "satisfaction_score"],
            "donations": ["channel", "designation"],
        }
        mis_idx = pick(dq.missing_rate * 3)  # 分散到多列
        if len(mis_idx):
            cands = [c for c in preferred_missing.get(table, []) if c in df.columns]
            if not cands:
                cands = [c for c in df.columns if c not in {key_col} and df[c].dtype == object]
            for j, pos in enumerate(mis_idx):
                if not cands:
                    continue
                c = cands[j % len(cands)]
                self._record(table, pos, c, "missing_value", df.at[pos, c], "", df.at[pos, key_col])
                df.at[pos, c] = np.nan

        # ---- 3) 日期格式混乱 ----
        for c in date_cols:
            for pos in pick(dq.malformed_date_rate):
                try:
                    ts = pd.Timestamp(df.at[pos, c])
                except Exception:
                    continue
                if pd.isna(ts):  # 空串 / NaT（如进行中项目的实际结束日期）
                    continue
                fmt = DATE_FORMATS[int(rng.integers(0, len(DATE_FORMATS)))]
                bad = ts.strftime(fmt)
                self._record(table, pos, c, "malformed_date", df.at[pos, c], bad, df.at[pos, key_col])
                df.at[pos, c] = bad

        # ---- 4) 数值单位混用 ----
        if unit_col and numeric_cols:
            c = numeric_cols[0]
            df[c] = df[c].astype(object)  # 允许同一列混入数值与带单位的字符串
            for pos in pick(dq.unit_inconsistency_rate):
                raw = df.at[pos, c]
                if pd.isna(raw):
                    continue
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    continue
                style = int(rng.integers(0, 3))
                if table == "volunteer_shifts":
                    # 分钟 / 小时 / 中文单位 三种写法混用
                    bad = f"{v:g}h" if style == 0 else (f"{int(round(v * 60))}min" if style == 1 else f"{v:g} 小时")
                else:
                    # 数量列的计量单位随手填写（箱/件/套混淆）
                    bad = f"{v:g}{str(rng.choice(unit_choices))}"
                self._record(table, pos, c, "unit_inconsistency", raw, bad, df.at[pos, key_col])
                df.at[pos, c] = bad

        # ---- 5) 文本空格 / 全半角 ----
        for c in text_cols:
            for pos in pick(dq.whitespace_rate):
                raw = df.at[pos, c]
                if not isinstance(raw, str) or not raw:
                    continue
                bad = raw.replace("（", "(").replace("）", ")")
                bad = str(rng.choice([f"  {bad}", f"{bad}  ", f" {bad} ", bad.replace("·", " ")]))
                if bad == raw:
                    bad = f" {raw}"
                self._record(table, pos, c, "whitespace_or_widechar", raw, bad, df.at[pos, key_col])
                df.at[pos, c] = bad

        # ---- 6) 悬空外键 ----
        if orphan_spec:
            col, orphan, pool = orphan_spec
            for pos in pick(dq.orphan_key_rate):
                valid = df.at[pos, col]
                self._record(table, pos, col, "orphan_foreign_key", valid, orphan, df.at[pos, key_col])
                df.at[pos, col] = orphan if rng.random() < 0.5 else f"{orphan}{int(rng.integers(1, 90)):03d}"

        # ---- 7) 极端值 ----
        if outlier_spec:
            for c, val in outlier_spec:
                for pos in pick(dq.outlier_rate):
                    self._record(table, pos, c, "extreme_outlier", df.at[pos, c], val, df.at[pos, key_col])
                    df.at[pos, c] = val

        defects = pd.DataFrame(self.records[before:])
        return df.reset_index(drop=True), defects


# =============================================================================
# 六、主流程
# =============================================================================


def _checksum(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成迪庆清风青联公益项目模拟多源数据")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--seed", type=int, default=None, help="覆盖随机种子")
    parser.add_argument("--out", default=None, help="输出目录（默认取配置 data/raw）")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, ensure_dirs=True)
    out_dir = Path(args.out).resolve() if args.out else cfg.paths.raw
    out_dir.mkdir(parents=True, exist_ok=True)
    gt_dir = out_dir / "_ground_truth"
    gt_dir.mkdir(exist_ok=True)

    g = Generator.create(cfg, args.seed)
    logger.info("随机种子 = %s | 数据窗口 = %s ~ %s", g.seed, g.start, g.as_of)

    timer = RunTimer()
    inj = DefectInjector.create(g)
    tables: list[pd.DataFrame] = []
    defects_all: list[pd.DataFrame] = []

    with timer.stage("生成 staff / projects / volunteers"):
        staff = build_staff(g)
        projects = build_projects(g, staff)
        volunteers = build_volunteers(g)

    with timer.stage("生成 volunteer_shifts（含植入异常）"):
        shifts = build_shifts(g, projects, volunteers)

    with timer.stage("生成 donors / donations / in-kind"):
        donors = build_donors(g)
        donations = build_donations(g, donors, projects)
        inkind = build_in_kind(g, donors, projects)

    with timer.stage("生成 beneficiaries / services"):
        bene = build_beneficiaries(g, projects)
        services = build_beneficiary_services(g, bene, projects)

    with timer.stage("生成 monthly budget execution / survey"):
        budget = build_budget_execution(g, projects)
        survey = build_survey(g, projects)

    with timer.stage("注入数据质量缺陷并记录 ground truth"):
        # 各表按业务特征选择注入策略
        clean_specs = {
            "projects": dict(
                df=projects, key_col="project_id",
                date_cols=["planned_start_date", "planned_end_date"],
                numeric_cols=["planned_budget_cny"],
                outlier_spec=[("planned_budget_cny", -50_000.0)],
                text_cols=["project_name", "county"],
            ),
            "volunteers": dict(
                df=volunteers, key_col="volunteer_id",
                date_cols=["join_date"],
                numeric_cols=["birth_year"],
                outlier_spec=[("birth_year", 1899)],
                text_cols=["county", "occupation"],
            ),
            "donors": dict(
                df=donors, key_col="donor_id",
                date_cols=["first_donation_date"],
                text_cols=["county", "donor_type"],
            ),
            "volunteer_shifts": dict(
                df=shifts, key_col="shift_id",
                date_cols=["service_date"],
                numeric_cols=["hours"], unit_col="hours",
                orphan_spec=("volunteer_id", "VN-99999", None),
                outlier_spec=[("hours", -3.5)],
                text_cols=["county", "service_role"],
            ),
            "donations": dict(
                df=donations, key_col="donation_id",
                date_cols=["donation_date"],
                numeric_cols=["amount_cny"],
                orphan_spec=("donor_id", "DN-99999", None),
                outlier_spec=[("amount_cny", -12_000.0), ("amount_cny", 3_000_000.0)],
                text_cols=["channel"],
            ),
            "in_kind_donations": dict(
                df=inkind, key_col="inkind_id",
                date_cols=["received_date"],
                numeric_cols=["quantity"], unit_col="quantity",
                unit_choices=["件", "箱", "套", "包", "袋"],
                outlier_spec=[("quantity", -20), ("est_total_value_cny", 9_999_999.0)],
                text_cols=["warehouse"],
            ),
            "beneficiaries": dict(
                df=bene, key_col="beneficiary_id",
                date_cols=["enroll_date"],
                numeric_cols=["age"],
                outlier_spec=[("age", 145)],
                text_cols=["township"],
            ),
            "beneficiary_services": dict(
                df=services, key_col="service_id",
                date_cols=["service_date"],
                numeric_cols=["duration_hours"],
                orphan_spec=("beneficiary_id", "BN-999999", None),
                outlier_spec=[("duration_hours", 96.0)],
                text_cols=["service_type"],
            ),
            "monthly_budget_execution": dict(
                df=budget, key_col="month",
                date_cols=[],
                numeric_cols=["actual_cny"],
                outlier_spec=[("actual_cny", -88_888.0)],
                text_cols=["budget_category"],
            ),
            "satisfaction_survey_raw": dict(
                df=survey, key_col="response_id",
                date_cols=["submit_time"],
                numeric_cols=["age_raw"],
                text_cols=["county_raw"],
            ),
        }

        dirtied: dict[str, pd.DataFrame] = {}
        for name, spec in clean_specs.items():
            df = spec.pop("df")
            dirty, dfd = inj.inject(df, name, **spec)
            dirtied[name] = dirty
            if len(dfd):
                defects_all.append(dfd)
            logger.info("  %-28s clean=%-7d dirty=%-7d defects=%d", name, len(df), len(dirty), len(dfd))

        # staff 表保持干净（作为参照维度表）
        dirtied["staff"] = staff

    with timer.stage("写出 CSV 与 ground truth"):
        order = [
            "projects", "staff", "volunteers", "volunteer_shifts", "donors", "donations",
            "in_kind_donations", "beneficiaries", "beneficiary_services",
            "monthly_budget_execution", "satisfaction_survey_raw",
        ]
        manifest_rows = []
        for name in order:
            path = out_dir / f"{name}.csv"
            dirtied[name].to_csv(path, **CSV_KWARGS)
            manifest_rows.append(
                {
                    "table": name,
                    "file": path.name,
                    "rows": int(len(dirtied[name])),
                    "columns": int(dirtied[name].shape[1]),
                    "bytes": path.stat().st_size,
                    "sha256_16": _checksum(path),
                }
            )
            tables.append(dirtied[name])

        defects = (
            pd.concat(defects_all, ignore_index=True) if defects_all else pd.DataFrame(
                columns=["table", "row_position", "record_key", "column", "defect_type", "original_value", "injected_value"]
            )
        )
        defects.to_csv(gt_dir / "injected_defects.csv", **CSV_KWARGS)

        anomalies = pd.DataFrame(PLANTED_ANOMALIES)
        anomalies.to_csv(gt_dir / "planted_anomalies.csv", **CSV_KWARGS)

        manifest = {
            "generator": "scripts/generate_synthetic_data.py",
            "seed": g.seed,
            "config_file": str(cfg.source_path),
            "date_window": [g.start.isoformat(), g.as_of.isoformat()],
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "tables": manifest_rows,
            "injected_defects_by_type": defects["defect_type"].value_counts().to_dict() if len(defects) else {},
            "planted_anomalies": len(anomalies),
            "disclaimer": (
                "本目录下所有数据均为程序生成的模拟数据，与任何真实个人或机构无关，"
                "不得作为真实统计口径引用。分布参数依据 data/external/calibration_reference.md 校准。"
            ),
        }
        (out_dir / "_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(timer.summary())
    total_rows = sum(len(t) for t in tables)
    logger.info("✔ 已生成 %d 张表 / %s 行 / 缺陷记录 %d 条 -> %s", len(tables), f"{total_rows:,}", len(defects), out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
