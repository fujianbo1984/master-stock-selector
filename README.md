# 龙场悟道 · 大师选股

品牌：龙场悟道｜系统名称：大师选股

“大师选股”是一个面向 A 股人工研究的网站。它使用 **Stan Weinstein** 与 **Mark Minervini** 两套彼此独立的方法缩小观察范围；最终的研究、重点关注与放弃均由用户决定。

本项目采用 [MIT License](LICENSE)。开源仓库不包含行情数据库、Tushare token、个人观察与成交记录、会话或原始参考资料。

> **在线试用：** [http://47.110.74.48:8888](http://47.110.74.48:8888)
>
> 该地址当前为 HTTP 直连，仅用于浏览公开行情与方法事实；请勿在不可信网络输入密码或其他敏感信息。

## 产品边界

- 四个指数（沪深300、中证1000、创业板指、科创50）：Weinstein 周线阶段，以及 Minervini 的日线 Stage 2 是/否。
- 沪深 A 股普通股票：Weinstein 完整周 Stage 2 与正式状态变化，以及每日收盘后的周内投影预进入/预退出；Minervini 趋势模板与状态变化。
- 申万三级行业：两套方法的入选数量、交集、宽度与成分股等权代理 K 线，仅供人工观察。
- 登录用户：隔离的观察、备注、成交、止损与交易复盘。

系统不合成跨方法总分、不预测“核心目标”、不生成自动买卖指令；O'Neil、VCP、ETF、回测选优与离线报告不属于当前产品。

## 方法与数据口径

| 方法 | 使用对象 | 输出边界 |
| --- | --- | --- |
| Weinstein | 指数、个股 | 已完成周线上的 Stage 及正式状态变化；个股额外显示“若本周今日收盘”的独立周内投影 |
| Minervini | 指数、个股 | 日线趋势条件；指数只判断 Stage 2，个股判断趋势模板 |

个股的两类事实独立计算与保存。“两法同时符合”仅表示两个集合的交集，并不是第三种策略或综合评分。指数只作市场背景，不决定个股是否入选。

Weinstein 周内投影用本周截至查询日的 OHLCV 构造临时周K线，并与前一交易日投影比较生成预进入、继续预符合和预退出。它不改写完整周事实；只有交易日历确认的当周最后一个交易日才转为正式周线结论。

市场输入和方法事实必须具有同一交易日的可追溯性；数据不足时系统保留未知状态，不能以未来数据、跨日期拼接或近似值补全。行业代理 K 线并非申万官方行业指数，也不构成方法有效性证据。

## 快速开始

要求 Python 3.11+。所有 Python 命令使用项目虚拟环境：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
uv pip install --python .venv/bin/python -e .

.venv/bin/masterstock --help
```

创建本地配置：

```bash
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写自己的 `TUSHARE_TOKEN`。不要把 token、密码、Cookie 或会话写入命令行、脚本、截图或 Git。

首次使用个人工作区前，先在目标用户数据库上明确检查并迁移结构：

```bash
scripts/manage_users.sh schema-check
scripts/manage_users.sh schema-migrate
scripts/manage_users.sh schema-migrate --apply --backup /path/to/users-before-migration.sqlite3
scripts/manage_users.sh create your-name --display-name "显示名称"
```

启动本地网站：

```bash
scripts/start_web.sh
```

默认地址为 <http://127.0.0.1:8000/a/daily>。常用页面包括：

- `/a/daily`：大师观察池
- `/a/indices`：四指数方法事实
- `/a/industries`：行业聚集观察
- `/a/stocks/{symbol}`：个股证据与 K 线
- `/a/observations`、`/a/review`：登录用户的观察与交易复盘
- `/a/runs`：运行凭证

## 日常数据闭环

收盘后执行：

```bash
scripts/run_master_watchlist.sh
```

该命令执行“交易日确认 → 行情采集 → 质量门 → 单一事务写入 → 方法计算”。写库必须显式授权；脚本负责传递 `--apply` 并使用运行锁。日期错误、股票覆盖率不足或缺少任一指数时，整批数据拒绝入库，网站继续展示上一批成功结果。

补齐已有本地原始日线的历史缺口时，先预览，再显式写入：

```bash
.venv/bin/masterstock market-backfill --from 2026-01-01
.venv/bin/masterstock market-backfill --from 2026-01-01 --apply
```

它只补齐本地已有原始日线覆盖日期的复权因子、市值与指数数据，不重写既有方法事实，也不会把事后数据标为 `OBSERVED`。

## 个人数据与 Agent API

公开行情、指数、行业及方法事实无需登录。观察、备注、图表标注、成交与止损只属于登录用户，并按 `user_id` 隔离。

Agent Token 由用户在“账户设置”中创建，建议一台设备或一个 Agent 一枚。Token 只显示一次，服务端仅保存摘要；`trades:read` 用于查询，`trades:write` 用于预检、批量录入成交和更新既有 BUY 止损。

```bash
export MASTERSTOCK_AGENT_URL=http://127.0.0.1:8888
read -s MASTERSTOCK_AGENT_TOKEN
export MASTERSTOCK_AGENT_TOKEN
.venv/bin/masterstock agent me
.venv/bin/masterstock agent trades validate trades.json
.venv/bin/masterstock agent trades import trades.json --commit
```

Agent CLI 拒绝普通公网 HTTP 地址；需要远程使用时，应通过受控的回环 SSH 隧道或 HTTPS 部署访问。Token 不支持作为命令行参数传入。

## 数据、部署与 GitHub 边界

运行时使用三个本地 SQLite 数据库：市场输入、公开方法事实和用户私有数据。它们及其备份、日志、缓存、`.env`、原始参考资料均被 Git 忽略，绝不可提交。

部署模板位于 [`deploy/`](deploy/)，详细操作见 [`deploy/README.md`](deploy/README.md)。模板不是线上状态声明：部署、数据库迁移与公网验收须在目标环境单独执行。ECS 上的运行数据库是线上权威数据，日常同步不得用本地数据库覆盖。

本地验证通过后不会自动部署或发布。只有在获得明确授权后，才可同步纯代码与配置；GitHub 仅保存已经验证的代码、模板和文档。

## 验证

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
git diff --check
```

这些检查只验证代码与数据合同的机械正确性，不构成页面视觉验收、ECS 运行验收或投资建议。
