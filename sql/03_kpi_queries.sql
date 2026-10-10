-- =============================================================================
-- 迪庆清风青年公益联合会 · 公益项目数据运营分析
-- 03_kpi_queries.sql —— 核心指标体系（KPI）查询集
-- =============================================================================
--
-- 【使用方式】
--   sqlite3 data/processed/qingfeng.sqlite < sql/03_kpi_queries.sql
--   或：python -m qingfeng.cli sql --file sql/03_kpi_queries.sql
--
-- 【口径约定】（全文件统一遵守，逐条查询不再重复声明）
--   1. 维度表的 -1 号为 UNKNOWN 成员，代表"源系统中缺失或无法匹配"。
--      规模类与结构类指标**排除 -1**，只统计可归因到具体实体的记录；
--      涉及"总业务量是否被脏数据侵蚀"的指标（如未知占比）才显式纳入。
--   2. 金额单位：元（CNY）。时长单位：小时。
--   3. 志愿服务经济价值按 30 元/小时 折算（由《慈善蓝皮书(2025)》口径反推，
--      详见 data/external/calibration_reference.md 与 config.yaml）。
--   4. 数据窗口：2024-01-01 ~ 2026-06-30（见 dim_date）。
-- =============================================================================


-- #############################################################################
-- 一、规模层指标（做了多少）
-- #############################################################################

-- 指标：在册公益项目数 | 口径：dim_project 中非 UNKNOWN 的项目数
SELECT
    COUNT(*)                                        AS 在册项目数,
    SUM(CASE WHEN status IN ('进行中', '筹备中') THEN 1 ELSE 0 END) AS 在办项目数,
    SUM(CASE WHEN is_completed = 1 THEN 1 ELSE 0 END)               AS 已结项项目数,
    ROUND(SUM(planned_budget_cny), 2)                                AS 计划预算总额
FROM dim_project
WHERE project_key <> -1;


-- 指标：注册志愿者数 | 口径：dim_volunteer 中非 UNKNOWN 的志愿者数
SELECT
    COUNT(*)                                                        AS 注册志愿者数,
    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END)                   AS 名册状态为活跃,
    SUM(CASE WHEN has_bilingual_skill = 1 THEN 1 ELSE 0 END)         AS 双语志愿者数,
    ROUND(AVG(age), 1)                                               AS 平均年龄
FROM dim_volunteer
WHERE volunteer_key <> -1;


-- 指标：志愿服务规模 | 口径：SUM(hours)，排除 UNKNOWN 志愿者
SELECT
    COUNT(*)                                    AS 服务人次,
    ROUND(SUM(hours), 2)                        AS 服务总时长,
    ROUND(SUM(volunteer_value_cny), 2)          AS 志愿服务经济价值,
    COUNT(DISTINCT volunteer_key)               AS 参与服务的志愿者数,
    COUNT(DISTINCT project_key)                 AS 被服务项目数
FROM fact_volunteer_service
WHERE volunteer_key <> -1 AND project_key <> -1;


-- 指标：捐赠规模 | 口径：SUM(amount_cny)，排除 UNKNOWN 捐赠人
SELECT
    COUNT(*)                            AS 捐赠笔数,
    ROUND(SUM(amount_cny), 2)           AS 现金捐赠总额,
    ROUND(AVG(amount_cny), 2)           AS 笔均捐赠额,
    COUNT(DISTINCT donor_key)           AS 捐赠人数,
    SUM(CASE WHEN is_major_gift = 1 THEN 1 ELSE 0 END) AS 大额捐赠笔数
FROM fact_donation
WHERE donor_key <> -1;


-- 指标：物资捐赠规模 | 口径：SUM(est_total_value_cny)
SELECT
    COUNT(*)                                AS 物资入库笔数,
    ROUND(SUM(est_total_value_cny), 2)      AS 物资折价总额,
    COUNT(DISTINCT item_category)           AS 物资品类数
FROM fact_in_kind;


-- 指标：受益覆盖 | 口径：建档对象数、接受服务人次
SELECT
    (SELECT COUNT(*) FROM dim_beneficiary WHERE beneficiary_key <> -1) AS 建档服务对象数,
    (SELECT COUNT(*) FROM fact_beneficiary_service WHERE beneficiary_key <> -1) AS 服务总人次,
    (SELECT COUNT(DISTINCT beneficiary_key) FROM fact_beneficiary_service
      WHERE beneficiary_key <> -1)                                     AS 实际被服务对象数;


-- #############################################################################
-- 二、活跃层指标（做得活不活）
-- #############################################################################

