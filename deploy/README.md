# ECS 部署资产

这些模板适用于单台 Ubuntu ECS 的双入口部署：FastAPI 只监听
`127.0.0.1:8000`，Nginx 在公网 `8888` 仅代理公开、只读页面。登录、个人空间、
`/api/me/` 和 `/api/v1/` Agent API 均不经过公网 HTTP；私人浏览器和 CLI 通过 SSH
隧道直连 FastAPI。三个 SQLite 数据库放在 `/var/lib/masterstock`，数据库端口不对外暴露。

`deploy/nginx/masterstock-public-readonly.conf.example` 是公网 HTTP 8888 只读入口模板；
`masterstock.conf.example` 是可与它同时启用的域名 HTTPS 入口模板。HTTPS 入口在 80
端口保留 ACME 验证并跳转到 443，443 代理完整应用，供登录、个人空间和 Agent API
使用。部署时必须把示例域名替换为真实域名，并确认对应证书同时覆盖根域名和 `www`。
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

访问日志使用用户库 schema v4。上线前先停止 Web，用
`scripts/manage_users.sh schema-migrate --apply --backup <未存在的备份路径>` 在 ECS
权威用户库上显式迁移，不得上传本地数据库覆盖。需要归属地时，将合法获取并
定期更新的 `GeoLite2-City.mmdb` 放在 `/var/lib/masterstock/`，并设置
`MASTERSTOCK_GEOIP_DATABASE=/var/lib/masterstock/GeoLite2-City.mmdb`。Nginx 必须覆写
`X-Real-IP` 与 `X-Forwarded-For`，`MASTERSTOCK_TRUSTED_PROXIES` 只列出 Nginx 到应用的
实际来源网段。该表包含个人信息，运维时应限制数据库访问权限并按实际合规
与分析需求设定保留期。

安装 Nginx 后，把公网只读模板复制到 `/etc/nginx/sites-available/masterstock-public-readonly`
并启用；需要 HTTPS 时，再把 HTTPS 模板复制到 `/etc/nginx/sites-available/masterstock-https`
并与 8888 模板同时启用。把 systemd Web 模板安装到
`/etc/systemd/system/masterstock-web.service`。
先运行 `nginx -t`，再执行 `systemctl daemon-reload` 并重启 Web 与 Nginx。切换后必须确认：

- `ss` 仅显示 FastAPI 监听 `127.0.0.1:8000`，Nginx 监听公网 `8888`；
- 启用 HTTPS 后，Nginx 额外监听公网 `80` 和 `443`，FastAPI 仍不得监听公网；
- 公网首页、行情和公开图表正常；公网 `/login`、`/account/`、`/api/me/`、
  `/api/v1/` 均返回 `404`，公网 POST 返回 `403`；
- SSH `-L 127.0.0.1:8888:127.0.0.1:8000` 后，本机登录和 Agent API 正常；
- `http://真实域名` 跳转到 HTTPS，`https://真实域名/login` 和 `/api/v1/` 只经 443；
- `certbot renew --dry-run` 通过，证书续期定时器为 active；
- `masterstock-daily.timer` 和 `masterstock-backup.timer` 仍为 active。

Certbot 使用 Webroot 续期时，把 `deploy/certbot/reload-nginx` 安装到
`/etc/letsencrypt/renewal-hooks/deploy/reload-nginx` 并设为 `0755`。这样证书文件更新后
会先验证 Nginx 配置，再平滑 reload 载入新证书。

首次备份必须手动运行 `masterstock-backup.service`，确认用户数据库备份通过 `quick_check`。

部署前使用 README 中的 `database-optimize` 生成公开数据库候选文件，并用 `database-validate` 对旧快照库逐日校验。只上传返回 `EQUIVALENT` 且通过 `PRAGMA quick_check` 的候选库；不要把本机的旧库回滚副本一同上传。

用户数据库恢复时先停止 Web，保留当前文件，对候选备份执行 `PRAGMA quick_check`，
替换后重启并验证登录和个人空间。公开数据库由云盘快照和重建流程保护，不进入每日
应用备份。模板不自动上传 OSS；跨设备备份应使用 ECS RAM 角色，不应在仓库或 env
文件中保存长期 AK。
