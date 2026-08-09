# 龙场悟道 · 大师选股

品牌：龙场悟道｜系统名称：大师选股

“大师选股”是一个面向 A 股人工研究的网站。程序只用 Stan Weinstein 与 Mark Minervini 两套独立方法缩小观察范围，最终判断仍由用户完成。

系统不寻找唯一的“核心目标”，不合成大师总分，也不输出自动交易指令。

这是“龙场悟道”的开源版本，采用 [MIT License](LICENSE)。仓库不包含 Tushare token、运行数据库、行情缓存、个人成交记录或原始参考资料。

> **在线试用：** [http://47.110.74.48:8888](http://47.110.74.48:8888)
> 公开的行情、指数、行业和方法事实无需登录即可浏览。该地址当前为 HTTP 直连，请勿在不可信网络中输入敏感密码。

## 本版更新

- 增加 Agent API 与远程 CLI：用户在网站生成最小权限 Token，Agent 可先预检再以幂等、原子批次录入实际成交，并按成交 ID 更新 BUY 止损。
- 增加“交易复盘”页面：记录实际成交、持仓、已实现盈亏及描述性统计；方法事实会按成交日快照保存，不能改写当时观察结论。
- 增加 `market-backfill`：仅补齐本地已有原始日线日期缺失的复权因子、市值指标和四指数数据；先输出计划，只有显式 `--apply` 才写入。
- 优化观察池、指数、行业与个股证据页的桌面和窄屏阅读体验。

## 当前产品

| 对象 | 方法 | 系统输出 |
|---|---|---|
| 沪深300、中证1000、创业板指、科创50 | Weinstein | Stage 1/2/3/4、转换期或数据不足 |
| 沪深300、中证1000、创业板指、科创50 | Minervini | 仅判断当前是否处于 Stage 2：是、否或数据不足 |
| 沪深 A 股普通股票 | Weinstein | 当前是否处于明确 Stage 2，以及进入、持续、退出状态 |
| 沪深 A 股普通股票 | Minervini | 当前是否通过趋势模板，以及进入、持续、退出状态 |
| 申万三级行业 | SW2021-L3 | 两种方法的入选数量、交集、宽度、当日变化及成分股等权代理K线 |
| 个人观察 | 用户 | 观察中、重点、已归档及私有备注；未加入即无记录 |
| 交易复盘 | 用户 | 实际成交、持仓、已实现结果与描述性统计 |

Weinstein 与 Minervini 的事实始终独立保存。“两法同时符合”只是集合交集，不是新策略或综合评分。行业观察使用同一当日非 ST 基础池，只用于发现入选股票的聚集现象，不评选“主线”，也不改变任何个股的方法结论。

首页用“新进/重进、持续符合、退出/中断、行业聚集”四个页签分类展示；六个统计卡片均可点击进入对应筛选结果。股票名称链接 TradingView 中文版K线，代码链接同花顺，“详情”进入站内方法证据页；行业名称进入站内行业K线页。

### 观察资格与可交易性

- 大师观察池从证券身份有效期历史中取所选日期当时的状态，默认排除 `ST`、`*ST`；历史页面不得使用未来日期的风险标识。
- 总市值不参与 Weinstein 或 Minervini 判定。网站提供“不限、30亿、50亿、100亿”四档可选下限，默认不限。
- 总市值低于50亿元只标记为“小市值”，不会从方法事实中删除；市值门槛只改变页面显示范围。
- 表格和个股页同时展示当日总市值、流通市值及最近20个交易日成交额中位数，供人工判断流动性，不生成流动性评分。

## 方法口径

### Weinstein

- 四指数和个股均使用已经结束的完整周线。
- 30周均线按周线序列直接计算；它与约150个交易日的观察长度接近，但系统不使用日线150日均线替代 Weinstein 30周线。
- 指数只作市场阶段诊断，不参与个股评分，不决定个股能否进入观察池。
- 证据不足或阶段无法唯一判定时输出 `UNKNOWN` 或转换期，不强行归类。

### Minervini

- 个股使用日线趋势模板，包括50日、150日、200日均线关系、200日均线方向、52周价格位置及横截面相对强度。
- 四指数使用相同的日线价格趋势条件，但只输出“是否处于 Stage 2”，不定义其他阶段。
- 指数不使用个股横截面相对强度排名，也不把四个指数相互排名作为替代。
- 个股通过模板只表示值得进入人工观察池，不等于出现买点。指数结果只作市场趋势背景。

本系统不包含 O’Neil、VCP、ETF、北极星、综合评分、自动买点、回测选优或离线报告模块。

## 每日闭环

交易日上海时间 17:10，系统可按下面的单向流程运行，以避开 Tushare 17:00 前当日数据尚未完整的窗口：

```text
Tushare交易日确认
  → 股票日线、复权因子、市值、证券身份、四指数日线
  → 日期与覆盖率质量门
  → 单一SQLite事务写入行情
  → Weinstein与Minervini计算
  → 网站直接读取当天最新结果
```

任何一项日期错误、完整股票少于4500只、覆盖率低于95%，或四指数任一缺失，整批采集都会拒绝入库，方法计算不会启动，网站继续展示上一次成功结果。重复执行同一交易日会复用成功凭证。前复权因子改变时，只重算受影响股票的既有前复权序列；原始行情和因子仍保留，可重建派生结果。

采集与方法计算凭证可在 `/a/runs` 一起查看。

## 数据边界

- `data/market.sqlite3`：采集环节唯一写入的市场输入库，包含原始及前复权股票日线、复权因子、指数日线、按日总市值/流通市值、证券身份和采集凭证。采集完成后，方法计算只读该库。
- `data/master_watchlist.sqlite3`：公开的方法事实、状态转移、行业观察和运行凭证。
- `data/users.sqlite3`：账号、服务端会话以及按 `user_id` 隔离的人工复核、成交和图表标注；权限应为 `0600`。
- `data/backups/`：清理前代码恢复包和带日期的数据库备份；恢复前必须核对对应 `.sha256` 文件和备份日期。

申万三级行业映射已经以有效期历史写入 `master_watchlist.sqlite3`，运行网站不再依赖旧 `research.sqlite3`。旧研究数据库、输出目录和行情缓存已移除。

事实来源分为：

- `OBSERVED`：在对应交易日收盘后，基于当时本地输入实际生成。
- `RECONSTRUCTED`：事后使用现有历史行情重建，不冒充当日曾经保存的结果。

缺失输入必须保留为未知状态。不得跨日期拼接、使用未来数据或用近似值伪造完整证据。

行业K线以所选日期当时有效的申万成员和成分股前复权 OHLC 日线等权合成，固定起始点为 1000。它不是申万官方行业指数，且回看历史时含有当期成员口径，只作人工趋势观察，不作方法有效性证据。

## 项目结构

```text
src/master_stock_selector/
├── commands/       # daily、market-backfill、watchlist 与 web CLI 入口
├── watchlist/      # Tushare采集、两种方法、行业观察及 SQLite 仓储
└── web/            # FastAPI 路由、模板和样式
scripts/
├── manage_users.sh
├── backup_databases.sh
├── install_daily_schedule.sh
├── run_master_watchlist.sh
├── start_web.sh
├── stop_web.sh
└── uninstall_daily_schedule.sh
tests/              # 当前产品合同测试
deploy/             # ECS systemd、Nginx 与备份模板
data/
├── market.sqlite3
├── master_watchlist.sqlite3
├── users.sqlite3
└── backups/
```

`data/`、日志、运行缓存和本地 `参考资料/` 仅保留在使用者机器上，均不属于开源仓库内容。

## 安装

要求 Python 3.11 或更高版本。所有 Python 命令必须使用项目虚拟环境，禁止使用系统 Python。

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
uv pip install --python .venv/bin/python -e .
```

查看现存命令：

```bash
.venv/bin/masterstock --help
.venv/bin/masterstock agent --help
.venv/bin/masterstock daily --help
.venv/bin/masterstock market-backfill --help
.venv/bin/masterstock watchlist --help
.venv/bin/masterstock web --help
```

## 配置采集

创建本地配置并填入自己的 Tushare Pro token；`.env` 已被 Git 忽略，且应仅允许当前用户读取：

```bash
cp .env.example .env
chmod 600 .env
```

至少填写：

```dotenv
TUSHARE_TOKEN=你的token
```

CLI 会从项目工作目录读取 `.env`，不会输出 token。

## Agent API 与交易 CLI

登录网站后进入“个人工作区 → 账户设置 → Agent Token”，创建带 `trades:read`、
`trades:write` 权限的 Token。建议每个 Agent 或设备单独创建一枚 Token：Token 只显示一次，
服务端仅保存摘要，设备遗失或凭据疑似泄露时可单独撤销，不影响网站密码和其他 Agent。
当前 ECS 采用双入口隔离：公网 `8888` 只提供公开只读页面，Agent API 和登录只允许通过
SSH 隧道访问回环地址。CLI 只允许本机地址使用 HTTP；若未来公开 Agent API，必须使用 HTTPS。

| 权限 | 允许的操作 | 建议场景 |
|---|---|---|
| `trades:read` | 验证账户、查询成交及提交验收结果 | 只读核验 Agent |
| `trades:write` | 预检、批量录入成交、更新既有 BUY 止损 | 交易录入 Agent；通常同时保留读取权限 |

Token 不支持命令行参数，避免进入 Shell 历史。可通过进程环境或标准输入提供：

```bash
ssh -N -T \
  -L 127.0.0.1:8888:127.0.0.1:8000 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -i ~/.ssh/id_ed25519 root@47.110.74.48

export MASTERSTOCK_AGENT_URL=http://127.0.0.1:8888
read -s MASTERSTOCK_AGENT_TOKEN
export MASTERSTOCK_AGENT_TOKEN
.venv/bin/masterstock agent me
```

也可以让 CLI 从标准输入读取 Token；不要把 Token 拼在命令参数、脚本、聊天、截图或日志中：

```bash
printf '%s\n' "$MASTERSTOCK_AGENT_TOKEN" | \
  .venv/bin/masterstock agent --token-stdin me
```

交易 JSON 可以是数组，也可以是包含 `trades` 数组的对象：

```json
{
  "trades": [
    {
      "client_id": "screenshot-20260809-1",
      "source_ref": "broker-order-20260806-001",
      "symbol": "600988.SH",
      "traded_on": "2026-08-06",
      "traded_at": "09:30:24",
      "side": "BUY",
      "quantity": 1000,
      "price": 43.18
    }
  ]
}
```

先预检，再显式提交：

```bash
.venv/bin/masterstock agent trades validate trades.json
.venv/bin/masterstock agent trades import trades.json --commit
.venv/bin/masterstock agent trades list --symbol 600988.SH
.venv/bin/masterstock agent trades set-stop EXECUTION_ID 37.56 \
  --expected-revision 1 --commit
```

`import --commit` 默认根据规范化 JSON 生成稳定的幂等键，也可显式传入
`--idempotency-key`。服务端按成交时间模拟 FIFO，整批存在无效字段或超卖时不写入任何
记录；同一 API 请求由 `Idempotency-Key` 去重，成交优先由调用方稳定的 `source_ref`
识别，缺少时身份包含完整 `traded_at`。完全重复的成交返回 `DUPLICATE`，不会再次创建。
止损接口只接受既有 BUY 的 `execution_id` 和当前整数 `revision`，版本冲突返回 `409`，
不创建替代 BUY 或 SELL。仅有 `trades:write` 的 Token 只获得编号、状态和版本等最小回执；
读取完整成交必须具有 `trades:read`。

常见故障边界：

- `401`：Token 无效、过期或已撤销；回到网站重新创建，不要重试旧 Token。
- `403`：Token 缺少对应权限；创建最小充分权限的新 Token，不修改网站密码。
- `422`：字段、重复、超卖或止损规则未通过；修正输入后重新预检，服务端不会部分写入。
- 公网 HTTP 地址：CLI 会拒绝连接；先建立 SSH 隧道并使用 `http://127.0.0.1:8888`，
  不要关闭这一保护。

## 运行每日闭环

真实交易日收盘且上海时间已过15:30后运行：

```bash
scripts/run_master_watchlist.sh
```

脚本现在执行“采集 → 质量门 → 行情事务写入 → 方法计算”完整闭环。写入命令必须显式包含 `--apply`；脚本已经负责添加该参数并使用运行锁，避免两个任务同时写库。周末或交易所休市日会安全跳过。

直接调用同一闭环：

```bash
.venv/bin/masterstock daily --apply
```

历史重建必须写入单独的新数据库，并明确标记为 `RECONSTRUCTED`：

```bash
MASTERSTOCK_WATCHLIST_DATABASE=/tmp/rebuilt-watchlist.sqlite3 \
MASTERSTOCK_WATCHLIST_DATE=2026-07-31 \
scripts/run_master_watchlist.sh --reconstruct-from 2025-09-15
```

历史重建仍只读取本地行情，不触发 Tushare 采集，也不冒充当日观察结果。

## 补齐历史行情缺口

当本地已有原始股票日线，但缺少复权因子、市值指标或四指数日线时，可先查看补采计划：

```bash
.venv/bin/masterstock market-backfill --from 2026-01-01
```

确认计划后才允许写入：

```bash
.venv/bin/masterstock market-backfill --from 2026-01-01 --apply
```

该命令只处理本地已有原始日线覆盖的交易日，并对每项补采执行日期、股票覆盖和四指数质量门；完成后会写入不可变凭证。它不用于回测、不重写观察池事实，也不会把事后数据标记为 `OBSERVED`。

## 启用交易日定时运行

确认 `.env` 中已有 token、手工闭环至少成功一次后，再安装当前用户的 macOS LaunchAgent：

```bash
scripts/install_daily_schedule.sh
```

它在周一至周五 17:10 启动；脚本仍会用 Tushare 交易日历判断节假日。日志写入 `logs/daily.log` 和 `logs/daily-error.log`。停用：

```bash
scripts/uninstall_daily_schedule.sh
```

## 启停网站

```bash
scripts/start_web.sh
scripts/stop_web.sh
scripts/restart_web.sh
```

默认地址：[http://127.0.0.1:8000/a/daily](http://127.0.0.1:8000/a/daily)

网站使用三个职责分离的 SQLite 数据库：`market.sqlite3` 保存公开行情，
`master_watchlist.sqlite3` 保存公开方法事实，`users.sqlite3` 保存账号、会话以及每个
用户自己的观察列表、备注、K 线绘图、买卖点、成交和复盘。一个用户就是一个独立租户，不支持团队共享。

Web 启动只校验用户库结构，不会建表或迁移。首次使用前先显式检查和创建结构，再创建账号
（密码通过终端安全提示输入，不进入命令历史）：

```bash
scripts/manage_users.sh schema-check
scripts/manage_users.sh schema-migrate
scripts/manage_users.sh schema-migrate --apply
scripts/manage_users.sh create your-name --display-name "显示名称"
scripts/manage_users.sh list
```

迁移现有用户库时，`--apply` 必须同时给出一个尚不存在的备份路径；命令先通过 SQLite
Backup API 生成备份并执行 `quick_check`，再迁移并核对既有表行数：

```bash
scripts/manage_users.sh schema-migrate --apply \
  --backup /var/backups/masterstock/users-before-schema-v2.sqlite3
```

线上执行前必须停止 Web；只能迁移 ECS 现有 `users.sqlite3`，不得上传本机用户库替换。

禁用账号或重置密码：

```bash
scripts/manage_users.sh disable your-name
scripts/manage_users.sh reset-password your-name
```

修改密码、管理员重置密码或禁用账号都会在同一用户库事务中撤销该账号的全部 Session
和 Agent Token。Token 默认有效 30 天、最长 90 天。

公开行情、指数、行业和方法证据无需登录；`/a/focus`、`/a/review`、人工备注、成交、
止损和图表标注必须登录，并以服务端会话中的 `user_id` 隔离。当前双入口部署只允许通过
SSH 隧道登录，设置 `MASTERSTOCK_SECURE_COOKIES=0`；公网 Nginx 必须阻断全部登录和个人
接口。若未来允许公网登录，必须改用 HTTPS 并设置 `MASTERSTOCK_SECURE_COOKIES=1`。

常用页面：

- `/a/daily`：大师观察池
- `/a/indices`：四指数 Weinstein 完整阶段与 Minervini Stage 2 是/否
- `/a/industries`：申万三级行业聚集观察
- `/a/industries/{industry_code}/chart`：申万三级行业成分股等权代理K线
- `/a/observations`：我的观察（观察中 / 重点 / 已归档）
- `/a/review`：实际成交与交易复盘
- `/a/stocks/{symbol}`：个股方法证据与人工备注
- `/a/runs`：观察池运行凭证

程序还提供只读 JSON 接口 `/api/a/watchlist/{date}`、`/api/a/indices/{date}`、`/api/a/industries/{date}`，以及进程级健康检查 `/healthz`。公开图表接口只返回行情和公开指标；个人成交与画线通过登录后的 `/api/me/` 接口单独读取，避免进入公开缓存。观察池页面和接口可使用 `min_cap=0|30|50|100` 选择总市值下限。

数据库、监听地址、Tushare限速和最低股票数可以通过 `.env.example` 中列出的环境变量覆盖。

在线数据库不能用普通文件复制备份。项目使用 SQLite Backup API 只备份不可重建的
用户数据库：

```bash
MASTERSTOCK_BACKUP_DIR=/var/backups/masterstock scripts/backup_databases.sh
```

脚本会对 `users.sqlite3` 执行 `quick_check`、生成一致性备份、再次校验备份，并按
`MASTERSTOCK_BACKUP_RETENTION_DAYS`（默认 7 天）清理过期文件。公开数据库依靠云盘
快照和重建流程保护，不进入每日应用备份。异地 OSS 上传由部署环境单独配置，不在
仓库中保存访问密钥。

如需保留旧版单用户私有数据，先执行只读预检，再明确应用：

```bash
scripts/manage_users.sh migrate-legacy \
  --source data/master_watchlist.sqlite3 --username your-name
scripts/manage_users.sh migrate-legacy \
  --source data/master_watchlist.sqlite3 --username your-name --apply
```

迁移不会删除或修改旧数据库。

### 公开数据库精简

`market.sqlite3` 和 `master_watchlist.sqlite3` 中的证券身份、名称、ST 状态及申万行业归属使用有效期历史，只在属性变化时新增记录。日常采集和观察池计算会继续写入这些历史表；历史页面按查询日期还原当时状态。

从旧快照库生成独立候选库，然后逐日校验等价性：

```bash
.venv/bin/masterstock database-optimize \
  --market-source data/market.sqlite3 \
  --market-target data/market_v2.sqlite3 \
  --watchlist-source data/master_watchlist.sqlite3 \
  --watchlist-target data/master_watchlist_v2.sqlite3 \
  --apply

.venv/bin/masterstock database-validate \
  --market-source data/market.sqlite3 \
  --market-target data/market_v2.sqlite3 \
  --watchlist-source data/master_watchlist.sqlite3 \
  --watchlist-target data/master_watchlist_v2.sqlite3
```

只有校验返回 `EQUIVALENT` 才可在停止 Web 和日常任务后切换文件。旧库应先改名保留为回滚副本；新库通过 `PRAGMA quick_check`、`/healthz` 及主要页面检查后，再决定是否删除旧库。

### ECS 部署

`deploy/systemd/` 提供 Web 常驻进程、交易日采集定时器和用户数据库备份定时器；
`deploy/nginx/masterstock-public-readonly.conf.example` 提供公网 `8888` 只读入口，
完整应用只监听回环地址并通过 SSH 隧道访问。域名和 HTTPS 示例仍保留供未来使用。
安装、权限、首次备份和恢复检查见 [`deploy/README.md`](deploy/README.md)。

### 开发、ECS 与 GitHub 同步流程

1. 所有功能先在本地开发，使用项目虚拟环境完成受影响测试和必要的页面验证。
2. 本地验证通过不代表自动发布。只有在收到明确的同步或部署指令后，才把已验证的代码与配置同步到 ECS。
3. ECS 上的 `market.sqlite3`、`master_watchlist.sqlite3` 和 `users.sqlite3` 是线上权威数据。日常部署不再从本地上传或覆盖这三个数据库。
4. 如代码改动需要数据库结构迁移，必须先备份 ECS 用户库，再在 ECS 数据上执行明确的迁移和回滚检查；不得用本地数据库替换。
5. ECS 只部署本机已提交版本；验收通过后仅在收到明确要求时推送 GitHub。运行数据、密钥、会话和个人数据不进入 GitHub。

## 验证

```bash
.venv/bin/ruff check src tests
.venv/bin/mypy src/master_stock_selector
.venv/bin/python -m pytest -q
bash -n scripts/run_master_watchlist.sh scripts/install_daily_schedule.sh \
  scripts/uninstall_daily_schedule.sh scripts/start_web.sh scripts/stop_web.sh \
  scripts/restart_web.sh
git diff --check
```

测试通过只证明当前代码、路由和数据合同在机械层面正确，不代表 Weinstein 或 Minervini 方法已经在 A 股获得正的历史风险收益期望，也不构成投资建议。
