-- =============================================================================
-- 迪庆清风青年公益联合会 · 公益项目数据运营分析
-- 01_schema.sql —— 星型模型建表脚本（DDL）
-- =============================================================================
--
-- 【一、星型模型总览】
-- 本仓库采用经典星型模型（Star Schema），由 6 张维度表 + 5 张事实表构成：
--
--   维度表（dim_*）                         事实表（fact_*）
--   ├─ dim_date        日期维（角色维）      ├─ fact_volunteer_service   志愿服务签到
--   ├─ dim_project     项目维                ├─ fact_donation            现金捐赠
--   ├─ dim_volunteer   志愿者维              ├─ fact_in_kind             物资捐赠
--   ├─ dim_beneficiary 受益对象维            ├─ fact_beneficiary_service 受益服务记录
--   ├─ dim_donor       捐赠人维              └─ fact_project_monthly     项目月度预算执行
--   └─ dim_geography   地理维（县/乡镇）
--
-- 设计约定：
--   * 维度表使用**代理键**（surrogate key，列名形如 ``*_key``，整型）作为主键，
--     业务键（``*_id`` 文本）另立唯一性索引，便于缓慢变化维扩展。
--   * 事实表同时冗余保存业务键（如 fact_donation.donor_id）与代理键（donor_key）：
--     代理键用于与维度精确 JOIN，业务键用于与原始 CSV 对账及悬空外键诊断。
--   * ``date_key`` 统一为整数日期键 YYYYMMDD（如 20260601），业务日期以 TEXT
--     'YYYY-MM-DD' 存放于维度表；``year_month`` 为 TEXT 'YYYY-MM'。
--   * 布尔字段一律以 INTEGER 0/1 存储（SQLite 无原生 BOOLEAN）。
--   * 目标方言为 SQLite 3；类型选择（INTEGER / REAL / TEXT）在 DuckDB 与
--     PostgreSQL 下均可直接建表（PostgreSQL 侧建议将 REAL 映射为 numeric/double
--     precision，其余语法无需改动）。
--
-- 【二、-1 UNKNOWN 维度成员约定】
--   每张维度表都预置一行代理键为 **-1** 的 UNKNOWN（未知/未匹配）成员：
--       *_id = 'UNKNOWN'，文本属性 = 'UNKNOWN'，数值属性为 NULL。
--   当事实表的外键在源系统中缺失、无法匹配到具体实体时，清洗层将其改写为 -1，
--   而不是丢弃记录或写入 NULL。这样做的好处：
--       1) 保留行数，指标口径不因脏数据而静默缩水；
--       2) JOIN 永远不产生 NULL 外键，避免 COUNT(*) / SUM() 口径歧义；
--       3) 可显式量化"未知占比"这一数据质量信号。
--   ⚠ 分析口径：KPI 与交叉分析**默认排除** ``*_key = -1`` 的行（仅统计可归因
--     到具体实体的记录），并在每条查询注释中声明。需要观测未知占比时，
--     显式将 -1 纳入即可。
--
-- 【三、各表粒度（grain）】
--   dim_date                 一行 = 一个自然日（覆盖 2024-01-01 起至数据截止日）
--   dim_project              一行 = 一个在册公益项目
--   dim_volunteer            一行 = 一名注册志愿者
--   dim_beneficiary          一行 = 一名建档受益对象
--   dim_donor                一行 = 一个捐赠人（个人或机构）
--   dim_geography            一行 = 一个县×乡镇组合
--   fact_volunteer_service   一行 = 一条志愿服务签到记录（志愿者×项目×日期×角色）
--   fact_donation            一行 = 一笔现金捐赠
--   fact_in_kind             一行 = 一笔物资捐赠（按物资条目计）
--   fact_beneficiary_service 一行 = 一条受益服务记录（对象×项目×日期×服务类型）
--   fact_project_monthly     一行 = 一个项目×年月×预算科目 的预算执行行
--
-- 执行顺序：本脚本可重复执行（先 DROP 后 CREATE），执行后表为空表，
-- 数据由 ``src/qingfeng/warehouse.py``（pandas ``to_sql``）或 ETL 流程装载。
-- =============================================================================

