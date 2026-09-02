# Brooks 强势第一腿—第二腿独立观察池设计

状态：实现设计 v1

方法标识：`brooks_second_leg`

策略版本：`brooks-second-leg-daily-v1`

研究依据：`AshareResearch/brooks_price_action/comprehensive_study/comprehensive_report_v1.md`

## 1. 产品决定

新增一套与 Weinstein、Minervini **并列但完全独立** 的 Brooks 日线观察体系。它只回答：

> 哪些 A 股刚完成高质量第一腿，哪些已经发生有意义回调，哪些在回调后出现了可观察的向上恢复？

它不回答“现在是否应买入”，不预测收益，不输出自动买卖指令，也不与另外两种方法合成总分。

第一版只覆盖沪深普通 A 股。四个指数继续只使用现有 Weinstein 周线 Stage 与 Minervini 日线 Stage 2；Brooks 不参与指数判定、行业确认、两法交集或现有状态生命周期。

Brooks 使用单独的事实表、查询接口和网站页面 `/a/brooks-second-leg`。现有 `/a/daily` 的 `all / weinstein / minervini / both` 语义保持不变，尤其不能把 `both` 改成“三法交集”。

## 2. 能力边界

### 系统做什么

- 识别 3、5、8 个交易日形成的高质量上涨第一腿；
- 按事件维护 `S0 → S1 → S2` 状态；
- 将 H2 作为 H1 失败后的研究标签，不提高排序；
- 展示第一腿、回调、目标和两种失效边界；
- 在目标完成、结构失效、超时或交易资格异常时给出明确状态与原因码；
- 保留逐日、可追溯、严格 as-of 的方法事实。

### 系统不做什么

- 不把 `touch25`、H1 或 H2 宣传成已验证买点；
- 不因第一腿更快、持续 8 日、超过 3 ATR、形成通道、H2、缩量或振幅收缩而加分；
- 不用 20/40 日固定收益替代路径状态；
- 不把研究中的历史发生率显示成个股未来概率；
- 不自动计算公共“建议仓位”；仓位只能在登录用户自己的交易计划中依据其风险预算试算；
- 不回填未来才知道的次日开盘可成交性。

## 3. 数据合同

### 3.1 价格与时间

- 频率：交易所日线；所有判断在决策日收盘后产生。
- 计算价格：同一决策日尺度下的前复权 OHLC。实现时复用市场库的原始 OHLC、同日复权因子和 `data_as_of_date <= decision_date` 约束，不能跨日期拼接不同价格尺度。
- 成交额：Tushare `amount` 的源单位为千元；20 日中位数门槛 `20,000` 等于人民币 2,000 万元。
- ATR：`ATR20[t]` 为 `t-20 ... t-1` 的 20 个已完成交易日真实波幅算术平均，不含信号日：

  `TR[j] = max(H[j]-L[j], |H[j]-C[j-1]|, |L[j]-C[j-1]|)`

- 最少历史：信号日前至少 133 根有效日线，并且 ATR、20 日成交额和第一腿窗口完整。
- 所有日期窗口均按该股票实际交易序列计数，不按自然日计数。

### 3.2 基础股票池

信号日必须同时满足：

- 沪深普通 A 股，排除代码以 `4`、`8` 开头的证券；
- 当日处于上市状态，非 ST／*ST／退市整理，非停牌；
- OHLC、复权因子、成交额和涨跌停价数据完整；
- 前 20 个已完成交易日成交额中位数不少于 2,000 万元；
- 信号日不是一字涨停；连续封板或次日无法合理成交时以 `NOT_ACTIONABLE_LIMIT` 排除；
- 数据异常时不猜测，输出 `DATA_INSUFFICIENT` 或对应原因码。

市值、现有 Weinstein/Minervini 结果、指数 Stage、行业强度均不参与入池。将来即使增加市场或行业背景，也只能作为独立上下文字段，不能悄悄变成硬门槛。

## 4. 第一腿识别

对每只股票的每个收盘日 `t`，分别检查 `N ∈ {3, 5, 8}`。第一腿起点是 `t-N` 的收盘，质量窗口是 `t-N+1 ... t` 的 N 根 K 线。

### 4.1 冻结公式

设 `P0 = C[t-N]`，`P1 = C[t]`，`impulse_height = P1-P0`：

