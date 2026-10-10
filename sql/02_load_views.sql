-- =============================================================================
-- 迪庆清风青年公益联合会 · 公益项目数据运营分析
-- 02_load_views.sql —— 星型模型便捷视图（分析层）
-- =============================================================================
--
-- 目的：把星型模型需要的多表 JOIN 固化成视图，让分析师可以直接
--       ``SELECT * FROM v_donation_enriched WHERE ...`` 而不必重复书写连接条件。
--
-- 视图清单：
--   v_volunteer_service_enriched   志愿服务签到 × 日期/志愿者/项目/地理
--   v_donation_enriched            现金捐赠 × 日期/捐赠人/项目
--   v_inkind_enriched              物资捐赠 × 日期/捐赠人/项目
--   v_beneficiary_service_enriched 受益服务 × 日期/受益对象/项目/地理
--   v_project_kpi                  项目级 KPI 宽表（一行 = 一个项目）
--
-- 约定：
--   * 全部使用 LEFT JOIN：-1 UNKNOWN 维度成员本身也是维度表中的真实行，
--     因此可以正常连接；若清洗后仍残留悬空外键，LEFT JOIN 保证事实行不丢失。
--   * 维度表代理键均为唯一主键，因此 JOIN 不会造成行数放大。
--   * 视图可重复执行：先 DROP VIEW IF EXISTS 再 CREATE VIEW。
--   * 时长为小时，金额为元（CNY）。布尔字段为 INTEGER 0/1。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 视图 1：v_volunteer_service_enriched —— 志愿服务签到明细宽表
-- 粒度：一行 = 一条签到记录（与 fact_volunteer_service 一致）
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_volunteer_service_enriched;
CREATE VIEW v_volunteer_service_enriched AS
SELECT
    -- 事实
    s.service_key,
    s.hours,
    s.volunteer_value_cny,
    s.service_role,
    s.checkin_method,
    s.is_long_shift,
    s.is_corrected,
    s.has_orphan_key,
    -- 日期维
    d.date_key,
    d.full_date,
    d.year,
    d.quarter,
    d.month,
    d.year_month,
    d.year_quarter,
    d.week_of_year,
    d.day_name,
    d.is_weekend,
    d.season,
    d.month_index,
    -- 志愿者维
    s.volunteer_key,
    v.volunteer_id,
    v.volunteer_name,
    v.gender,
    v.age,
    v.age_group,
    v.ethnicity,
    v.occupation,
    v.education,
    v.join_year,
    v.status        AS volunteer_status,
    v.is_active     AS volunteer_is_active,
    v.skill_count,
    v.has_bilingual_skill,
    -- 项目维
    s.project_key,
    p.project_id,
    p.project_name,
    p.category      AS project_category,
    p.category_code AS project_category_code,
    p.status        AS project_status,
    p.risk_level    AS project_risk_level,
    -- 地理维
    s.geo_key,
    g.county,
    g.township,
    g.county_short,
    g.is_state_capital,
    g.is_tibetan_area,
    g.geo_code
FROM fact_volunteer_service AS s
LEFT JOIN dim_date      AS d ON d.date_key      = s.date_key
LEFT JOIN dim_volunteer AS v ON v.volunteer_key = s.volunteer_key
LEFT JOIN dim_project   AS p ON p.project_key   = s.project_key
LEFT JOIN dim_geography AS g ON g.geo_key       = s.geo_key;

-- -----------------------------------------------------------------------------
-- 视图 2：v_donation_enriched —— 现金捐赠明细宽表
-- 粒度：一行 = 一笔现金捐赠（与 fact_donation 一致）
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_donation_enriched;
CREATE VIEW v_donation_enriched AS
SELECT
    -- 事实
    f.donation_key,
    f.amount_cny,
    f.amount_band,
    f.is_major_gift,
    f.channel,
    f.designation,
    f.receipt_issued,
    f.is_corrected,
    f.has_orphan_key,
    -- 日期维
    d.date_key,
    d.full_date,
    d.year,
    d.quarter,
    d.month,
    d.year_month,
    d.year_quarter,
    d.season,
    d.month_index,
    -- 捐赠人维
    f.donor_key,
    o.donor_id,
    o.donor_name,
    COALESCE(NULLIF(o.donor_type, '未知'), NULLIF(o.donor_type, 'UNKNOWN'), '未知/未披露') AS donor_type,
    o.county        AS donor_county,
    o.is_recurring,
    o.is_institutional,
    o.is_local,
    o.first_donation_year,
    o.relationship_days,
    -- 项目维
    f.project_key,
    p.project_id,
    p.project_name,
    p.category      AS project_category,
    p.county        AS project_county,
    p.status        AS project_status
