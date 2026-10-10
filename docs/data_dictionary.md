# 数据字典

> 本文件由 `scripts/build_docs.py` 从**实际生成的数据**自动导出，请勿手工修改（改字段请改 `src/qingfeng/tables.py` 与生成器脚本后重跑 `make pipeline`）。

> 生成时间：2026-10-10 22:56 ｜ 数据窗口：2024-01-01 ~ 2026-06-30 ｜ 随机种子：20260601


## 一、数据源总览

| 数据表 | 中文名称 | 来源系统 | 粒度 | 主键 |
|---|---|---|---|---|
| `projects` | 公益项目台账 | 项目组 Excel 台账（人工维护） | 一行 = 一个在册公益项目 | `project_id` |
| `staff` | 机构工作人员 | 机构人事花名册 | 一行 = 一名专职/兼职工作人员 | `staff_id` |
| `volunteers` | 志愿者花名册 | 志愿服务信息系统导出 | 一行 = 一名注册志愿者 | `volunteer_id` |
| `volunteer_shifts` | 志愿服务签到记录 | 签到小程序导出 + 纸质签到表补录 | 一行 = 一名志愿者在一个项目上的一次服务 | `shift_id` |
| `donors` | 捐赠人档案 | 捐赠平台 + 财务台账 | 一行 = 一个捐赠人（个人或机构） | `donor_id` |
| `donations` | 现金捐赠流水 | 捐赠平台导出 + 银行流水 | 一行 = 一笔现金捐赠 | `donation_id` |
| `in_kind_donations` | 物资捐赠入库单 | 仓库入库单（纸质转录） | 一行 = 一笔物资捐赠入库 | `inkind_id` |
| `beneficiaries` | 服务对象档案 | 社工入户走访登记表 | 一行 = 一名建档服务对象 | `beneficiary_id` |
| `beneficiary_services` | 服务提供记录 | 服务工单系统 + 社工台账 | 一行 = 对一名服务对象的一次服务 | `service_id` |
| `monthly_budget_execution` | 项目月度预算执行 | 财务月度报表 | 一行 = 一个项目在一个月份上一个预算科目的计划/实际支出 | `—` |
| `satisfaction_survey_raw` | 满意度问卷（原始回收） | 问卷星导出 | 一行 = 一份问卷回复（含大量未结构化的自由文本） | `response_id` |

## 二、星型模型

| 表名 | 类型 | 行数 | 列数 | 说明 |
|---|---|---|---|---|
| `dim_date` | 维度表 | 912 | 16 | 一行 = 一个自然日（日期维度，覆盖全部事实表的日期范围） |
| `dim_project` | 维度表 | 53 | 25 |  |
| `dim_volunteer` | 维度表 | 3,601 | 20 |  |
| `dim_beneficiary` | 维度表 | 5,141 | 19 |  |
| `dim_donor` | 维度表 | 5,201 | 12 |  |
| `dim_geography` | 维度表 | 23 | 7 | 一行 = 一个「县 + 乡镇」组合（复合业务键 geo_code） |
| `fact_volunteer_service` | 事实表 | 12,888 | 15 |  |
| `fact_donation` | 事实表 | 19,988 | 15 |  |
| `fact_in_kind` | 事实表 | 612 | 16 |  |
| `fact_beneficiary_service` | 事实表 | 16,137 | 16 |  |
| `fact_project_monthly` | 事实表 | 3,762 | 14 |  |

> **UNKNOWN 成员约定**：每张维度表都有一行代理键为 `-1` 的记录，用于承载源系统中缺失或悬空的维度引用。事实表按维度汇总时，未匹配的量会落在该成员上而不会被静默丢弃。


## 三、字段明细