-- 指标：志愿者活跃率 | 口径：有签到记录的志愿者数 ÷ 注册志愿者总数
-- 参照：全国活跃志愿者占比 28.72%（《慈善蓝皮书(2025)》）
WITH registered AS (
    SELECT COUNT(*) AS n_registered FROM dim_volunteer WHERE volunteer_key <> -1
),
active AS (
    SELECT COUNT(DISTINCT volunteer_key) AS n_active
    FROM fact_volunteer_service
    WHERE volunteer_key <> -1
)
SELECT
    a.n_active                                                  AS 活跃志愿者数,
    r.n_registered                                              AS 注册志愿者数,
    ROUND(1.0 * a.n_active / r.n_registered, 4)                 AS 志愿者活跃率,
    ROUND(1.0 * a.n_active / r.n_registered - 0.2872, 4)        AS 与全国基线之差
FROM active a, registered r;


-- 指标：人均服务时长与人均服务次数 | 分母 = 活跃志愿者数
WITH per_vol AS (
    SELECT volunteer_key,
           COUNT(*)     AS n_shifts,
           SUM(hours)   AS hours
    FROM fact_volunteer_service
    WHERE volunteer_key <> -1
    GROUP BY volunteer_key
)
SELECT
    COUNT(*)                                AS 活跃志愿者数,
    ROUND(AVG(hours), 2)                    AS 人均服务时长,
    ROUND(AVG(n_shifts), 2)                 AS 人均服务次数,
    ROUND(MIN(hours), 2)                    AS 最少时长,
    ROUND(MAX(hours), 2)                    AS 最多时长
FROM per_vol;


-- 指标：单次服务时长统计（中位数用 ORDER BY + LIMIT 实现，SQLite 无 MEDIAN 内置函数）
SELECT
    COUNT(*)                        AS 服务人次,
    ROUND(AVG(hours), 3)            AS 平均时长,
    MIN(hours)                      AS 最短时长,
    MAX(hours)                      AS 最长时长
FROM fact_volunteer_service
WHERE volunteer_key <> -1;

-- 指标：单次服务时长中位数
SELECT
    ROUND(AVG(hours), 3) AS 单次服务时长中位数
FROM (
    SELECT hours
    FROM fact_volunteer_service
    WHERE volunteer_key <> -1
    ORDER BY hours
    LIMIT 2 - (SELECT COUNT(*) FROM fact_volunteer_service WHERE volunteer_key <> -1) % 2
    OFFSET (SELECT (COUNT(*) - 1) / 2 FROM fact_volunteer_service WHERE volunteer_key <> -1)
);


-- 指标：服务对象复访率 | 口径：服务 ≥2 次的对象数 ÷ 有服务记录的对象数
WITH per_bene AS (
    SELECT beneficiary_key, COUNT(*) AS n
    FROM fact_beneficiary_service
    WHERE beneficiary_key <> -1
    GROUP BY beneficiary_key
)
SELECT
    COUNT(*)                                                        AS 被服务对象数,
    SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END)                         AS 复访对象数,
    ROUND(1.0 * SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) / COUNT(*), 4) AS 复访率,
    ROUND(AVG(n), 2)                                                AS 人均被服务次数
FROM per_bene;


-- 指标：志愿者一年留存率
-- 口径：注册满 12 个月者中，最近 6 个月（相对 2026-06-30 数据截止日）仍有服务记录的比例
WITH cutoff AS (SELECT DATE('2026-06-30') AS d),
old_vol AS (
    SELECT v.volunteer_key
    FROM dim_volunteer v, cutoff c
    WHERE v.volunteer_key <> -1
      AND DATE(v.join_date) <= DATE(c.d, '-12 months')
),
recent AS (
    SELECT DISTINCT f.volunteer_key
    FROM fact_volunteer_service f, cutoff c
    WHERE f.volunteer_key <> -1
      AND DATE(substr(CAST(f.date_key AS TEXT), 1, 4) || '-' ||
               substr(CAST(f.date_key AS TEXT), 5, 2) || '-' ||
               substr(CAST(f.date_key AS TEXT), 7, 2)) >= DATE(c.d, '-6 months')
)
SELECT
    (SELECT COUNT(*) FROM old_vol)                                          AS 注册满一年人数,
    (SELECT COUNT(*) FROM old_vol WHERE volunteer_key IN (SELECT volunteer_key FROM recent))
                                                                            AS 近半年仍活跃人数,
    ROUND(1.0 * (SELECT COUNT(*) FROM old_vol
                 WHERE volunteer_key IN (SELECT volunteer_key FROM recent))
          / (SELECT COUNT(*) FROM old_vol), 4)                              AS 一年留存率;