FROM fact_donation AS f
LEFT JOIN dim_date    AS d ON d.date_key    = f.date_key
LEFT JOIN dim_donor   AS o ON o.donor_key   = f.donor_key
LEFT JOIN dim_project AS p ON p.project_key = f.project_key;

-- -----------------------------------------------------------------------------
-- 视图 3：v_inkind_enriched —— 物资捐赠明细宽表
-- 粒度：一行 = 一笔物资条目（与 fact_in_kind 一致）
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_inkind_enriched;
CREATE VIEW v_inkind_enriched AS
SELECT
    -- 事实
    k.inkind_key,
    k.item_name,
    k.item_category,
    k.quantity,
    k.unit,
    k.est_unit_value_cny,
    k.est_total_value_cny,
    k.value_band,
    k.warehouse,
    k.is_corrected,
    -- 日期维
    d.date_key,
    d.full_date,
    d.year,
    d.quarter,
    d.year_month,
    d.year_quarter,
    d.month_index,
    -- 捐赠人维
    k.donor_key,
    o.donor_id,
    o.donor_name,
    COALESCE(NULLIF(o.donor_type, '未知'), NULLIF(o.donor_type, 'UNKNOWN'), '未知/未披露') AS donor_type,
    -- 项目维
    k.project_key,
    p.project_id,
    p.project_name,
    p.category      AS project_category,
    p.county        AS project_county
FROM fact_in_kind AS k
LEFT JOIN dim_date    AS d ON d.date_key    = k.date_key
LEFT JOIN dim_donor   AS o ON o.donor_key   = k.donor_key
LEFT JOIN dim_project AS p ON p.project_key = k.project_key;

-- -----------------------------------------------------------------------------
-- 视图 4：v_beneficiary_service_enriched —— 受益服务明细宽表
-- 粒度：一行 = 一条受益服务记录（与 fact_beneficiary_service 一致）
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_beneficiary_service_enriched;
CREATE VIEW v_beneficiary_service_enriched AS
SELECT
    -- 事实
    b.service_key,
    b.service_type,
    b.duration_hours,
    b.satisfaction_score,
    b.is_satisfied,
    b.is_dissatisfied,
    b.followup_needed,
    b.is_corrected,
    b.has_orphan_key,
    -- 日期维
    d.date_key,
    d.full_date,
    d.year,
    d.quarter,
    d.month,
    d.year_month,
    d.year_quarter,
    d.season,
    d.month_index,
    -- 受益对象维
    b.beneficiary_key,
    e.beneficiary_id,
    e.age,
    e.age_group,
    e.gender,
    e.ethnicity,
    e.household_type,
    e.risk_score,
    e.risk_band,
    e.is_minor,
    e.is_elderly,
    e.enroll_year,
    -- 项目维
    b.project_key,
    p.project_id,
    p.project_name,
    p.category      AS project_category,
    p.category_code AS project_category_code,
    p.status        AS project_status,
    -- 地理维
    b.geo_key,
    g.county,
    g.township,
    g.county_short,
    g.is_state_capital,
    g.is_tibetan_area
FROM fact_beneficiary_service AS b
LEFT JOIN dim_date        AS d ON d.date_key        = b.date_key
LEFT JOIN dim_beneficiary AS e ON e.beneficiary_key = b.beneficiary_key
LEFT JOIN dim_project     AS p ON p.project_key     = b.project_key
LEFT JOIN dim_geography   AS g ON g.geo_key         = b.geo_key;