PRAGMA foreign_keys = OFF;

-- -----------------------------------------------------------------------------
-- 维度表 1/6：dim_date —— 日期维
-- 粒度：一行 = 一个自然日
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_date;
CREATE TABLE dim_date (
    date_key     INTEGER PRIMARY KEY,  -- 日期代理键，YYYYMMDD
    full_date    TEXT,                 -- 完整日期 '2026-06-01'
    year         INTEGER,              -- 年
    quarter      INTEGER,              -- 季度 1-4
    month        INTEGER,              -- 月 1-12
    month_name   TEXT,                 -- 月份中文名，如 '6月'
    year_month   TEXT,                 -- 年月 '2026-06'
    year_quarter TEXT,                 -- 年季 '2026Q2'
    week_of_year INTEGER,              -- 年内周序号
    day_of_week  INTEGER,              -- 星期（0=周一 … 6=周日）
    day_name     TEXT,                 -- 星期中文名
    is_weekend   INTEGER,              -- 是否周末 0/1
    day_of_year  INTEGER,              -- 年内第几天
    is_month_end INTEGER,              -- 是否月末 0/1
    season       TEXT,                 -- 季节
    month_index  INTEGER               -- 单调月序号（跨年连续，用于序列排序）
);

-- -----------------------------------------------------------------------------
-- 维度表 2/6：dim_project —— 公益项目维
-- 粒度：一行 = 一个在册公益项目（含 -1 UNKNOWN 成员）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_project;
CREATE TABLE dim_project (
    project_key          INTEGER PRIMARY KEY,  -- 项目代理键
    project_id           TEXT,                 -- 项目业务编号
    project_name         TEXT,                 -- 项目名称
    category             TEXT,                 -- 项目类别（助学支教/助老敬老/…）
    category_code        TEXT,                 -- 类别编码
    county               TEXT,                 -- 实施县市
    township             TEXT,                 -- 实施乡镇
    manager_staff_id     TEXT,                 -- 项目负责人（工作人员编号）
    status               TEXT,                 -- 项目状态（筹备中/进行中/已完成/已结项/暂停）
    funding_source       TEXT,                 -- 资金来源
    risk_level           TEXT,                 -- 风险等级
    planned_budget_cny   REAL,                 -- 计划预算（元）
    target_beneficiaries REAL,                 -- 目标受益人数
    planned_start_date   TEXT,                 -- 计划开始日期
    planned_end_date     TEXT,                 -- 计划结束日期
    actual_end_date      TEXT,                 -- 实际结束日期
    duration_days        REAL,                 -- 计划工期（天）
    actual_duration_days REAL,                 -- 实际工期（天）
    plan_delay_days      REAL,                 -- 延期天数（正=延期）
    start_year           REAL,                 -- 启动年份
    start_year_month     TEXT,                 -- 启动年月 'YYYY-MM'
    is_completed         REAL,                 -- 是否已完工 0/1
    budget_per_beneficiary REAL,               -- 人均预算（元/人）
    budget_band          TEXT,                 -- 预算区间分档
    is_cross_year        REAL                  -- 是否跨年项目 0/1
);