-- #############################################################################
-- 三、效率层指标（做得省不省）
-- #############################################################################

-- 指标：单位服务成本、每小时服务成本、捐赠撬动服务时长
WITH spend AS (
    SELECT SUM(actual_cny) AS total_spend FROM fact_project_monthly
),
serv AS (
    SELECT SUM(hours) AS total_hours, COUNT(*) AS n_visits
    FROM fact_volunteer_service WHERE volunteer_key <> -1
),
don AS (
    SELECT SUM(amount_cny) AS total_donation FROM fact_donation WHERE donor_key <> -1
),
vis AS (
    SELECT COUNT(*) AS n_services FROM fact_beneficiary_service
)
SELECT
    ROUND(s.total_spend, 2)                                     AS 项目实际支出总额,
    ROUND(s.total_spend / v.n_services, 2)                      AS 单位服务成本_元每人次,
    ROUND(s.total_spend / sv.total_hours, 2)                    AS 每小时服务成本_元,
    ROUND(sv.total_hours / d.total_donation * 10000, 2)         AS 每万元捐赠撬动服务时长_小时
FROM spend s, serv sv, don d, vis v;


-- 指标：项目层面成本效率 Top / Bottom
SELECT
    p.project_name                                          AS 项目名称,
    p.category                                              AS 类别,
    ROUND(m.actual_cny, 0)                                  AS 实际支出,
    COALESCE(v.n_visits, 0)                                 AS 服务人次,
    ROUND(m.actual_cny / NULLIF(v.n_visits, 0), 2)          AS 单位服务成本
FROM (
    SELECT project_key, SUM(actual_cny) AS actual_cny
    FROM fact_project_monthly GROUP BY project_key
) m
JOIN dim_project p ON p.project_key = m.project_key
LEFT JOIN (
    SELECT project_key, COUNT(*) AS n_visits
    FROM fact_beneficiary_service WHERE project_key <> -1 GROUP BY project_key
) v ON v.project_key = m.project_key
WHERE p.project_key <> -1
ORDER BY 单位服务成本 DESC
LIMIT 10;


-- #############################################################################
-- 四、质量层指标（做得好不好）
-- #############################################################################

-- 指标：服务满意度均值与达标率
SELECT
    COUNT(*)                                                        AS 有效评分记录数,
    ROUND(AVG(satisfaction_score), 4)                               AS 满意度均值,
    ROUND(1.0 * SUM(CASE WHEN satisfaction_score >= 4 THEN 1 ELSE 0 END)
          / COUNT(*), 4)                                            AS 满意度达标率_4分及以上,
    ROUND(1.0 * SUM(CASE WHEN is_dissatisfied = 1 THEN 1 ELSE 0 END)
          / COUNT(*), 4)                                            AS 不满意率_低于3分,
    ROUND(AVG(duration_hours), 3)                                   AS 单次服务平均时长
FROM fact_beneficiary_service
WHERE beneficiary_key <> -1 AND satisfaction_score IS NOT NULL;


-- 指标：项目预算执行率 | 口径：SUM(实际) ÷ SUM(计划)
SELECT
    ROUND(SUM(planned_cny), 2)                              AS 计划支出总额,
    ROUND(SUM(actual_cny), 2)                               AS 实际支出总额,
    ROUND(SUM(actual_cny) / SUM(planned_cny), 4)            AS 预算执行率,
    SUM(CASE WHEN is_overspend = 1 THEN 1 ELSE 0 END)       AS 超支科目月数
FROM fact_project_monthly;


-- 指标：项目按期结项率
SELECT
    COUNT(*)                                                                    AS 已结项项目数,
    SUM(CASE WHEN plan_delay_days <= 0 THEN 1 ELSE 0 END)                       AS 按期数,
    ROUND(1.0 * SUM(CASE WHEN plan_delay_days <= 0 THEN 1 ELSE 0 END) / COUNT(*), 4) AS 按期结项率,
    ROUND(AVG(plan_delay_days), 1)                                              AS 平均延期天数
FROM dim_project
WHERE project_key <> -1 AND is_completed = 1;


-- 指标：需跟进的服务占比
SELECT
    COUNT(*)                                                        AS 服务人次,
    SUM(followup_needed)                                            AS 需跟进人次,
    ROUND(1.0 * SUM(followup_needed) / COUNT(*), 4)                 AS 需跟进占比
FROM fact_beneficiary_service;


-- #############################################################################
-- 五、结构层指标（资源结构健不健康）
-- #############################################################################

