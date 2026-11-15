# 竞品券后价还原与跟价建议台（`pricewatch`）

> 数据采集 / RPA · 竞品价格采集与跟价建议。原生 HTML/CSS/JS 前端 + FastAPI 后端 + Celery worker + PostgreSQL 16 + Redis，5 个容器一键起。

## 1. 场景

- **模拟客户**：泊岸家居 · 电商定价组（5 人），盯 6 个竞品店铺、320 个自有 SKU 的每日到手价。
- **现有工作流**：运营早晚两次人工抄价到 Excel；券后价靠心算（满减、店铺券、平台跨店券、会员价叠加）；跟价靠经验拍数，改价前在群里问一句「能不能跟」。
- **三个痛点**：
  1. 页面价 ≠ 到手价：满减/店铺券/平台券/会员价叠加，同一条链接算三次得出三个数。
  2. 跟价一刀切：直接跟到竞品最低价，毛利被打穿，月底才发现几个爆款是负毛利。
  3. 没有依据链：跟了哪一档、依据哪天哪条快照、谁批的，吵架时说不清。
  4. 整单券被逐行重复抵扣：平台跨店券按整单门槛只发一张，旧口径却把它分别全额抵到每个货号，
     订单实付和逐行券后价越差越远，跟价建议被系统性压低。
- **上下游**：竞品店铺页面（RPA 抓价，`capture_config.replay` 指向 fixture 回放）→ **本系统**（抓价快照入库 → 券后价还原 → 跟价建议 → 审批 → 改价单/导出）→ 电商后台改价 + 定价组日报。
- **一天的工作剧本**：
  1. 08:00 定时任务回放当天 320 条抓价快照（`campaign=618 现货`），失败快照进 `invalid_snapshots`。
  2. 08:01 算法 A 把每条快照按 1/2/3 件还原成券后单价，写入 `snapshots.effective`。
  3. 08:02 算法 B 对每个 SKU 取对标竞品近 3 天券后单件价的分位数，套用命中的跟价规则算出建议价。
     样本优先用「已对账通过」订单按货号成交金额分摊整单优惠后的券后单价，没有订单才回退抓价快照。
  4. 08:05 定价主管打开「跟价建议」，先看 9 条高风险（竞品券后价已破我方底价）。
  5. 09:00 主管对 `PB-sofa-001` 批准改价 ¥284.58（竞品 P25 ¥279.00，毛利 32.74%）。
  6. 10:30 驳回 2 条降幅 > 15% 的建议，备注「等 618 结束再跟」。
  7. 18:00 导出「跟价建议清单 CSV」给店长群，附券后 1/2/3 件价格与命中组合。
  8. 20:00 晚班运营再跑一次测算，对比早晚价差，审计日志留痕。

## 2. 角色与权限

| 角色 | 账号 | 权限 | 典型动作 |
|---|---|---|---|
| 定价主管 `admin` | `admin@pricewatch.io` | 竞品/SKU/规则维护 + 跑测算 + 审批 + 订单录入与回冲 + 导出 + 审计 | 改底价与目标毛利、审批改价、补录订单与退款事件、导出 CSV |
| 价格运营 `analyst` | `analyst@pricewatch.io` | 只读配置 + 跑测算 + 看建议与订单对账 | 早晚各跑一次抓价测算、核对券后价与分摊 |
| 店长只读 `viewer` | `viewer@pricewatch.io` | 只读；成本/底价脱敏 | 看券后价与建议，不能改价不能导出 |

密码统一 `Demo@2026`。低权角色调用高权接口返回**真实 HTTP 403**（`viewer` 写配置/导出/审计、`analyst` 审批/导出/审计），无 token 401，非法参数 422，重复自然键 409。

## 3. 功能模块（7 个业务模块）