-- -----------------------------------------------------------------------------
-- 维度表 3/6：dim_volunteer —— 志愿者维
-- 粒度：一行 = 一名注册志愿者
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_volunteer;
CREATE TABLE dim_volunteer (
    volunteer_key      INTEGER PRIMARY KEY,  -- 志愿者代理键
    volunteer_id       TEXT,                 -- 志愿者业务编号
    volunteer_name     TEXT,                 -- 姓名
    gender             TEXT,                 -- 性别
    birth_year         REAL,                 -- 出生年份
    age                REAL,                 -- 年龄
    age_group          TEXT,                 -- 年龄段
    ethnicity          TEXT,                 -- 民族
    county             TEXT,                 -- 所属县市
    township           TEXT,                 -- 所属乡镇
    occupation         TEXT,                 -- 职业
    education          TEXT,                 -- 学历
    join_date          TEXT,                 -- 注册日期
    join_year          REAL,                 -- 注册年份（留存队列用）
    tenure_days        REAL,                 -- 注册至数据截止日的在册天数
    skills             TEXT,                 -- 技能标签（分号分隔）
    skill_count        REAL,                 -- 技能标签数量
    has_bilingual_skill REAL,                -- 是否具备双语能力 0/1
    status             TEXT,                 -- 状态（活跃/沉睡/退出/新增）
    is_active          REAL                  -- 是否活跃 0/1
);

-- -----------------------------------------------------------------------------
-- 维度表 4/6：dim_beneficiary —— 受益对象维
-- 粒度：一行 = 一名建档受益对象（服务对象）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_beneficiary;
CREATE TABLE dim_beneficiary (
    beneficiary_key  INTEGER PRIMARY KEY,  -- 受益对象代理键
    beneficiary_id   TEXT,                 -- 对象业务编号
    project_id       TEXT,                 -- 建档关联项目编号
    project_name     TEXT,                 -- 建档关联项目名称
    project_category TEXT,                 -- 关联项目类别
    county           TEXT,                 -- 所属县市
    township         TEXT,                 -- 所属乡镇
    village          TEXT,                 -- 所属村/社区
    age              REAL,                 -- 年龄
    age_group        TEXT,                 -- 年龄段
    gender           TEXT,                 -- 性别
    ethnicity        TEXT,                 -- 民族
    household_type   TEXT,                 -- 家庭类型（低保户/脱贫户/…）
    risk_score       REAL,                 -- 风险评分
    risk_band        TEXT,                 -- 风险分档
    is_minor         REAL,                 -- 是否未成年 0/1
    is_elderly       REAL,                 -- 是否老年 0/1
    enroll_date      TEXT,                 -- 建档日期
    enroll_year      REAL                  -- 建档年份
);

-- -----------------------------------------------------------------------------
-- 维度表 5/6：dim_donor —— 捐赠人维
-- 粒度：一行 = 一个捐赠人（个人或机构）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_donor;
CREATE TABLE dim_donor (
    donor_key           INTEGER PRIMARY KEY,  -- 捐赠人代理键
    donor_id            TEXT,                 -- 捐赠人业务编号
    donor_name          TEXT,                 -- 名称
    donor_type          TEXT,                 -- 类型（个人/企业/基金会/政府/社会组织）
    county              TEXT,                 -- 所属地区（或"州外"）
    is_recurring        REAL,                 -- 是否连续捐赠 0/1
    is_institutional    REAL,                 -- 是否机构捐赠人 0/1
    is_local            REAL,                 -- 是否本地捐赠人 0/1
    first_donation_date TEXT,                 -- 首次捐赠日期
    first_donation_year REAL,                 -- 首次捐赠年份
    relationship_days   REAL,                 -- 关系存续天数
    contact_masked      TEXT                  -- 脱敏联系方式
);

-- -----------------------------------------------------------------------------
-- 维度表 6/6：dim_geography —— 地理维
-- 粒度：一行 = 一个县×乡镇组合
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS dim_geography;
CREATE TABLE dim_geography (
    geo_key          INTEGER PRIMARY KEY,  -- 地理代理键
    county           TEXT,                 -- 县市（香格里拉市/维西傈僳族自治县/德钦县）
    township         TEXT,                 -- 乡镇
    county_short     TEXT,                 -- 县市简称
    is_state_capital REAL,                 -- 是否州府所在地 0/1
    is_tibetan_area  REAL,                 -- 是否藏区 0/1
    geo_code         TEXT                  -- 行政区划代码
);

