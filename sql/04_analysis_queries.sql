-- =============================================================================
-- 迪庆清风青年公益联合会 · 公益项目数据运营分析
-- 04_analysis_queries.sql —— 描述性 / 交叉 / 趋势 / 归因 分析查询集
-- =============================================================================
--
-- 与 03_kpi_queries.sql 的分工：
--   03 回答"指标是多少"；04 回答"为什么是这样、结构差异在哪里、下一步该动谁"。
--
-- 口径约定同 03：维度 -1 为 UNKNOWN 成员，分析默认排除；
-- 金额单位元，时长单位小时，日期键为 YYYYMMDD 整数。
-- =============================================================================


-- #############################################################################
-- 一、月度趋势分析（含环比与同比）
-- #############################################################################

-- 分析：志愿服务时长与捐赠额的月度趋势 + 环比 + 同比
-- 说明：同比需 12 个月以上历史，2025-01 之后的月份才有同比值。
WITH base AS (
    SELECT d.year_month                                              AS 年月,
           SUM(f.hours)                                              AS 服务时长,
           COUNT(*)                                                   AS 服务人次,
           COUNT(DISTINCT f.volunteer_key)                            AS 活跃志愿者数
    FROM fact_volunteer_service f
    JOIN dim_date d ON d.date_key = f.date_key
    WHERE f.volunteer_key <> -1
    GROUP BY d.year_month
),
don AS (
    SELECT d.year_month AS 年月,
           SUM(f.amount_cny)  AS 捐赠额,
           COUNT(*)           AS 捐赠笔数
    FROM fact_donation f
    JOIN dim_date d ON d.date_key = f.date_key
    WHERE f.donor_key <> -1
    GROUP BY d.year_month
)
SELECT
    b.年月,
    ROUND(b.服务时长, 1)                                                        AS 服务时长,
    ROUND(b.服务时长 - LAG(b.服务时长) OVER (ORDER BY b.年月), 1)               AS 时长环比增量,
    ROUND(1.0 * b.服务时长 / LAG(b.服务时长) OVER (ORDER BY b.年月) - 1, 4)     AS 时长环比,
    ROUND(1.0 * b.服务时长 / LAG(b.服务时长, 12) OVER (ORDER BY b.年月) - 1, 4) AS 时长同比,
    b.活跃志愿者数,
    ROUND(COALESCE(d.捐赠额, 0), 0)                                             AS 捐赠额,
    ROUND(1.0 * COALESCE(d.捐赠额, 0)
          / LAG(COALESCE(d.捐赠额, 0)) OVER (ORDER BY b.年月) - 1, 4)           AS 捐赠环比,
    COALESCE(d.捐赠笔数, 0)                                                     AS 捐赠笔数,
    ROUND(b.服务时长 / NULLIF(COALESCE(d.捐赠额, 0), 0) * 10000, 2)             AS 每万元捐赠撬动时长
FROM base b
LEFT JOIN don d ON d.年月 = b.年月
ORDER BY b.年月;


-- 分析：月度预算执行率趋势
SELECT
    year_month                                          AS 年月,
    ROUND(SUM(planned_cny), 0)                          AS 计划支出,
    ROUND(SUM(actual_cny), 0)                           AS 实际支出,
    ROUND(SUM(actual_cny) / SUM(planned_cny), 4)        AS 预算执行率,
    SUM(is_overspend)                                   AS 超支科目月数
FROM fact_project_monthly
GROUP BY year_month
ORDER BY year_month;


-- 分析：年度季节性——各自然月的服务强度指数（1.0 = 全年月均）
WITH monthly AS (
    SELECT d.month AS 月,
           SUM(f.hours) AS 时长
    FROM fact_volunteer_service f
    JOIN dim_date d ON d.date_key = f.date_key
    WHERE f.volunteer_key <> -1
    GROUP BY d.year, d.month
)
SELECT
    m.月,
    ROUND(AVG(m.时长), 1)                                   AS 月均时长,
    ROUND(AVG(m.时长) / (SELECT AVG(时长) FROM monthly), 3)  AS 相对强度指数,
    COUNT(*)                                                AS 观测年数
FROM monthly m
GROUP BY m.月
ORDER BY 相对强度指数 DESC;


-- #############################################################################
-- 二、交叉分析（结构差异）
-- #############################################################################