- `move_atr = impulse_height / ATR20[t]`，要求 `>= 3.0`；
- `path_efficiency = (C[t]-C[t-N]) / Σ|C[j]-C[j-1]|`，`j=t-N+1...t`，要求 `>= 0.75`；
- `bullish_body_count = count(C[j] > O[j])`，要求 `>= ceil(0.60*N)`；
- `bullish_body_share = bullish_body_count / N`，仅用于展示与同状态排序；
- 单根 `CLV[j] = (C[j]-L[j])/(H[j]-L[j])`；零振幅 K 线记为 `0.5`；
- `mean_close_location = mean(CLV[j])`，要求 `>= 0.65`；
- 相邻 K 线重叠：

  `overlap[j] = max(0, min(H[j],H[j-1])-max(L[j],L[j-1])) / (max(H[j],H[j-1])-min(L[j],L[j-1]))`

  分母为零时记为 `1.0`；`mean_overlap` 对窗口内 N 对相邻 K 线求均值，要求 `<= 0.35`；
- 至少两根阳线。该条件对 3/5/8 日窗口都显式保留；实际更严格的 `ceil(0.60*N)` 仍是主判断；
- `speed_atr_per_session = move_atr / N` 只展示，不参与筛选或排序。

这里的“阳线贡献”采用冻结研究合同实际使用的**阳线根数占比**，不改成阳线实体金额占比，否则就不再是同一套规则。

### 4.2 去重

- 同一收盘日多个窗口合格时，保留最长窗口：`8 > 5 > 3`；
- 每只股票按时间正序扫描，前一个保留信号后的 40 个交易位置内不再产生新事件；
- 冷却属于事件去重，不代表旧事件必须继续活跃 40 日；
- 事件 ID 固定为 `brooks|{symbol}|{signal_date}|{duration}`。

## 5. 状态机

每个第一腿事件独立演进，不使用某只股票的单一覆盖状态。若同一股票在冷却结束后产生新事件，旧事件可保留为历史完成/失效记录。

| 状态 | 进入条件 | 网站含义 | 排序层级 |
| --- | --- | --- | ---: |
| `S0_IMPULSE` | 第一腿在信号日收盘合格 | 基础观察 | 3 |
| `S1_TOUCH25` | 信号后 15 个交易日内首次满足有效 `touch25` | 重点观察，不是买点 | 2 |
| `S2_H1` | `touch25` 当日或之后、且不晚于信号后第 15 个交易日，首次出现 H1 | 恢复确认候选，不是自动买入 | 1 |
| `S2_H1_H2_TAG` | H1 失败后出现 H2 | 仍归入 S2 层，只加 H2 标签 | 1 |
| `COMPLETED` | 前高触及与 MM1.0 均已完成 | 主要路径均完成 | 不进活跃池 |
| `INVALIDATED` | 结构失效 | 移出活跃池 | 不进活跃池 |
| `STALE` | 观察窗口结束仍未升级 | 降级，历史保留 | 不进默认活跃池 |
| `PAUSED` | ST、停牌、涨跌停不可成交或数据异常 | 暂停资格并显示原因 | 不进默认活跃池 |

### 5.1 `touch25`

对信号后第 1 至 15 个交易日逐日检查：

- `current_retracement = max(0, signal_close-current_close) / impulse_height`；
- `pullback_low_anchor = min(low[signal+1 ... decision])`；
- `maximum_low_retracement = max(0, signal_close-pullback_low_anchor) / impulse_height`；
- `0.25 <= current_retracement < 1.0`；
- `maximum_low_retracement < 1.0` 且 `current_close > impulse_start`。

首次满足即进入 S1。若日内低点已完整回吐第一腿，即使收盘重新站回，也不生成 S1，因为日线无法证明盘中先后顺序。

### 5.2 H1

H1 可与首次 `touch25` 同日发生，必须同时满足：

- 当前仍是有效结构；
- `close > open`；
- 当日 `CLV >= 0.65`；
- `high > previous_day_high`。

H1 只标记状态恢复。决策日收盘时 `next_open_status=PENDING`；到下一交易日后再更新为 `TRADABLE` 或 `REJECTED_{ST|SUSPENDED|LIMIT_UP|MISSING}`。这个事后字段不能反写 H1 决策日事实。

研究可比的 H1 只允许发生在信号后 15 个交易日内。S1 可在页面保留至首次 `touch25` 后 20 个交易日；窗口外出现的同类形态只记 `LATE_H1_DIAGNOSTIC`，不能升级为研究口径的 S2。超过 20 日仍无合格 H1，状态转为 `STALE_NO_H1`。