-- -----------------------------------------------------------------------------
-- 事实表 1/5：fact_volunteer_service —— 志愿服务签到事实
-- 粒度：一行 = 一条签到记录（志愿者×项目×日期×服务角色）
-- 度量：hours（时长）、volunteer_value_cny（经济价值折算）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS fact_volunteer_service;
CREATE TABLE fact_volunteer_service (
    service_key         TEXT PRIMARY KEY,  -- 签到记录业务主键
    date_key            INTEGER,           -- → dim_date
    volunteer_key       INTEGER,           -- → dim_volunteer
    project_key         INTEGER,           -- → dim_project
    geo_key             INTEGER,           -- → dim_geography
    volunteer_id        TEXT,              -- 志愿者业务编号（冗余，便于对账）
    project_id          TEXT,              -- 项目业务编号（冗余，便于对账）
    county              TEXT,              -- 服务发生县市（冗余）
    service_role        TEXT,              -- 服务角色
    checkin_method      TEXT,              -- 签到方式
    hours               REAL,              -- 服务时长（小时）
    volunteer_value_cny REAL,              -- 经济价值折算（元）
    is_long_shift       INTEGER,           -- 是否长时段班次 0/1
    is_corrected        INTEGER,           -- 是否经清洗修正 0/1
    has_orphan_key      INTEGER            -- 原始外键是否悬空 0/1
);

-- -----------------------------------------------------------------------------
-- 事实表 2/5：fact_donation —— 现金捐赠事实
-- 粒度：一行 = 一笔现金捐赠
-- 度量：amount_cny（捐赠金额）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS fact_donation;
CREATE TABLE fact_donation (
    donation_key   TEXT PRIMARY KEY,  -- 捐赠记录业务主键
    date_key       INTEGER,           -- → dim_date
    donor_key      INTEGER,           -- → dim_donor
    project_key    INTEGER,           -- → dim_project
    donor_id       TEXT,              -- 捐赠人业务编号（冗余）
    project_id     TEXT,              -- 项目业务编号（冗余）
    amount_cny     REAL,              -- 捐赠金额（元）
    amount_band    TEXT,              -- 金额分档
    is_major_gift  INTEGER,           -- 是否大额捐赠 0/1
    channel        TEXT,              -- 捐赠渠道
    designation    TEXT,              -- 是否指定用途
    receipt_issued INTEGER,           -- 是否开具票据 0/1
    year_month     TEXT,              -- 年月 'YYYY-MM'（冗余，便于按月汇总）
    is_corrected   INTEGER,           -- 是否经清洗修正 0/1
    has_orphan_key INTEGER            -- 原始外键是否悬空 0/1
);

-- -----------------------------------------------------------------------------
-- 事实表 3/5：fact_in_kind —— 物资捐赠事实
-- 粒度：一行 = 一笔物资捐赠中的一个物资条目
-- 度量：quantity（数量）、est_total_value_cny（折价总额）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS fact_in_kind;
CREATE TABLE fact_in_kind (
    inkind_key          TEXT PRIMARY KEY,  -- 物资捐赠业务主键
    date_key            INTEGER,           -- → dim_date
    donor_key           INTEGER,           -- → dim_donor
    project_key         INTEGER,           -- → dim_project
    donor_id            TEXT,              -- 捐赠人业务编号（冗余）
    project_id          TEXT,              -- 项目业务编号（冗余）
    item_name           TEXT,              -- 物资名称
    item_category       TEXT,              -- 物资类别
    quantity            REAL,              -- 数量
    unit                TEXT,              -- 计量单位
    est_unit_value_cny  REAL,              -- 估计单价（元）
    est_total_value_cny REAL,              -- 估计折价总额（元）
    value_band          TEXT,              -- 价值分档
    warehouse           TEXT,              -- 入库仓库
    year_month          TEXT,              -- 年月 'YYYY-MM'（冗余）
    is_corrected        INTEGER            -- 是否经清洗修正 0/1
);