-- 分析：县域 × 项目类别 的服务供给结构（行内占比）
SELECT
    p.county                                            AS 县,
    p.category                                          AS 项目类别,
    COUNT(*)                                            AS 服务人次,
    ROUND(SUM(f.hours), 1)                              AS 服务时长,
    ROUND(1.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY p.county), 4) AS 县内占比
FROM fact_volunteer_service f
JOIN dim_project p ON p.project_key = f.project_key
WHERE f.volunteer_key <> -1 AND p.project_key <> -1
GROUP BY p.county, p.category
ORDER BY p.county, 服务时长 DESC;


-- 分析：志愿者民族 × 服务角色（用于检验是否存在系统性分工差异）
SELECT
    v.ethnicity                                         AS 民族,
    f.service_role                                      AS 服务角色,
    COUNT(*)                                            AS 人次,
    ROUND(1.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY v.ethnicity), 4) AS 民族内占比
FROM fact_volunteer_service f
JOIN dim_volunteer v ON v.volunteer_key = f.volunteer_key
WHERE f.volunteer_key <> -1
GROUP BY v.ethnicity, f.service_role
HAVING 人次 >= 20
ORDER BY v.ethnicity, 人次 DESC;


-- 分析：捐赠渠道 × 捐赠人类型（募款投入应往哪里倾斜）
SELECT
    f.channel                                           AS 捐赠渠道,
    d.donor_type                                        AS 捐赠人类型,
    COUNT(*)                                            AS 笔数,
    ROUND(SUM(f.amount_cny), 0)                         AS 金额,
    ROUND(AVG(f.amount_cny), 1)                         AS 笔均金额
FROM fact_donation f
JOIN dim_donor d ON d.donor_key = f.donor_key
WHERE f.donor_key <> -1
GROUP BY f.channel, d.donor_type
ORDER BY 金额 DESC;


-- 分析：服务对象家庭类型 × 满意度（服务质量公平性）
SELECT
    b.household_type                                    AS 家庭类型,
    COUNT(*)                                            AS 服务人次,
    ROUND(AVG(s.satisfaction_score), 3)                 AS 满意度均值,
    ROUND(1.0 * SUM(CASE WHEN s.satisfaction_score >= 4 THEN 1 ELSE 0 END)
          / SUM(CASE WHEN s.satisfaction_score IS NOT NULL THEN 1 ELSE 0 END), 4) AS 满意度达标率,
    ROUND(1.0 * SUM(s.followup_needed) / COUNT(*), 4)   AS 需跟进占比,
    ROUND(AVG(b.risk_score), 1)                         AS 平均风险分
FROM fact_beneficiary_service s
JOIN dim_beneficiary b ON b.beneficiary_key = s.beneficiary_key
WHERE s.beneficiary_key <> -1
GROUP BY b.household_type
ORDER BY 满意度均值;


-- 分析：志愿者年龄段 × 服务角色
SELECT
    v.age_group                                         AS 年龄段,
    f.service_role                                      AS 服务角色,
    COUNT(*)                                            AS 人次,
    ROUND(SUM(f.hours), 1)                              AS 时长
FROM fact_volunteer_service f
JOIN dim_volunteer v ON v.volunteer_key = f.volunteer_key
WHERE f.volunteer_key <> -1
GROUP BY v.age_group, f.service_role
HAVING 人次 >= 30
ORDER BY v.age_group, 人次 DESC;


-- #############################################################################
-- 三、投入产出与项目绩效
-- #############################################################################

-- 分析：按项目类别的投入产出（预算 / 服务 / 满意度）
SELECT
    p.category                                          AS 项目类别,
    COUNT(DISTINCT p.project_key)                       AS 项目数,
    ROUND(SUM(DISTINCT p.planned_budget_cny), 0)        AS 计划预算,
    ROUND(m.actual, 0)                                  AS 实际支出,
    svc.服务人次,
    ROUND(svc.时长, 1)                                  AS 服务时长,
    ROUND(m.actual / NULLIF(svc.服务人次, 0), 2)        AS 单位服务成本,
    ROUND(svc.满意度, 3)                                AS 满意度均值
FROM dim_project p
LEFT JOIN (
    SELECT project_key, SUM(actual_cny) AS actual
    FROM fact_project_monthly GROUP BY project_key
) m ON m.project_key = p.project_key
LEFT JOIN (
    SELECT project_key, COUNT(*) AS 服务人次, SUM(duration_hours) AS 时长,
           AVG(satisfaction_score) AS 满意度
    FROM fact_beneficiary_service WHERE project_key <> -1 GROUP BY project_key
) svc ON svc.project_key = p.project_key
WHERE p.project_key <> -1
GROUP BY p.category
ORDER BY 服务时长 DESC;