| # | 模块 | 对应页面 | 后端 services + controller | 主要 API |
|---|---|---|---|---|
| ① | 竞品店铺与 SKU 价格口径维护（成本/底价/目标毛利/对标货号） | `#/catalog` | `competitor_service` + `competitor_controller`；`sku_service` + `sku_controller` | `GET/POST /api/competitors`、`PATCH /api/competitors/{id}`、`GET/POST /api/skus`、`GET/PATCH /api/skus/{id}` |
| ② | 抓价任务与快照入库（回放 RPA 抓价、手工补录、按日期/店铺/类目检索） | `#/snapshots` | `snapshot_service` + `snapshot_controller` | `GET /api/snapshots`、`GET /api/snapshots/{id}`、`POST /api/snapshots` |
| ③ | 券后价还原（算法 A，3 个件数档位 + 命中组合说明） | `#/snapshots`（列表列 + 明细抽屉） | `services/effective_price.py` + `pricing_service` | `POST /api/runs`（回放当日快照并重算）、`GET /api/snapshots/{id}`（`effective` 明细） |
| ④ | 跟价规则与建议（算法 B：P25 锚点、成本底线、底价硬约束） | `#/advices`、`#/catalog` | `rule_service` + `follow_advice.py` + `pricing_service` + `advice_controller` | `GET/POST /api/rules`、`PATCH /api/rules/{id}`、`POST /api/runs`、`GET /api/advices` |
| ⑤ | 审批与改价（批准/驳回、核定改价、审批人留痕） | `#/advices`（明细抽屉） | `advice_service` + `advice_controller` | `PATCH /api/advices/{id}/decision` |
| ⑥ | 订单整单优惠分摊与对账（订单/货品组成/优惠明细/逐行分摊/晚到退款回冲，对账通过才可作跟价样本） | `#/orders` | `order_allocation.py` + `order_service` + `order_controller` | `GET /api/orders`、`GET /api/orders/{id}`、`POST /api/orders`、`POST /api/orders/{id}/refunds`、`POST /api/orders/{id}/resettle` |
| ⑦ | 报表导出与值班盘点（建议/快照/运行/分摊/回冲/对账六张 CSV、总览 KPI） | `#/exports`、`#/overview` | `export_service` + `export_controller`；`overview_service` | `GET /api/exports/advices.csv`、`/snapshots.csv`、`/runs.csv`、`/allocations.csv`、`/refunds.csv`、`/reconciliations.csv`、`GET /api/overview` |

横切能力（不计入业务模块）：JWT 三角色鉴权、审计日志（`GET /api/audit`，仅 admin）、Redis 固定窗口限流、worker 心跳。

## 4. 核心算法与口径

### 算法 A｜券后有效价还原（`backend/src/services/effective_price.py`）

输入一条快照：页面价 `page_price`、满减阶梯 `full_reduction[{threshold, minus}]`、店铺券 `shop_coupon{threshold, minus, stackable}`、平台跨店券 `platform_coupon{per, minus}`、会员折扣 `member_discount`。

对 `n ∈ {1,2,3}`：

```
subtotal = page_price × n
满减     = 命中「threshold ≤ subtotal」的最高档（只叠一档）
店铺券   = subtotal ≥ threshold 时减券面；stackable=false 时与平台券二选一取更省
平台券   = floor(subtotal / per) × minus      # 跨店每满，可重复命中
payable  = (subtotal − 满减 − 店铺券 − 平台券) × member_discount   # 四舍五入到分
unit(n)  = payable / n
```

输出 `unit_price_1/2/3`、`best_units`、`best_payable`、`note`（命中组合说明）、`discount_pct`、`confidence`。失败态（页面价 ≤ 0、门槛为负、券面 > 门槛、会员折扣越界）返回 `valid=false, error=INVALID_SNAPSHOT`，计入运行指标 `invalid_snapshots`。

**手算校验（验收口径）**：页面价 299、满 300 减 40、店铺券满 200 减 20、平台每满 300 减 50：