-- -----------------------------------------------------------------------------
-- 事实表 4/5：fact_beneficiary_service —— 受益服务事实
-- 粒度：一行 = 一条受益服务记录（对象×项目×日期×服务类型）
-- 度量：duration_hours（服务时长）、satisfaction_score（满意度 1-5）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS fact_beneficiary_service;
CREATE TABLE fact_beneficiary_service (
    service_key          TEXT PRIMARY KEY,  -- 服务记录业务主键
    date_key             INTEGER,           -- → dim_date
    beneficiary_key      INTEGER,           -- → dim_beneficiary
    project_key          INTEGER,           -- → dim_project
    geo_key              INTEGER,           -- → dim_geography
    beneficiary_id       TEXT,              -- 对象业务编号（冗余）
    project_id           TEXT,              -- 项目业务编号（冗余）
    service_type         TEXT,              -- 服务类型
    duration_hours       REAL,              -- 服务时长（小时）
    satisfaction_score   REAL,              -- 满意度评分（1-5，可能缺失）
    is_satisfied         INTEGER,           -- 是否满意（>=4 分）0/1
    is_dissatisfied      INTEGER,           -- 是否不满意（<=2 分）0/1
    followup_needed      INTEGER,           -- 是否需要跟进 0/1
    county               TEXT,              -- 服务发生县市（冗余）
    is_corrected         INTEGER,           -- 是否经清洗修正 0/1
    has_orphan_key       INTEGER            -- 原始外键是否悬空 0/1
);

-- -----------------------------------------------------------------------------
-- 事实表 5/5：fact_project_monthly —— 项目月度预算执行事实（无事实事实表/累计快照）
-- 粒度：一行 = 一个项目 × 年月 × 预算科目
-- 度量：planned_cny（预算）、actual_cny（实际支出）、execution_rate（执行率）
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS fact_project_monthly;
CREATE TABLE fact_project_monthly (
    year_month      TEXT,     -- 年月 'YYYY-MM'（复合主键 1/3）
    date_key        INTEGER,  -- → dim_date（取该月 1 日）
    project_key     INTEGER,  -- → dim_project
    project_id      TEXT,     -- 项目业务编号（复合主键 2/3）
    year            INTEGER,  -- 年
    month_num       INTEGER,  -- 月
    budget_category TEXT,     -- 预算科目（复合主键 3/3）
    planned_cny     REAL,     -- 预算金额（元）
    actual_cny      REAL,     -- 实际支出（元）
    variance_cny    REAL,     -- 差异 = 实际 - 预算
    execution_rate  REAL,     -- 执行率 = 实际 / 预算
    is_overspend    INTEGER,  -- 是否超支 0/1
    spend_share     REAL,     -- 该科目占项目当月支出比重
    is_corrected    INTEGER,  -- 是否经清洗修正 0/1
    PRIMARY KEY (year_month, project_id, budget_category)
);

-- =============================================================================
-- 索引：覆盖全部外键列与主要日期键
-- 说明：SQLite 会为 PRIMARY KEY 自动建索引；此处仅对外键/日期/常用分组列建索引，
--       以保证事实表与维度表 JOIN、按月汇总、按项目下钻的查询性能。
-- =============================================================================

-- dim_date：日期维主要检索键
CREATE INDEX idx_dim_date_year_month  ON dim_date (year_month);
CREATE INDEX idx_dim_date_full_date   ON dim_date (full_date);
CREATE INDEX idx_dim_date_year        ON dim_date (year);
CREATE INDEX idx_dim_date_month_index ON dim_date (month_index);

-- dim_project：业务键 + 常用分组列
CREATE INDEX idx_dim_project_project_id ON dim_project (project_id);
CREATE INDEX idx_dim_project_category   ON dim_project (category);
CREATE INDEX idx_dim_project_county     ON dim_project (county);
CREATE INDEX idx_dim_project_status     ON dim_project (status);