-- 分析：项目综合表现排行（执行率 × 满意度 × 单位成本）
SELECT
    p.project_name                                                  AS 项目名称,
    p.category                                                      AS 类别,
    p.county                                                        AS 县,
    p.status                                                        AS 状态,
    ROUND(p.planned_budget_cny, 0)                                  AS 计划预算,
    ROUND(m.actual, 0)                                              AS 实际支出,
    ROUND(m.actual / NULLIF(p.planned_budget_cny, 0), 4)            AS 执行率,
    COALESCE(svc.服务人次, 0)                                       AS 服务人次,
    ROUND(svc.满意度, 3)                                            AS 满意度,
    ROUND(m.actual / NULLIF(svc.服务人次, 0), 2)                    AS 单位服务成本,
    COALESCE(p.plan_delay_days, 0)                                  AS 延期天数
FROM dim_project p
LEFT JOIN (
    SELECT project_key, SUM(actual_cny) AS actual
    FROM fact_project_monthly GROUP BY project_key
) m ON m.project_key = p.project_key
LEFT JOIN (
    SELECT project_key, COUNT(*) AS 服务人次, AVG(satisfaction_score) AS 满意度
    FROM fact_beneficiary_service WHERE project_key <> -1 GROUP BY project_key
) svc ON svc.project_key = p.project_key
WHERE p.project_key <> -1
ORDER BY 执行率 DESC, 满意度 DESC
LIMIT 20;


-- 分析：月度预算执行率异常项目（跨期入账 / 执行滞后排查）
WITH proj_month AS (
    SELECT project_key, year_month,
           SUM(planned_cny) AS planned, SUM(actual_cny) AS actual
    FROM fact_project_monthly
    GROUP BY project_key, year_month
),
stats AS (
    SELECT project_key,
           AVG(actual / NULLIF(planned, 0)) AS avg_rate
    FROM proj_month
    WHERE planned > 0
    GROUP BY project_key
)
SELECT
    pm.year_month                                       AS 年月,
    p.project_name                                      AS 项目名称,
    p.county                                            AS 县,
    ROUND(pm.planned, 0)                                AS 计划支出,
    ROUND(pm.actual, 0)                                 AS 实际支出,
    ROUND(pm.actual / NULLIF(pm.planned, 0), 4)         AS 当月执行率,
    ROUND(s.avg_rate, 4)                                AS 项目平均执行率,
    ROUND(pm.actual / NULLIF(pm.planned, 0) - s.avg_rate, 4) AS 偏离度
FROM proj_month pm
JOIN dim_project p ON p.project_key = pm.project_key
JOIN stats s ON s.project_key = pm.project_key
WHERE pm.planned > 1000
ORDER BY ABS(pm.actual / NULLIF(pm.planned, 0) - s.avg_rate) DESC
LIMIT 15;


-- #############################################################################
-- 四、异常检测下钻（按县 × 月的稳健 Z 分数）
-- #############################################################################

-- 分析：县域月度服务时长的偏离度
-- 说明：SQLite 无 STDDEV，用 sqrt(AVG(x²) − AVG(x)²) 计算总体标准差；
--       用中位数偏离度（相对全县历史中位数）作为稳健补充指标。
WITH county_month AS (
    SELECT f.county AS 县, d.year_month AS 年月, SUM(f.hours) AS 时长
    FROM fact_volunteer_service f
    JOIN dim_date d ON d.date_key = f.date_key
    WHERE f.volunteer_key <> -1 AND f.county <> 'UNKNOWN'
    GROUP BY f.county, d.year_month
),
baseline AS (
    SELECT 县,
           AVG(时长)                                                       AS 均值,
           SQRT(MAX(AVG(时长 * 时长) - AVG(时长) * AVG(时长), 0))          AS 标准差
    FROM county_month
    GROUP BY 县
)
SELECT
    cm.年月,
    cm.县,
    ROUND(cm.时长, 1)                                   AS 服务时长,
    ROUND(b.均值, 1)                                    AS 县历史均值,
    ROUND((cm.时长 - b.均值) / NULLIF(b.标准差, 0), 3)  AS Z分数,
    CASE
        WHEN ABS((cm.时长 - b.均值) / NULLIF(b.标准差, 0)) >= 2.0 THEN '异常'
        WHEN ABS((cm.时长 - b.均值) / NULLIF(b.标准差, 0)) >= 1.5 THEN '关注'
        ELSE '正常'
    END                                                 AS 判定