### 5.3 H2 标签

记录 H1 当时已知的 `h1_pullback_low_anchor`。在信号后最多 30 个交易日内：

1. 价格低点严格跌破该锚点，但尚未触及第一腿前高，也未跌破第一腿起点，记为 H1 失败；
2. 此后首次再次满足 H1 的三项反转条件，记 `H2_RESCUE`；失败当日本身可在收盘后同时成为 H2；
3. 若同一根日 K 同时触及前高并跌破 H1 锚点，记 `AMBIGUOUS_SAME_BAR`，不生成 H2；
4. H2 不加分、不高于 H1，也不成为买入指令。

## 6. 目标、失效与删失

在任一决策日，使用截至当日已知的回调低点锚点 `A`：

- 第一腿前高：`prior_high = signal_day_high`；
- `MM0.8 = A + 0.8 * impulse_height`；
- `MM1.0 = A + 1.0 * impulse_height`；
- 紧失效价：`tight_invalidation = A`，后续低点严格低于 A 才触发；
- 结构失效价：`structural_invalidation = impulse_start`，后续低点小于等于该价触发。

公共页面同时显示两种失效距离，但不替用户选择止损。以结构失效路径的历史结果说明机会，却用紧失效距离伪装成同一概率，是禁止的。

目标从决策日后的下一交易日开始观察：

- `high >= prior_high`：前高触及；`close >= prior_high`：前高收盘站上；
- `high >= MM0.8/MM1.0`：相应测量目标完成；
- 同一根日 K 同时到达目标和所选失效边界，记 `AMBIGUOUS_SAME_BAR`，不推断盘中顺序；
- 单个目标完成只增加不可逆的完成标记，事件仍可留在原 S1/S2 层继续观察其他目标；前高触及和 MM1.0 都完成后才归档为 `COMPLETED`；
- 60 个交易日未到目标也未失效，只记研究删失；产品活跃池的超时规则独立执行，不能显示成“必须持有 60 日”。

## 7. 排序：词典序，不生成分数

默认活跃池按以下字段逐级排序：

1. 状态层级：`S2 > S1 > S0`；H2 与 H1 同层；
2. `path_efficiency` 降序；
3. `mean_close_location` 降序；
4. `mean_overlap` 升序；
5. `bullish_body_share` 降序；
6. `median_amount20` 降序；
7. 最近状态日期降序；
8. 股票代码升序，保证稳定排序。

页面必须将排序字段逐项展示或在“规则说明”中可见，不能把这套词典序包装成 0–100 分。

以下字段明确不参加排序：`duration_bars`、`speed_atr_per_session`、超过 3 ATR 的幅度、H2、通道、缩量、振幅收缩、Weinstein/Minervini 结果、市场 Stage、行业强弱。

## 8. 持久化设计

不得修改现有 `stock_method_daily_fact.method IN ('minervini','weinstein')` 约束。新增表只追加，不与现有方法事实共用生命周期基线。

### 8.1 `brooks_impulse_event`

一行一个不可变第一腿事件，主键 `(event_id, policy_version)`：

```text
event_id, symbol, signal_date, start_date, duration_bars
impulse_start, signal_close, signal_high, impulse_height, atr20, move_atr
path_efficiency, bullish_body_count, bullish_body_share
mean_close_location, mean_overlap, speed_atr_per_session
median_amount20, signal_tradability, source_digest
origin, policy_version, created_at
```

### 8.2 `brooks_second_leg_daily_fact`

一行表示一个事件在一个决策日收盘后的不可变快照，主键 `(as_of_date, event_id, policy_version)`：

```text
as_of_date, event_id, symbol, state, state_reason, state_rank
touch25_date, h1_date, h1_failure_date, h2_date
bars_after_signal, bars_after_touch25
current_retracement, maximum_low_retracement, pullback_atr
pullback_low_anchor, prior_high, mm_0_8, mm_1_0
tight_invalidation, structural_invalidation
prior_high_touched, prior_high_closed, mm_0_8_completed, mm_1_0_completed
next_open_status, tradability_reason, evidence_json
source_digest, origin, policy_version, created_at
```

`evidence_json` 保存阈值检查、使用的数据位置和异常原因；高频筛选字段必须是实体列，不能只藏在 JSON 中。

### 8.3 `brooks_run_receipt`