-- dim_volunteer：业务键 + 常用分组列
CREATE INDEX idx_dim_volunteer_volunteer_id ON dim_volunteer (volunteer_id);
CREATE INDEX idx_dim_volunteer_county       ON dim_volunteer (county);
CREATE INDEX idx_dim_volunteer_ethnicity    ON dim_volunteer (ethnicity);
CREATE INDEX idx_dim_volunteer_join_year    ON dim_volunteer (join_year);
CREATE INDEX idx_dim_volunteer_is_active    ON dim_volunteer (is_active);

-- dim_beneficiary：业务键 + 关联项目 + 常用分组列
CREATE INDEX idx_dim_beneficiary_beneficiary_id ON dim_beneficiary (beneficiary_id);
CREATE INDEX idx_dim_beneficiary_project_id     ON dim_beneficiary (project_id);
CREATE INDEX idx_dim_beneficiary_county         ON dim_beneficiary (county);

-- dim_donor：业务键 + 类型
CREATE INDEX idx_dim_donor_donor_id   ON dim_donor (donor_id);
CREATE INDEX idx_dim_donor_donor_type ON dim_donor (donor_type);

-- dim_geography：县×乡镇
CREATE INDEX idx_dim_geography_county          ON dim_geography (county);
CREATE INDEX idx_dim_geography_county_township ON dim_geography (county, township);

-- fact_volunteer_service：全部外键 + 日期键
CREATE INDEX idx_fvs_date_key      ON fact_volunteer_service (date_key);
CREATE INDEX idx_fvs_volunteer_key ON fact_volunteer_service (volunteer_key);
CREATE INDEX idx_fvs_project_key   ON fact_volunteer_service (project_key);
CREATE INDEX idx_fvs_geo_key       ON fact_volunteer_service (geo_key);
CREATE INDEX idx_fvs_volunteer_id  ON fact_volunteer_service (volunteer_id);
CREATE INDEX idx_fvs_project_id    ON fact_volunteer_service (project_id);

-- fact_donation：全部外键 + 日期键 / 年月键
CREATE INDEX idx_fd_date_key    ON fact_donation (date_key);
CREATE INDEX idx_fd_donor_key   ON fact_donation (donor_key);
CREATE INDEX idx_fd_project_key ON fact_donation (project_key);
CREATE INDEX idx_fd_year_month  ON fact_donation (year_month);
CREATE INDEX idx_fd_donor_id    ON fact_donation (donor_id);
CREATE INDEX idx_fd_channel     ON fact_donation (channel);

-- fact_in_kind：全部外键 + 日期键 / 年月键
CREATE INDEX idx_fik_date_key    ON fact_in_kind (date_key);
CREATE INDEX idx_fik_donor_key   ON fact_in_kind (donor_key);
CREATE INDEX idx_fik_project_key ON fact_in_kind (project_key);
CREATE INDEX idx_fik_year_month  ON fact_in_kind (year_month);

-- fact_beneficiary_service：全部外键 + 日期键
CREATE INDEX idx_fbs_date_key        ON fact_beneficiary_service (date_key);
CREATE INDEX idx_fbs_beneficiary_key ON fact_beneficiary_service (beneficiary_key);
CREATE INDEX idx_fbs_project_key     ON fact_beneficiary_service (project_key);
CREATE INDEX idx_fbs_geo_key         ON fact_beneficiary_service (geo_key);
CREATE INDEX idx_fbs_beneficiary_id  ON fact_beneficiary_service (beneficiary_id);

-- fact_project_monthly：外键 + 年月键
CREATE INDEX idx_fpm_date_key       ON fact_project_monthly (date_key);
CREATE INDEX idx_fpm_project_key    ON fact_project_monthly (project_key);
CREATE INDEX idx_fpm_project_id     ON fact_project_monthly (project_id);
CREATE INDEX idx_fpm_year_month     ON fact_project_monthly (year_month);
CREATE INDEX idx_fpm_budget_category ON fact_project_monthly (budget_category);