FROM county_month cm
JOIN baseline b ON b.县 = cm.县
ORDER BY ABS((cm.时长 - b.均值) / NULLIF(b.标准差, 0)) DESC
LIMIT 20;


-- 分析：数据错误特征——按月的签到修正率（修正率突增 = 录入错误而非业务波动）
SELECT
    d.year_month                                                    AS 年月,
    COUNT(*)                                                        AS 签到记录数,
    SUM(f.is_corrected)                                             AS 被修正记录数,
    ROUND(1.0 * SUM(f.is_corrected) / COUNT(*), 4)                  AS 修正率,
    ROUND(SUM(f.hours), 1)                                          AS 修正后时长
FROM fact_volunteer_service f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.year_month
ORDER BY 修正率 DESC
LIMIT 12;


-- 分析：县域 × 月的悬空外键率（数据接入质量）
SELECT
    f.county                                                        AS 县,
    d.year_month                                                    AS 年月,
    COUNT(*)                                                        AS 服务人次,
    SUM(f.has_orphan_key)                                           AS 悬空键记录数,
    ROUND(1.0 * SUM(f.has_orphan_key) / COUNT(*), 4)                AS 悬空键率
FROM fact_volunteer_service f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY f.county, d.year_month
HAVING 悬空键记录数 > 0
ORDER BY 悬空键率 DESC
LIMIT 15;


-- #############################################################################
-- 五、留存与集中度
-- #############################################################################

-- 分析：志愿者留存队列（注册年份 × 活动年份）
SELECT
    CAST(substr(v.join_date, 1, 4) AS INTEGER)          AS 注册年份,
    CAST(substr(CAST(f.date_key AS TEXT), 1, 4) AS INTEGER) AS 活动年份,
    COUNT(DISTINCT f.volunteer_key)                     AS 活跃人数,
    (SELECT COUNT(*) FROM dim_volunteer v2
      WHERE v2.volunteer_key <> -1
        AND substr(v2.join_date, 1, 4) = substr(v.join_date, 1, 4)) AS 队列规模,
    ROUND(1.0 * COUNT(DISTINCT f.volunteer_key)
          / (SELECT COUNT(*) FROM dim_volunteer v2
              WHERE v2.volunteer_key <> -1
                AND substr(v2.join_date, 1, 4) = substr(v.join_date, 1, 4)), 4) AS 留存率
FROM fact_volunteer_service f
JOIN dim_volunteer v ON v.volunteer_key = f.volunteer_key
WHERE f.volunteer_key <> -1
GROUP BY 注册年份, 活动年份
ORDER BY 注册年份, 活动年份;


-- 分析：帕累托曲线点位（前 1% / 5% / 10% / 20% / 50% 捐赠人贡献的金额占比）
WITH per_donor AS (
    SELECT donor_key, SUM(amount_cny) AS amt
    FROM fact_donation WHERE donor_key <> -1
    GROUP BY donor_key
),
ranked AS (
    SELECT amt,
           ROW_NUMBER() OVER (ORDER BY amt DESC) AS rn,
           COUNT(*) OVER ()                      AS n,
           SUM(amt) OVER ()                      AS total
    FROM per_donor
)
SELECT '前 1%' AS 捐赠人占比, CAST(CEIL(n * 0.01) AS INT) AS 人数,
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.01) THEN amt ELSE 0 END), 0) AS 金额,
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.01) THEN amt ELSE 0 END) / MAX(total), 4) AS 金额占比
FROM ranked
UNION ALL
SELECT '前 5%', CAST(CEIL(MAX(n) * 0.05) AS INT),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.05) THEN amt ELSE 0 END), 0),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.05) THEN amt ELSE 0 END) / MAX(total), 4)
FROM ranked
UNION ALL
SELECT '前 10%', CAST(CEIL(MAX(n) * 0.10) AS INT),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.10) THEN amt ELSE 0 END), 0),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.10) THEN amt ELSE 0 END) / MAX(total), 4)
FROM ranked
UNION ALL
SELECT '前 20%', CAST(CEIL(MAX(n) * 0.20) AS INT),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.20) THEN amt ELSE 0 END), 0),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.20) THEN amt ELSE 0 END) / MAX(total), 4)
FROM ranked
UNION ALL
SELECT '前 50%', CAST(CEIL(MAX(n) * 0.50) AS INT),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.50) THEN amt ELSE 0 END), 0),
       ROUND(SUM(CASE WHEN rn <= CEIL(n * 0.50) THEN amt ELSE 0 END) / MAX(total), 4)