### 3.1 `dim_date`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| date_key | int64 | 1.0 | 912 | [2,024万, 2,026万]  中位数 2,025万 |  |
| full_date | object | 1.0 | 912 | 912 个不同取值 | 2024-01-01；2025-08-22；2025-08-24；2025-08-25 |
| year | int64 | 1.0 | 3 | [2,024, 2,026]  中位数 2,025 |  |
| quarter | int64 | 1.0 | 4 | [1, 4]  中位数 2 |  |
| month | int64 | 1.0 | 12 | [1, 12]  中位数 6 |  |
| month_name | object | 1.0 | 12 | 12 个不同取值 | 1月；3月；5月；4月 |
| year_month | object | 1.0 | 30 | 30 个不同取值 | 2024-01；2024-10；2025-07；2025-10 |
| year_quarter | object | 1.0 | 10 | 10 个不同取值 | 2024Q3；2024Q4；2025Q3；2025Q4 |
| week_of_year | int64 | 1.0 | 52 | [1, 52]  中位数 22 |  |
| day_of_week | int64 | 1.0 | 7 | [1, 7]  中位数 4 |  |
| day_name | object | 1.0 | 7 | 7 个不同取值 | 周一；周二；周三；周四 |
| is_weekend | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| day_of_year | int64 | 1.0 | 366 | [1, 366]  中位数 152.500 |  |
| is_month_end | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| season | object | 1.0 | 4 | 4 个不同取值 | 春；冬；夏；秋 |
| month_index | int64 | 1.0 | 30 | [1, 30]  中位数 15.500 |  |

### 3.2 `dim_project`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| project_key | int64 | 1.0 | 53 | [-1, 52]  中位数 26 |  |
| project_id | object | 1.0 | 53 | 53 个不同取值 | UNKNOWN；PJ-2024-027；PJ-2024-029；PJ-2024-030 |
| project_name | object | 0.9811 | 52 | 52 个不同取值 | UNKNOWN；香格里拉-暖冬助老行动；香格里拉-傈僳族阿尺木刮传习(1期)；维西-特殊儿童融合课堂 |
| category | object | 0.9811 | 8 | 8 个不同取值 | 助老敬老；助学支教；乡村振兴；生态环保 |
| category_code | object | 0.9811 | 8 | 8 个不同取值 | ELD；EDU；RUR；DIS |
| county | object | 0.9811 | 4 | 4 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；UNKNOWN |
| township | object | 0.9811 | 23 | 23 个不同取值 | 上江乡；建塘镇；白济汛乡；五境乡 |
| manager_staff_id | object | 1.0 | 10 | 10 个不同取值 | ST-012；ST-006；ST-004；ST-007 |
| status | object | 1.0 | 5 | 5 个不同取值 | 已结项；进行中；暂停；UNKNOWN |
| funding_source | object | 1.0 | 6 | 6 个不同取值 | 政府购买服务；基金会资助；企业定向捐赠；社会众筹 |
| risk_level | object | 1.0 | 4 | 4 个不同取值 | 低；中；高；UNKNOWN |
| planned_budget_cny | float64 | 0.9811 | 50 | [50,000, 947,000]  中位数 311,500 |  |
| target_beneficiaries | float64 | 0.9811 | 45 | [6, 298]  中位数 113.500 |  |
| planned_start_date | object | 0.9811 | 51 | 51 个不同取值 | 2024-09-18；2025-05-04；2024-04-07；2024-09-13 |
| planned_end_date | object | 0.9811 | 50 | 50 个不同取值 | 2025-10-21；2026-02-24；2026-03-04；2024-08-18 |
| actual_end_date | object | 0.8113 | 42 | 42 个不同取值 | 2025-10-02；2026-03-10；2025-08-26；2025-03-12 |
| duration_days | float64 | 0.9811 | 15 | [121, 547]  中位数 334 |  |
| actual_duration_days | float64 | 0.8113 | 39 | [87, 495]  中位数 284 |  |
| plan_delay_days | float64 | 0.8113 | 33 | [-53, 29]  中位数 -17 |  |
| start_year | float64 | 0.9811 | 3 | [2,024, 2,026]  中位数 2,024 |  |
| start_year_month | object | 1.0 | 24 | 24 个不同取值 | 2025-05；2024-09；2025-03；2025-09 |
| is_completed | float64 | 0.9811 | 2 | [0, 1]  中位数 1 |  |
| budget_per_beneficiary | float64 | 0.9811 | 52 | [274.730, 49,385]  中位数 2,727 |  |
| budget_band | object | 1.0 | 6 | 6 个不同取值 | 20-35万；35-60万；10-20万；60万以上 |
| is_cross_year | float64 | 0.9811 | 2 | [0, 1]  中位数 1 |  |