| 件数 | 计算 | 单价 |
|---|---|---|
| n=1 | 299 − 0（满减未触发）− 20 − 0 | **¥279.00** |
| n=2 | 598 − 40 − 20 − 50 | **¥244.00** |
| n=3 | 897 − 40 − 20 − 100 | **¥245.67** |

最优件数 2（单件最省），页面与抽屉里显示 `1件 ¥279.00｜2件 ¥244.00（最优）｜3件 ¥245.67`。

### 算法 B｜跟价建议（`backend/src/services/follow_advice.py`）

输入：该 SKU 对标竞品**近 3 天券后单件价**列表（优先订单分摊券后价，见算法 C）、成本 `cost`、底价 `floor_price`、目标毛利 `target_margin_pct`、我方当前价 `current_price`、命中规则的 `strategy` 与 `max_drop_pct`。

```
P10/P25/P50 = 线性插值分位数（等价 numpy percentile）
cost_floor   = max(cost × (1 + target_margin%/100), floor_price)
market_cap   = min(P25 × 1.02, current_price)
suggested    = strategy == "hold" ? current_price
             : (market_cap < cost_floor ? cost_floor : market_cap)
suggested    = max(suggested, floor_price)          # 硬约束：任何情况下不破底价
gap_pct      = (suggested − current) / current × 100
margin_pct   = (suggested − cost) / suggested × 100
风险         = high   若 竞品券后价 < 底价（breach）或 margin_pct < 5%
             | medium  若降幅 > max_drop_pct
             | low     其他
动作         = follow（gap ≤ −0.5%）/ raise（gap ≥ +0.5%）/ hold
```

输出 `advice(action)`、`suggested_price`、`p10/p25/p50`、`margin_pct`、`risk`、`reason`（含每条触发原因）、`clamped_by`（`cost_floor`/`floor_price`/`hold`）与 `need_follow`（风险 high 或 |价差| ≥ 1.5% 才出建议）。

**手算校验**：`PB-sofa-001` 竞品三日券后 ¥279.00（P10=P25=P50=279），成本 ¥191.40、底价 ¥239.25、目标毛利 12% → 成本底线 ¥239.25，市价上限 `min(279×1.02, 319)=284.58` → 建议价 **¥284.58**，价差 **−10.79%**，建议毛利 **32.74%**，风险 low。

### 算法 C｜订单整单优惠分摊与回冲（`backend/src/services/order_allocation.py`）

平台跨店券按**整单门槛**发放，一张券只减一次。旧口径把同一张券分别全额抵到每个货号，
订单实付与逐行券后价越差越远。现在的口径：

```
1. 每笔整单优惠只记一次金额（discounts），不复制到行
2. 按各货号成交金额（单价×数量）比例分摊，分值用「累计最大余数法」：
   Σ逐行分摊 == 整单优惠（精确到分，不丢不重）
3. 行券后净额 = 成交金额 − 分摊额；券后单价 = 净额 / 数量
4. 退款事件按业务发生时间回放（晚到事件也按发生顺序回冲）：
   回冲金额按该行剩余数量比例拆到各笔优惠分摊，保持「净额 + 分摊 = 成交额」
5. 对账闸门：逐行分摊合计、逐行净额合计、订单实付、退款回冲四组数对平
   才算 reconcile_state = passed；只有通过的订单进跟价样本
```

**手算校验**：沙发 2 件 @¥600 + 茶几 1 件 @¥300，跨店券整单 ¥150（每满 300 减 30 ×2，只发一张）：
成交额 ¥1500、实付 ¥1350；按 1200:300 分摊 → 沙发 ¥120、茶几 ¥30 → 券后单价 **¥540 / ¥270**。
旧口径每行各抵 ¥150：沙发单价 ¥525、茶几 ¥150，逐行加总 ¥1200，比实付少 ¥150（`legacy_gap = −150`）。

**晚到退款**：若「整单全退」事件先到（到达序 1，业务发生 11-12 18:00）、「退 1 件沙发」后到
（到达序 2，业务发生 11-11 09:00），按到达顺序会算反；系统按业务发生时间重放：
先退沙发 1 件（回冲 ¥540），再整单退剩余（回冲 ¥810），合计 ¥1350 = 实付，对账通过。