-- 指标：捐赠来源集中度 HHI | 口径：SUM((单个捐赠人金额 ÷ 捐赠总额)²)
-- 参考阈值：> 0.25 视为过度依赖少数捐赠人
WITH per_donor AS (
    SELECT donor_key, SUM(amount_cny) AS amt
    FROM fact_donation
    WHERE donor_key <> -1
    GROUP BY donor_key
),
total AS (SELECT SUM(amt) AS t FROM per_donor)
SELECT
    COUNT(*)                                                AS 捐赠人数,
    ROUND((SELECT t FROM total), 2)                         AS 捐赠总额,
    ROUND(SUM(POWER(amt * 1.0 / (SELECT t FROM total), 2)), 5) AS HHI,
    ROUND(MAX(amt) * 1.0 / (SELECT t FROM total), 4)        AS 最大单一捐赠人金额占比
FROM per_donor;


-- 指标：捐赠人结构（金额与笔数双口径）
SELECT
    d.donor_type                                        AS 捐赠人类型,
    COUNT(*)                                            AS 捐赠笔数,
    ROUND(SUM(f.amount_cny), 2)                         AS 捐赠金额,
    ROUND(1.0 * SUM(f.amount_cny)
          / (SELECT SUM(amount_cny) FROM fact_donation WHERE donor_key <> -1), 4) AS 金额占比,
    ROUND(1.0 * COUNT(*)
          / (SELECT COUNT(*) FROM fact_donation WHERE donor_key <> -1), 4)        AS 笔数占比,
    ROUND(AVG(f.amount_cny), 2)                         AS 笔均金额
FROM fact_donation f
JOIN dim_donor d ON d.donor_key = f.donor_key
WHERE f.donor_key <> -1
GROUP BY d.donor_type
ORDER BY 捐赠金额 DESC;


-- 指标：捐赠渠道分布
SELECT
    channel                                             AS 捐赠渠道,
    COUNT(*)                                            AS 笔数,
    ROUND(SUM(amount_cny), 2)                           AS 金额,
    ROUND(AVG(amount_cny), 2)                           AS 笔均金额
FROM fact_donation
WHERE donor_key <> -1
GROUP BY channel
ORDER BY 金额 DESC;


-- 指标：帕累托集中度 | 口径：金额前 20% 捐赠人贡献的金额占比
WITH per_donor AS (
    SELECT donor_key, SUM(amount_cny) AS amt
    FROM fact_donation WHERE donor_key <> -1
    GROUP BY donor_key
),
ranked AS (
    SELECT amt,
           ROW_NUMBER() OVER (ORDER BY amt DESC) AS rn,
           COUNT(*) OVER ()                      AS n
    FROM per_donor
)
SELECT
    (SELECT COUNT(*) FROM per_donor)                            AS 捐赠人总数,
    CAST(CEIL((SELECT COUNT(*) FROM per_donor) * 0.2) AS INT)   AS 前20人数,
    ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.2) THEN amt ELSE 0 END), 2) AS 前20金额,
    ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.2) THEN amt ELSE 0 END)
          / (SELECT SUM(amt) FROM per_donor), 4)                AS 前20金额占比
FROM ranked;


-- 指标：数据质量三项（完整性 / 修正率 / 悬空外键率）
SELECT
    '志愿服务'  AS 事实表,
    COUNT(*)                                            AS 记录数,
    ROUND(1.0 * SUM(is_corrected) / COUNT(*), 4)        AS 修正记录率,
    ROUND(1.0 * SUM(has_orphan_key) / COUNT(*), 4)      AS 悬空外键率
FROM fact_volunteer_service
UNION ALL
SELECT '现金捐赠', COUNT(*), ROUND(1.0 * SUM(is_corrected) / COUNT(*), 4),
       ROUND(1.0 * SUM(has_orphan_key) / COUNT(*), 4)
FROM fact_donation
UNION ALL
SELECT '服务提供', COUNT(*), ROUND(1.0 * SUM(is_corrected) / COUNT(*), 4),
       ROUND(1.0 * SUM(has_orphan_key) / COUNT(*), 4)
FROM fact_beneficiary_service;


-- 指标：UNKNOWN 维度成员承载的业务量（数据质量信号，显式纳入 -1）
SELECT
    (SELECT COUNT(*) FROM fact_donation WHERE project_key = -1)          AS 无归属项目捐赠笔数,
    (SELECT COUNT(*) FROM fact_volunteer_service WHERE project_key = -1) AS 无归属项目签到数,
    (SELECT COUNT(*) FROM fact_beneficiary_service WHERE beneficiary_key = -1) AS 无匹配对象服务数,
    (SELECT COUNT(*) FROM fact_beneficiary_service WHERE geo_key = -1)   AS 无匹配地理服务数;