独立记录运行日期、策略版本、行情摘要、候选数、各状态数、未知/暂停数、源摘要、开始/结束时间和成功状态。现有 watchlist receipt 不得冒充 Brooks 已成功计算。

三个表均追加禁止更新/删除触发器及常用索引。线上迁移只能新增对象；迁移前备份 ECS 权威观察池数据库，失败时回滚到备份。日常部署不得用本地库覆盖线上库。

## 9. 代码边界

建议按以下职责实现：

- `watchlist/brooks.py`：纯计算；K 线标准化、ATR、第一腿、状态机、目标/失效；
- `watchlist/brooks_service.py`：遍历证券、40 日冷却、as-of 数据资格、运行凭证；
- `watchlist/repository.py`：只增加 Brooks 表初始化、追加写入和只读查询；
- `commands/watchlist.py`：增加可预览的 Brooks 历史重建命令；正式写入继续要求显式 `--apply`；
- `web/routers/watchlist.py`：独立 `/a/brooks-second-leg` 路由和详情查询；
- `web/templates/ashare/brooks_second_leg.html`：独立桌面页面；
- `web/templates/ashare/watchlist_stock.html`：只在个股详情增加 Brooks 证据卡，不改变现有两法交集。

首轮上线不做行业 Brooks 聚合、不做指数 Brooks 状态、不改成交方法枚举、不引入个人自动仓位或通知。

## 10. 网站交付面

桌面端 1280–1600px 页面包括：

- 顶部四个状态数量：H1、touch25、第一腿、今日失效/完成；
- 单一状态筛选、日期、行业、代码/名称搜索、最低执行成交额（仅展示过滤）和“含暂停”开关；
- 主表固定列：状态、代码/名称、第一腿日期与天数、`move_atr`、路径效率、CLV、重叠、回调%、回调 ATR、当前价、前高、MM0.8、MM1.0、紧/结构失效距离、状态年龄；
- 行内展开：逐项阈值证据、H1/H2 日期、次日可成交状态、目标完成状态、暂停/移出原因；
- 明示文案：“这是结构化观察池，不是买点、收益预测或交易指令”。

风险字段优先显示百分比距离，而不是醒目的“止损价按钮”。登录用户若把候选加入个人观察或交易计划，才可在私有页面输入账户风险预算并试算数量。

## 11. 验收标准

### 纯计算

- 用研究工程的冻结样例验证 ATR、3/5/8 日第一腿及五个质量指标逐项一致；
- 同日多窗口只保留最长，40 个交易位置冷却边界分别测试 39/40；
- 零振幅 CLV=0.5，零并集 overlap=1.0；
- 未来一根 K 的变动不能改变此前任一决策日事实。

### 状态机

- 覆盖 S0、同日 touch25+H1、普通 S1→S2、完整回吐、H1 失败→H2、目标/失败同 K 歧义、15/20/30 日边界；
- H2 标签不改变排序层级；
- 次日开盘状态在 H1 决策日必须为 `PENDING`，不能泄漏下一日信息；
- ST、停牌、缺失和一字板均有明确原因码，不静默丢失。

### 数据与页面

- 新表迁移不改写现有两法事实，迁移前后现有 `/a/daily` 结果完全一致；
- 任意页面日期只读取 `<= query_date` 的行情、身份和方法事实；
- Brooks 页面无综合分、无自动买卖措辞、无“三法交集”；
- 1440px 桌面视口完成主表、筛选、展开证据和个股跳转检查。

### 最小回归

```bash
.venv/bin/pytest -q tests/test_watchlist_brooks.py tests/test_watchlist_repository.py tests/test_watchlist_web.py
.venv/bin/ruff check src tests
.venv/bin/mypy src
git diff --check
```

## 12. 实施顺序与上线闸门

1. 先实现纯计算与研究样例对齐；对不齐时停止，不进入数据库和页面。
2. 在临时本地数据库增加新表并验证追加、幂等拒绝和旧表零变化。
3. 接入独立页面，用本地历史真实数据做严格 as-of 重建；页面展示和统计不构成收益有效性验证。
4. 用户确认后再制定 ECS 显式迁移；备份、迁移、运行一次 Brooks 计算并核对 receipt 后才开放导航。
5. 不自动发布，不自动同步 GitHub，不恢复影子运行。

真正新增的 2026-08-27 之后数据只能用于前瞻监测冻结规则是否失效，不能继续调阈值后再称为独立验证。