## 5. 快速启动

```bash
cp .env.example .env
docker compose up -d --build     # 等 5/5 healthy
open http://localhost:18202
```

首次启动 backend 自动建表并执行一次 seed（`SEED_DEMO=true`）。seed 幂等，可重复执行：

```bash
docker compose exec -T backend python -m scripts.seed_demo
# SEED_SUMMARY brand=pricewatch users=3 sources=6 runs=1 findings=42 pending=42
# 同时写入 5 张订单样例（整单分摊 3 张 / 晚到退款 1 张 / 对账失败 1 张）
```

## 6. 访问地址

| 服务 | 地址 |
|---|---|
| 前端 | http://localhost:18202 |
| 后端 API 文档 | http://localhost:19202/docs |
| 健康检查 | http://localhost:19202/api/health |
| 数据库映射 | localhost:17702（PostgreSQL 16） |

## 7. 技术栈

| 层 | 选型 |
|---|---|
| 前端 | 原生 HTML5 + CSS（oklch 令牌 / 价签语言）+ ES Modules；无框架、无打包、无 CDN 脚本（仅 Google Fonts） |
| 后端 | Python 3.11、FastAPI 0.115、SQLAlchemy 2.0、PyJWT、passlib(bcrypt) |
| 异步 | Celery 5.4 + Redis（心跳、每日 08:40 自动测算） |
| 数据库 | PostgreSQL 16 |
| 部署 | Docker Compose，恰好 5 服务（frontend/backend/worker/db/redis），全部 healthcheck |

## 8. 目录

```text
backend/
  src/{config,constants,controllers,middlewares,models,routes,schemas,services,utils}
  src/services/{effective_price,follow_advice,pricing_service,...}.py   # 两个算法是纯函数
  scripts/{seed_demo.py,demo_config.py,worker_healthcheck.sh}
  migrations/001_core_tables.sql   tests/
frontend/site/
  index.html  css/{tokens,app}.css
  js/{config,api,store,ui,app}.js  js/components/price.js  js/pages/*.js
```

## 9. 环境变量

见 `.env.example`：`COMPOSE_PROJECT_NAME`、`FRONTEND_PORT=18202`、`BACKEND_PORT=19202`、`DB_PORT=17702`、`DB_*`、`JWT_SECRET`、`REDIS_URL`、`SEED_DEMO`、`RATE_LIMIT_*`、`APP_TIMEZONE_OFFSET_MINUTES=480`。

## 10. 数据模型（10 个领域实体）

| 实体 | 表 | 关键字段 | 自然键 |
|---|---|---|---|
| 竞品店铺 `Competitor` | `competitors` | code、name、platform、shop_url、capture_config、last_captured_at | `code` |
| 自有 SKU `Sku` | `skus` | sku_code、name、category、competitor_id、cost、floor_price、target_margin_pct、current_price | `sku_code` |
| 抓价快照 `Snapshot` | `snapshots` | sku_id、captured_on、page_price、满减/店铺券/平台券/会员折扣、effective(算法 A) | `(sku_id, captured_on)` |
| 跟价规则 `Rule` | `rules` | code、scope_kind/scope_value（sku>category>competitor>global）、strategy、target_margin_pct、max_drop_pct、priority | `code` |
| 跟价建议 `Advice` | `advices` | run_id、sku_id、P10/P25/P50、suggested_price、margin_pct、gap_pct、severity、status、approver | `(run_id, sku_id)` |
| 订单 `Order` | `orders` | order_no、competitor_id、paid_on、items_total、discount_total、payable_declared、refunded_total、reconcile_state、settle | `order_no` |
| 货品组成 `OrderItem` | `order_items` | order_id、line_no、sku_code、qty、unit_price、discount_allocated、net_amount、effective_unit_price、remaining_qty | `(order_id, line_no)` |
| 整单优惠 `OrderDiscount` | `order_discounts` | order_id、seq、kind、amount（整单只记一次）、rule | `(order_id, seq)` |
| 逐行分摊 `OrderAllocation` | `order_allocations` | order_id、discount_id、item_id、weight（成交金额）、amount、refunded_amount、remaining_amount | `(discount_id, item_id)` |
| 回款变更 `RefundEvent` | `refund_events` | order_id、event_no、kind、sku_code、qty、occurred_at、arrival_seq、business_seq、amount_declared、amount_reversed、variance、state | `(order_id, event_no)` |