-- -----------------------------------------------------------------------------
-- 视图 5：v_project_kpi —— 项目级 KPI 宽表
-- 粒度：一行 = 一个在册项目（含 -1 UNKNOWN 成员）
-- 口径：各事实子查询先按 project_key 聚合再 LEFT JOIN，避免多事实表连接造成行放大；
--       子查询内已排除代理键为 -1 的未知成员。
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS v_project_kpi;
CREATE VIEW v_project_kpi AS
SELECT
    p.project_key,
    p.project_id,
    p.project_name,
    p.category,
    p.category_code,
    p.county,
    p.township,
    p.manager_staff_id,
    p.status,
    p.risk_level,
    p.funding_source,
    p.planned_budget_cny,
    p.target_beneficiaries,
    p.planned_start_date,
    p.planned_end_date,
    p.start_year_month,
    p.budget_band,
    -- 志愿服务
    COALESCE(sv.n_service_records, 0)  AS n_service_records,
    COALESCE(sv.total_hours, 0)        AS total_service_hours,
    COALESCE(sv.n_volunteers, 0)       AS n_volunteers,
    COALESCE(sv.avg_hours_per_shift, 0) AS avg_hours_per_shift,
    -- 受益服务
    COALESCE(bs.n_beneficiary_services, 0) AS n_beneficiary_services,
    COALESCE(bs.n_beneficiaries, 0)        AS n_beneficiaries,
    COALESCE(bs.total_service_hours, 0)    AS total_beneficiary_hours,
    bs.avg_satisfaction,
    bs.satisfied_rate,
    -- 现金捐赠
    COALESCE(dn.total_donation_cny, 0) AS total_donation_cny,
    COALESCE(dn.n_donations, 0)        AS n_donations,
    COALESCE(dn.n_donors, 0)           AS n_donors,
    -- 物资捐赠
    COALESCE(ik.total_inkind_value_cny, 0) AS total_inkind_value_cny,
    -- 预算执行
    COALESCE(bg.planned_cny, 0) AS planned_cny,
    COALESCE(bg.actual_cny, 0)  AS actual_cny,
    CASE WHEN COALESCE(bg.planned_cny, 0) > 0
         THEN bg.actual_cny / bg.planned_cny END AS budget_execution_rate
FROM dim_project AS p
LEFT JOIN (
    SELECT project_key,
           COUNT(*)                  AS n_service_records,
           SUM(hours)                AS total_hours,
           COUNT(DISTINCT volunteer_key) AS n_volunteers,
           AVG(hours)                AS avg_hours_per_shift
    FROM fact_volunteer_service
    WHERE project_key <> -1
    GROUP BY project_key
) AS sv ON sv.project_key = p.project_key
LEFT JOIN (
    SELECT project_key,
           COUNT(*)                        AS n_beneficiary_services,
           COUNT(DISTINCT beneficiary_key) AS n_beneficiaries,
           SUM(duration_hours)             AS total_service_hours,
           AVG(satisfaction_score)         AS avg_satisfaction,
           AVG(CASE WHEN satisfaction_score >= 4 THEN 1.0 ELSE 0.0 END) AS satisfied_rate
    FROM fact_beneficiary_service
    WHERE project_key <> -1
    GROUP BY project_key
) AS bs ON bs.project_key = p.project_key
LEFT JOIN (
    SELECT project_key,
           SUM(amount_cny)         AS total_donation_cny,
           COUNT(*)                AS n_donations,
           COUNT(DISTINCT donor_key) AS n_donors
    FROM fact_donation
    WHERE project_key <> -1
    GROUP BY project_key
) AS dn ON dn.project_key = p.project_key
LEFT JOIN (
    SELECT project_key,
           SUM(est_total_value_cny) AS total_inkind_value_cny
    FROM fact_in_kind
    WHERE project_key <> -1
    GROUP BY project_key
) AS ik ON ik.project_key = p.project_key
LEFT JOIN (
    SELECT project_key,
           SUM(planned_cny) AS planned_cny,
           SUM(actual_cny)  AS actual_cny
    FROM fact_project_monthly
    WHERE project_key <> -1
    GROUP BY project_key
) AS bg ON bg.project_key = p.project_key;