**参与的质量规则（9 条）**：`not_null:project_id`；`range:planned_budget_cny`；`row_count>=10`；`unique:project_id`；`completeness>=95%`；`date_order:planned_start_date<=planned_end_date`；`in_set:category`；`in_set:county`；`in_set:status`


### 3.3 `dim_volunteer`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| volunteer_key | int64 | 1.0 | 3601 | [-1, 3,600]  中位数 1,800 |  |
| volunteer_id | object | 1.0 | 3601 | 3601 个不同取值 | UNKNOWN；VN-02364；VN-02394；VN-02395 |
| volunteer_name | object | 0.9917 | 715 | 715 个不同取值 | 木英；白玛；蜂花；格桑 |
| gender | object | 1.0 | 3 | 3 个不同取值 | 女；男；UNKNOWN |
| birth_year | float64 | 0.9997 | 55 | [1,930, 2,008]  中位数 1,991 |  |
| age | float64 | 0.9997 | 55 | [18, 96]  中位数 35 |  |
| age_group | object | 1.0 | 7 | 7 个不同取值 | 18-24岁；35-44岁；25-34岁；45-54岁 |
| ethnicity | object | 1.0 | 10 | 10 个不同取值 | 藏族；傈僳族；纳西族；汉族 |
| county | object | 1.0 | 4 | 4 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；UNKNOWN |
| township | object | 1.0 | 30 | 30 个不同取值 | 洛吉乡；小中甸镇；建塘镇；格咱乡 |
| occupation | object | 1.0 | 11 | 11 个不同取值 | 在校大学生；中小学教师；机关事业单位职工；企业职员 |
| education | object | 1.0 | 6 | 6 个不同取值 | 本科；大专；高中/中专；初中及以下 |
| join_date | object | 0.9933 | 1864 | 1864 个不同取值 | 2026-06-10；2026-04-02；2025-10-18；2021-12-15 |
| join_year | float64 | 0.9933 | 8 | [2,019, 2,026]  中位数 2,023 |  |
| tenure_days | float64 | 0.9933 | 1864 | [20, 2,735]  中位数 985 |  |
| skills | object | 0.7553 | 441 | 441 个不同取值 | 摄影摄像；活动策划；驾驶；手工艺 |
| skill_count | float64 | 0.9997 | 4 | [0, 3]  中位数 2 |  |
| has_bilingual_skill | float64 | 0.9997 | 2 | [0, 1]  中位数 0 |  |
| status | object | 0.9919 | 5 | 5 个不同取值 | 活跃；沉睡；退出；新增 |
| is_active | float64 | 0.9997 | 2 | [0, 1]  中位数 1 |  |

**参与的质量规则（20 条）**：`not_null:shift_id`；`range:hours`；`referential:project_id->projects.project_id`；`referential:volunteer_id->volunteers.volunteer_id`；`row_count>=1000`；`unique:shift_id`；`not_null:volunteer_id`；`row_count>=10`；`unique:volunteer_id`；`completeness>=99%`；`cross_field:shift_after_volunteer_join`；`in_set:checkin_method`