FROM ranked;


-- 分析：前 15 大捐赠人画像
SELECT
    d.donor_name                                        AS 捐赠人,
    d.donor_type                                        AS 类型,
    d.county                                            AS 所在县,
    CASE WHEN d.is_recurring = 1 THEN '是' ELSE '否' END AS 是否月捐人,
    COUNT(*)                                            AS 捐赠笔数,
    ROUND(SUM(f.amount_cny), 0)                         AS 累计金额,
    ROUND(SUM(f.amount_cny) / (SELECT SUM(amount_cny) FROM fact_donation WHERE donor_key <> -1), 4) AS 金额占比,
    MIN(f.year_month)                                   AS 首次捐赠月,
    MAX(f.year_month)                                   AS 最近捐赠月
FROM fact_donation f
JOIN dim_donor d ON d.donor_key = f.donor_key
WHERE f.donor_key <> -1
GROUP BY f.donor_key
ORDER BY 累计金额 DESC
LIMIT 15;


-- #############################################################################
-- 六、地理与公平性
-- #############################################################################

-- 分析：县域资源配置均衡度（人口权重下的服务覆盖）
-- 说明：各县常住人口权重来自公开统计口径（香格里拉 52% / 维西 27% / 德钦 21%），
--       用于判断服务投入是否与人口规模匹配。
WITH by_county AS (
    SELECT p.county                                     AS 县,
           COUNT(DISTINCT p.project_key)                AS 项目数,
           ROUND(SUM(DISTINCT p.planned_budget_cny), 0) AS 计划预算,
           (SELECT ROUND(SUM(f.hours), 1) FROM fact_volunteer_service f
             WHERE f.county = p.county AND f.volunteer_key <> -1) AS 服务时长,
           (SELECT COUNT(DISTINCT s.beneficiary_key) FROM fact_beneficiary_service s
             WHERE s.county = p.county AND s.beneficiary_key <> -1) AS 被服务对象数
    FROM dim_project p
    WHERE p.project_key <> -1
    GROUP BY p.county
)
SELECT
    县,
    项目数,
    计划预算,
    服务时长,
    被服务对象数,
    ROUND(1.0 * 服务时长 / SUM(服务时长) OVER (), 4)        AS 时长份额,
    ROUND(1.0 * 计划预算 / SUM(计划预算) OVER (), 4)        AS 预算份额,
    CASE 县 WHEN '香格里拉市' THEN 0.52 WHEN '维西傈僳族自治县' THEN 0.27 ELSE 0.21 END AS 人口权重,
    ROUND(1.0 * 服务时长 / SUM(服务时长) OVER ()
          - CASE 县 WHEN '香格里拉市' THEN 0.52 WHEN '维西傈僳族自治县' THEN 0.27 ELSE 0.21 END, 4) AS 供给偏离度
FROM by_county
ORDER BY 服务时长 DESC;


-- 分析：双语能力的空间分布（民族地区服务可达性）
SELECT
    v.county                                            AS 县,
    COUNT(*)                                            AS 注册志愿者数,
    SUM(v.has_bilingual_skill)                          AS 双语志愿者数,
    ROUND(1.0 * SUM(v.has_bilingual_skill) / COUNT(*), 4) AS 双语占比,
    ROUND(AVG(v.tenure_days), 0)                        AS 平均在职天数
FROM dim_volunteer v
WHERE v.volunteer_key <> -1
GROUP BY v.county
ORDER BY 双语占比 DESC;


-- 分析：服务对象年龄结构（按项目类别）
SELECT
    p.category                                          AS 项目类别,
    b.age_group                                         AS 年龄段,
    COUNT(DISTINCT b.beneficiary_key)                   AS 对象数,
    ROUND(AVG(b.risk_score), 1)                         AS 平均风险分
FROM dim_beneficiary b
JOIN dim_project p ON p.project_id = b.project_id
WHERE b.beneficiary_key <> -1
GROUP BY p.category, b.age_group
ORDER BY p.category, 对象数 DESC;
