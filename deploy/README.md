# ECS 部署资产

## 单入口发布

正式发布和远端候选演练统一使用：

```bash
# 默认安全模式：完整本地验证，ECS 上创建并验收临时候选，随后清理，不切换正式服务
scripts/release_ecs.sh --rehearse

# 新功能有额外登录后页面时，可重复指定烟测路径
scripts/release_ecs.sh --rehearse \
  --smoke-path /a/reading/al-brooks-pullback-trading

# 正式切换必须显式确认当前提交；脚本拒绝脏的已跟踪工作区、跳过测试或提交不匹配
scripts/release_ecs.sh --apply --confirm-commit "$(git rev-parse HEAD)"
```

发布器只归档已提交的 `HEAD`，禁止数据库、`.env`、日志、备份和个人数据进入归档。
默认使用 Cloud Assistant，不修改 SSH、UFW 或安全组。远端候选使用
`/etc/uv/uv.toml` 和持久化 `/var/cache/masterstock/uv`，依赖来自
`requirements-runtime-lock.txt`；Ruff、Mypy 和 Pytest 不安装到生产候选。

演练模式只读访问三份线上权威数据库，登录验收使用本次专属临时用户库；演练结束会
核对两份公共库完整指纹未变，并确认用户库 inode、设备、schema 与 quick_check 正常，
允许线上真实访问日志自然增长；随后删除精确候选和暂存目录。正式模式先创建
独立用户库备份，只接受已兼容的用户库 schema，再原子切换代码；启动失败自动恢复旧版。
数据库 schema 迁移仍是单独的高风险流程，不由普通代码发布器自动执行。

这些模板适用于单台 Ubuntu ECS 的 HTTPS 单入口部署：FastAPI 只监听
`127.0.0.1:8000`，Nginx 在 443 代理完整应用。80 端口仅保留 ACME 验证和向 HTTPS 的
重定向；不启用公网 8888 入口。三个 SQLite 数据库放在 `/var/lib/masterstock`，数据库
端口不对外暴露。

`deploy/nginx/masterstock.conf.example` 是域名 HTTPS 入口模板。它在 80 端口保留 ACME
验证并跳转到 443，443 代理完整应用，供登录、个人空间和 Agent API 使用。部署时必须
把示例域名替换为真实域名，并确认对应证书同时覆盖根域名和 `www`。
首次签发证书前先临时启用 `masterstock-acme-http.conf.example`，它只公开 ACME 验证
路径，其他 80 端口请求返回 404；证书签发成功后再用正式 HTTPS 模板替换它。
HTTPS 模板对动态请求设置了按来源 IP 的限速和并发上限，静态资源与健康检查不计入
动态请求限速；`robots.txt` 禁止搜索引擎收录这个个人研究站点。
基础部署需要创建：

- 系统账号 `masterstock`；
- `/opt/masterstock` 代码与项目虚拟环境；
- `/var/lib/masterstock` 及 `/var/backups/masterstock`，所有者为 `masterstock`；
- `/etc/masterstock/masterstock.env`，权限 `0600`，至少包含数据采集所需的密钥；
  仅使用 SSH 隧道通过本机 HTTP 登录时设置 `MASTERSTOCK_SECURE_COOKIES=0`；启用公网
  HTTPS 登录后改为 `1`。Agent CLI 使用 Bearer Token，通过 SSH 隧道访问回环 HTTP
  不依赖会话 Cookie，仍可继续使用。

访问日志匿名汇总与保留期使用用户库 schema v8。上线前先停止 Web，用
`scripts/manage_users.sh schema-migrate --apply --backup <未存在的备份路径>` 在 ECS
权威用户库上显式迁移，不得上传本地数据库覆盖。需要归属地时，将合法获取并
定期更新的 `GeoLite2-City.mmdb` 放在 `/var/lib/masterstock/`，并设置
`MASTERSTOCK_GEOIP_DATABASE=/var/lib/masterstock/GeoLite2-City.mmdb`。Nginx 必须覆写
`X-Real-IP` 与 `X-Forwarded-For`，`MASTERSTOCK_TRUSTED_PROXIES` 只列出 Nginx 到应用的
实际来源网段。该表包含个人信息，运维时应限制数据库访问权限并按实际合规
与分析需求设定保留期。默认保留 90 天原始明细；过期数据只保留不含 IP、用户和城市的
匿名日汇总。可通过 `MASTERSTOCK_ACCESS_LOG_RETENTION_DAYS` 调整，但不得低于实际合规
和安全调查所需期限。

安装 Nginx 后，把 HTTPS 模板复制到 `/etc/nginx/sites-available/masterstock-https` 并启用。
确认未启用 `masterstock-public-readonly`，且没有服务监听 8888。把 systemd Web 模板安装到
`/etc/systemd/system/masterstock-web.service`，并把访问日志维护的 service/timer 模板安装到
`/etc/systemd/system/`。维护任务默认 04:15 执行，排在 03:30 用户库备份之后；首次启用前
先手动运行一次不带 `--apply` 的 `scripts/manage_users.sh access-log-maintain` 核对待清理数量。
先运行 `nginx -t`，再执行 `systemctl daemon-reload` 并重启 Web 与 Nginx。切换后必须确认：

- `ss` 显示 FastAPI 仅监听 `127.0.0.1:8000`，Nginx 监听公网 `80` 和 `443`，且没有
  8888 监听；
- 公网 HTTPS 首页、行情、公开图表、登录、个人空间和 Agent API 正常；
- `http://真实域名` 跳转到 HTTPS，`https://真实域名/login` 和 `/api/v1/` 只经 443；
- `certbot renew --dry-run` 通过，证书续期定时器为 active；
- `masterstock-daily.timer`、`masterstock-backup.timer` 和
  `masterstock-access-log-maintenance.timer` 均为 active。

Certbot 使用 Webroot 续期时，把 `deploy/certbot/reload-nginx` 安装到
`/etc/letsencrypt/renewal-hooks/deploy/reload-nginx` 并设为 `0755`。这样证书文件更新后
会先验证 Nginx 配置，再平滑 reload 载入新证书。

首次备份必须手动运行 `masterstock-backup.service`，确认用户数据库备份通过 `quick_check`。

部署前使用 README 中的 `database-optimize` 生成公开数据库候选文件，并用 `database-validate` 对旧快照库逐日校验。只上传返回 `EQUIVALENT` 且通过 `PRAGMA quick_check` 的候选库；不要把本机的旧库回滚副本一同上传。

用户数据库恢复时先停止 Web，保留当前文件，对候选备份执行 `PRAGMA quick_check`，
替换后重启并验证登录和个人空间。公开数据库由云盘快照和重建流程保护，不进入每日
应用备份。模板不自动上传 OSS；跨设备备份应使用 ECS RAM 角色，不应在仓库或 env
文件中保存长期 AK。