### 3.4 `dim_beneficiary`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| beneficiary_key | int64 | 1.0 | 5141 | [-1, 5,140]  中位数 2,570 |  |
| beneficiary_id | object | 1.0 | 5141 | 5141 个不同取值 | UNKNOWN；BN-003422；BN-003480；BN-003479 |
| project_id | object | 0.9885 | 53 | 53 个不同取值 | PJ-2024-016；PJ-2025-002；PJ-2025-047；PJ-2025-024 |
| project_name | object | 1.0 | 52 | 52 个不同取值 | 香格里拉-高原雏鹰课堂(2期)；德钦县-格桑花助学计划；香格里拉-雪域书香伴读(4期)；维西-高原雏鹰课堂(3期) |
| project_category | object | 1.0 | 8 | 8 个不同取值 | 助学支教；助老敬老；乡村振兴；助残扶弱 |
| county | object | 0.9885 | 4 | 4 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；UNKNOWN |
| township | object | 1.0 | 23 | 23 个不同取值 | 上江乡；白济汛乡；小中甸镇；五境乡 |
| village | object | 1.0 | 263 | 263 个不同取值 | 上江8村；上江1村；UNKNOWN；上江5村 |
| age | float64 | 0.9998 | 92 | [5, 97]  中位数 23 |  |
| age_group | object | 1.0 | 8 | 8 个不同取值 | 18岁以下；65岁及以上；35-44岁；55-64岁 |
| gender | object | 0.9889 | 3 | 3 个不同取值 | 男；女；UNKNOWN |
| ethnicity | object | 0.9889 | 10 | 10 个不同取值 | 藏族；傈僳族；纳西族；汉族 |
| household_type | object | 1.0 | 8 | 8 个不同取值 | 脱贫户；低保户；留守儿童家庭；防返贫监测户 |
| risk_score | float64 | 0.9998 | 806 | [1, 99]  中位数 52.400 |  |
| risk_band | object | 1.0 | 4 | 4 个不同取值 | 中风险；高风险；低风险；UNKNOWN |
| is_minor | float64 | 0.9998 | 2 | [0, 1]  中位数 0 |  |
| is_elderly | float64 | 0.9998 | 2 | [0, 1]  中位数 0 |  |
| enroll_date | object | 0.9998 | 821 | 821 个不同取值 | 2025-05-28；2025-06-12；2025-01-09；2025-04-10 |
| enroll_year | float64 | 0.9998 | 3 | [2,024, 2,026]  中位数 2,025 |  |

**参与的质量规则（9 条）**：`not_null:service_id`；`range:duration_hours`；`range:satisfaction_score`；`referential:beneficiary_id->beneficiaries.beneficiary_id`；`referential:project_id->projects.project_id`；`row_count>=1000`；`unique:service_id`；`completeness>=95%`；`cross_field:service_after_enroll`


### 3.5 `dim_donor`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| donor_key | int64 | 1.0 | 5201 | [-1, 5,200]  中位数 2,600 |  |
| donor_id | object | 1.0 | 5201 | 5201 个不同取值 | UNKNOWN；DN-03455；DN-03473；DN-03472 |
| donor_name | object | 0.9819 | 848 | 848 个不同取值 | 格桑；白玛；和秀；木英 |
| donor_type | object | 1.0 | 6 | 6 个不同取值 | 个人；企业；UNKNOWN；基金会 |
| county | object | 1.0 | 5 | 5 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；州外 |
| is_recurring | float64 | 0.9998 | 2 | [0, 1]  中位数 0 |  |
| is_institutional | float64 | 0.9998 | 2 | [0, 1]  中位数 0 |  |
| is_local | float64 | 0.9998 | 2 | [0, 1]  中位数 1 |  |
| first_donation_date | object | 0.9998 | 2328 | 2328 个不同取值 | 2021-10-25；2025-08-26；2023-06-30；2021-08-23 |
| first_donation_year | float64 | 0.9998 | 8 | [2,019, 2,026]  中位数 2,022 |  |
| relationship_days | float64 | 0.9998 | 2328 | [0, 2,736]  中位数 1,424 |  |
| contact_masked | object | 0.9821 | 4957 | 4957 个不同取值 | 139****9554；156****3274；187****6417；138****5136 |

**参与的质量规则（4 条）**：`not_null:donor_id`；`row_count>=10`；`unique:donor_id`；`in_set:donor_type`