另有执行实体 `Run`（一次测算）与横切的 `User` / `AuditLog`。seed 口径：6 竞品 / 320 SKU / 960 快照（320×3 天）/ 5 条规则 / 5 张订单（3 张对账通过分摊单 + 1 张晚到退款单 + 1 张对账失败单）/ 1 次测算 → 42 条建议（high 9 / medium 13 / low 20），其中 `PB-sofa-001` 的样本口径为「订单分摊券后价」。

## 11. 共享枚举出现位置

| 枚举 | 后端 | 前端 |
|---|---|---|
| 角色 `admin/analyst/viewer` | `backend/src/constants/roles.py`（含 `PERMISSIONS`） | `frontend/site/js/config.js` → `PERMISSIONS` / `ROLE_LABELS` |
| 平台 `douyin/taobao/jd/pdd/xhs/weidian` | `backend/src/constants/enums.py`（`Platform` + `PLATFORM_LABELS`） | `config.js` → `ENUMS.platform` |
| 类目 `sofa/table/bed/cabinet/decor` | `enums.py`（`Category` + `CATEGORY_LABELS`） | `config.js` → `ENUMS.category` |
| 跟价策略 `guarded/aggressive/hold` | `enums.py`（`RuleStrategy`） | `config.js` → `ENUMS.strategy` |
| 风险等级 `high/medium/low` | `enums.py`（`RiskLevel` + `RISK_LABELS`） | `config.js` → `ENUMS.risk` / `riskTone()` |
| 审批状态 `pending/approved/rejected` | `enums.py`（`AdviceStatus`） | `config.js` → `ENUMS.adviceStatus` / `statusTone()` |
| 建议动作 `follow/hold/raise` | `enums.py`（`AdviceAction`） | `config.js` → `ENUMS.action` |
| 运行状态 / 触发方式 | `enums.py`（`RunStatus` / `TriggerKind`） | `config.js` → `ENUMS.runStatus` / `ENUMS.trigger` |
| 订单状态 `paid/partially_refunded/refunded` | `enums.py`（`OrderStatus`） | `config.js` → `ENUMS.orderStatus` |
| 对账结论 `passed/failed` | `enums.py`（`ReconcileState`） | `config.js` → `ENUMS.reconcile` |
| 整单优惠类型 | `enums.py`（`OrderDiscountKind` + `ORDER_DISCOUNT_KIND_LABELS`） | 页面按后端 `kind_label` 展示 |
| 回款变更类型 `item/order/shipping` | `enums.py`（`OrderRefundKind`） | `config.js` → `ENUMS.refundKind` |
| 建议样本口径 `order/snapshot` | `enums.py`（`SampleBasis`） | `config.js` → `ENUMS.sampleBasis` |

## 12. Docker 说明

- 5 个服务：`frontend`(Nginx 静态站) / `backend`(uvicorn) / `worker`(Celery) / `db`(PostgreSQL 16) / `redis`，全部带 healthcheck，`depends_on: service_healthy`。
- Nginx：`location /api/ { proxy_pass http://backend:8000/; }` + SPA 回落；端口 `${FRONTEND_PORT:-18202}`。
- 命名卷：`db_data`、`redis_data`。收尾：`docker compose down -v --remove-orphans`。

## License

内部演示项目，仅用于评测与教学。