### 3.6 `dim_geography`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| geo_key | int64 | 1.0 | 23 | [-1, 22]  中位数 11 |  |
| county | object | 1.0 | 4 | 4 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；UNKNOWN |
| township | object | 1.0 | 23 | 23 个不同取值 | UNKNOWN；白济汛乡；洛吉乡；格咱乡 |
| county_short | object | 1.0 | 4 | 4 个不同取值 | 香格里拉；维西；德钦县；UNKNOWN |
| is_state_capital | float64 | 0.9565 | 2 | [0, 1]  中位数 0 |  |
| is_tibetan_area | float64 | 0.9565 | 2 | [0, 1]  中位数 1 |  |
| geo_code | object | 1.0 | 23 | 23 个不同取值 | UNKNOWN；维西傈僳族自治县\|白济汛乡；香格里拉市\|洛吉乡；香格里拉市\|格咱乡 |

### 3.7 `fact_volunteer_service`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| service_key | object | 1.0 | 12888 | 12888 个不同取值 | SH-0007477；SH-0002197；SH-0002170；SH-0006873 |
| date_key | int64 | 1.0 | 875 | [2,024万, 2,026万]  中位数 2,025万 |  |
| volunteer_key | int64 | 1.0 | 951 | [-1, 3,598]  中位数 1,748 |  |
| project_key | int64 | 1.0 | 53 | [-1, 52]  中位数 25 |  |
| geo_key | int64 | 1.0 | 23 | [-1, 22]  中位数 12 |  |
| volunteer_id | object | 1.0 | 951 | 951 个不同取值 | UNKNOWN；VN-00579；VN-01497；VN-02017 |
| project_id | object | 0.9775 | 52 | 52 个不同取值 | PJ-2025-004；PJ-2025-005；PJ-2026-036；PJ-2024-025 |
| county | object | 1.0 | 4 | 4 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县；UNKNOWN |
| service_role | object | 1.0 | 9 | 9 个不同取值 | 入户走访；双语翻译；教学辅导；专业支持 |
| checkin_method | object | 1.0 | 4 | 4 个不同取值 | 小程序签到；纸质签到表；社工代录；UNKNOWN |
| hours | float64 | 1.0 | 1239 | [0.250, 12]  中位数 3.440 |  |
| volunteer_value_cny | float64 | 1.0 | 1239 | [7.500, 360]  中位数 103.200 |  |
| is_long_shift | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| is_corrected | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| has_orphan_key | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |

### 3.8 `fact_donation`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| donation_key | object | 1.0 | 19988 | 19988 个不同取值 | DO-000830；DO-009103；DO-013974；DO-017035 |
| date_key | int64 | 1.0 | 912 | [2,024万, 2,026万]  中位数 2,025万 |  |
| donor_key | int64 | 1.0 | 5185 | [-1, 5,200]  中位数 2,580 |  |
| project_key | int64 | 1.0 | 53 | [-1, 52]  中位数 15 |  |
| donor_id | object | 1.0 | 5185 | 5185 个不同取值 | UNKNOWN；DN-05004；DN-00012；DN-03306 |
| project_id | object | 1.0 | 53 | 53 个不同取值 | UNKNOWN；PJ-2024-016；PJ-2025-024；PJ-2025-021 |
| amount_cny | float64 | 1.0 | 16323 | [5, 850,000]  中位数 148.055 |  |
| amount_band | object | 1.0 | 5 | 5 个不同取值 | 100-1000元；100元以下；1000-1万元；1-10万元 |
| is_major_gift | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| channel | object | 1.0 | 7 | 7 个不同取值 | 微信支付；公益平台；支付宝；现场捐赠 |
| designation | object | 1.0 | 3 | 3 个不同取值 | 定向；非定向；UNKNOWN |
| receipt_issued | bool | 1.0 | 2 | [0, 1]  中位数 1 |  |
| year_month | object | 1.0 | 30 | 30 个不同取值 | 2026-06；2026-05；2026-03；2026-04 |
| is_corrected | bool | 1.0 | 1 | [0, 0]  中位数 0 |  |
| has_orphan_key | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |

**参与的质量规则（13 条）**：`not_null:donation_id`；`range:amount_cny`；`referential:donor_id->donors.donor_id`；`row_count>=500`；`unique:donation_id`；`not_null:inkind_id`；`range:est_total_value_cny`；`range:quantity`；`referential:donor_id->donors.donor_id`；`unique:inkind_id`；`completeness>=99%`；`cross_field:donation_after_donor_first`


### 3.9 `fact_in_kind`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| inkind_key | object | 1.0 | 612 | 612 个不同取值 | IK-00001；IK-00416；IK-00409；IK-00410 |
| date_key | int64 | 1.0 | 461 | [2,024万, 2,026万]  中位数 2,025万 |  |
| donor_key | int64 | 1.0 | 267 | [-1, 5,194]  中位数 2,427 |  |
| project_key | int64 | 1.0 | 53 | [-1, 52]  中位数 27 |  |
| donor_id | object | 0.9869 | 266 | 266 个不同取值 | DN-03199；DN-03896；DN-01465；DN-04762 |
| project_id | object | 0.9869 | 52 | 52 个不同取值 | PJ-2024-050；PJ-2025-005；PJ-2024-037；PJ-2024-032 |
| item_name | object | 0.9869 | 10 | 10 个不同取值 | 棉被；大米；保暖衣物；学习用品套装 |
| item_category | object | 1.0 | 5 | 5 个不同取值 | 御寒物资；教育物资；食品；医疗物资 |
| quantity | float64 | 1.0 | 195 | [1, 520]  中位数 54 |  |
| unit | object | 0.9869 | 8 | 8 个不同取值 | 件；床；袋；套 |
| est_unit_value_cny | float64 | 1.0 | 605 | [17.230, 696.530]  中位数 113.060 |  |
| est_total_value_cny | float64 | 1.0 | 612 | [23.200, 141,943]  中位数 5,776 |  |
| value_band | object | 1.0 | 4 | 4 个不同取值 | 5000元以下；5000-2万元；2-10万元；10万元以上 |
| warehouse | object | 1.0 | 4 | 4 个不同取值 | 香格里拉中心仓；维西分仓；德钦分仓；UNKNOWN |
| year_month | object | 1.0 | 30 | 30 个不同取值 | 2025-07；2025-03；2024-09；2025-05 |
| is_corrected | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |

**参与的质量规则（5 条）**：`not_null:inkind_id`；`range:est_total_value_cny`；`range:quantity`；`referential:donor_id->donors.donor_id`；`unique:inkind_id`


### 3.10 `fact_beneficiary_service`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| service_key | object | 1.0 | 16137 | 16137 个不同取值 | SV-0001535；SV-0001495；SV-0010884；SV-0011761 |
| date_key | int64 | 1.0 | 880 | [2,024万, 2,026万]  中位数 2,025万 |  |
| beneficiary_key | int64 | 1.0 | 5129 | [-1, 5,140]  中位数 2,503 |  |
| project_key | int64 | 1.0 | 52 | [1, 52]  中位数 25 |  |
| geo_key | int64 | 1.0 | 23 | [-1, 22]  中位数 15 |  |
| beneficiary_id | object | 1.0 | 5129 | 5129 个不同取值 | UNKNOWN；BN-002184；BN-001894；BN-003735 |
| project_id | object | 1.0 | 52 | 52 个不同取值 | PJ-2025-002；PJ-2024-016；PJ-2025-047；PJ-2025-024 |
| service_type | object | 1.0 | 9 | 9 个不同取值 | 课业辅导；物资发放；心理陪伴；技能培训 |
| duration_hours | float64 | 1.0 | 867 | [0.330, 12]  中位数 2.470 |  |
| satisfaction_score | float64 | 0.9701 | 34 | [1, 5]  中位数 4.400 |  |
| is_satisfied | bool | 1.0 | 2 | [0, 1]  中位数 1 |  |
| is_dissatisfied | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| followup_needed | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| county | object | 1.0 | 3 | 3 个不同取值 | 香格里拉市；维西傈僳族自治县；德钦县 |
| is_corrected | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| has_orphan_key | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |

**参与的质量规则（9 条）**：`not_null:service_id`；`range:duration_hours`；`range:satisfaction_score`；`referential:beneficiary_id->beneficiaries.beneficiary_id`；`referential:project_id->projects.project_id`；`row_count>=1000`；`unique:service_id`；`completeness>=95%`；`cross_field:service_after_enroll`


### 3.11 `fact_project_monthly`

| 字段名 | 类型 | 非空率 | 基数 | 取值范围/分布 | 高频取值 |
|---|---|---|---|---|---|
| year_month | object | 1.0 | 30 | 30 个不同取值 | 2025-03；2025-05；2025-02；2025-04 |
| date_key | int64 | 1.0 | 30 | [2,024万, 2,026万]  中位数 2,025万 |  |
| project_key | int64 | 1.0 | 53 | [-1, 52]  中位数 24 |  |
| project_id | object | 0.9692 | 52 | 52 个不同取值 | PJ-2024-025；PJ-2025-024；PJ-2024-016；PJ-2024-003 |
| year | int64 | 1.0 | 3 | [2,024, 2,026]  中位数 2,025 |  |
| month_num | int64 | 1.0 | 12 | [1, 12]  中位数 6 |  |
| budget_category | object | 1.0 | 8 | 8 个不同取值 | 差旅交通；培训费；宣传传播；场地租赁 |
| planned_cny | float64 | 1.0 | 3741 | [65.440, 52,086]  中位数 2,741 |  |
| actual_cny | float64 | 1.0 | 3692 | [0, 63,601]  中位数 2,507 |  |
| variance_cny | float64 | 1.0 | 3723 | [-31,970, 25,656]  中位数 -111.005 |  |
| execution_rate | float64 | 1.0 | 3706 | [0, 1.892]  中位数 0.926 |  |
| is_overspend | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |
| spend_share | float64 | 1.0 | 3751 | [0.000, 0.180]  中位数 0.004 |  |
| is_corrected | bool | 1.0 | 2 | [0, 1]  中位数 0 |  |

## 四、指标口径索引

核心指标的口径公式、目标值与数据来源见 `reports/kpi_catalog.csv`。

常用的口径定义速查：

| 概念 | 口径 |
|---|---|
| 活跃志愿者 | 在观察窗口内至少产生 1 条志愿服务签到记录的志愿者 |
| 志愿者活跃率 | 活跃志愿者数 ÷ 注册志愿者总数（全国基线 28.72%） |
| 志愿服务经济价值 | 服务时长 × 30 元/小时（由全国口径反推的折算率） |
| 服务人次 | `fact_beneficiary_service` 的记录数（去重后的一次服务 = 一行） |
| 复访率 | 服务 ≥2 次的服务对象数 ÷ 有服务记录的服务对象数 |
| 预算执行率 | SUM(实际支出) ÷ SUM(计划支出) |
| 捐赠来源集中度 HHI | SUM((单个捐赠人金额 ÷ 捐赠总额)²) |
| 签约有效期留存 | 注册满 12 个月且近 6 个月仍有服务记录的志愿者占比 |
| 数据完整性 | 1 − 缺失单元格数 ÷ 总单元格数 |

## 五、已知局限

1. `fact_volunteer_service` 本身不含乡镇字段，`geo_key` 由项目台账的乡镇反推，因此无法刻画同一项目跨乡镇的服务分布。
2. `dim_volunteer` 中少量无任何服务记录的志愿者其 `join_date` 无法反推，保留为空，这类记录仍计入注册志愿者总数，但不参与在职时长类指标。
3. `fact_in_kind` 中入库日期缺失的记录已从事实表剔除（原因见 `cleaning_log.csv`），因此物资捐赠总额会略低于源表直接求和的结果。
4. 满意度评分缺失的记录不参与满意度均值，但计入服务人次。

