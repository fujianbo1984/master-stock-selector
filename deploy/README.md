# ECS 部署资产

这些模板适用于单台 Ubuntu ECS 的双入口部署：FastAPI 只监听
`127.0.0.1:8000`，Nginx 在公网 `8888` 仅代理公开、只读页面。登录、个人空间、
`/api/me/` 和 `/api/v1/` Agent API 均不经过公网 HTTP；私人浏览器和 CLI 通过 SSH
隧道直连 FastAPI。三个 SQLite 数据库放在 `/var/lib/masterstock`，数据库端口不对外暴露。

`deploy/nginx/masterstock-public-readonly.conf.example` 是当前双入口模板；
`masterstock.conf.example` 保留为将来启用域名和 HTTPS 时的示例，不能同时原样启用。
基础部署需要创建：

- 系统账号 `masterstock`；
- `/opt/masterstock` 代码与项目虚拟环境；
- `/var/lib/masterstock` 及 `/var/backups/masterstock`，所有者为 `masterstock`；
- `/etc/masterstock/masterstock.env`，权限 `0600`，至少包含数据采集所需的密钥；
  SSH 隧道通过本机 HTTP 访问时设置 `MASTERSTOCK_SECURE_COOKIES=0`；将来完整切换到
  HTTPS 后改为 `1`。

安装 Nginx 后，把公网只读模板复制到 `/etc/nginx/sites-available/masterstock-public-readonly`
并启用；把 systemd Web 模板安装到 `/etc/systemd/system/masterstock-web.service`。
先运行 `nginx -t`，再执行 `systemctl daemon-reload` 并重启 Web 与 Nginx。切换后必须确认：

- `ss` 仅显示 FastAPI 监听 `127.0.0.1:8000`，Nginx 监听公网 `8888`；
- 公网首页、行情和公开图表正常；公网 `/login`、`/account/`、`/api/me/`、
  `/api/v1/` 均返回 `404`，公网 POST 返回 `403`；
- SSH `-L 127.0.0.1:8888:127.0.0.1:8000` 后，本机登录和 Agent API 正常；
- `masterstock-daily.timer` 和 `masterstock-backup.timer` 仍为 active。

首次备份必须手动运行 `masterstock-backup.service`，确认用户数据库备份通过 `quick_check`。

部署前使用 README 中的 `database-optimize` 生成公开数据库候选文件，并用 `database-validate` 对旧快照库逐日校验。只上传返回 `EQUIVALENT` 且通过 `PRAGMA quick_check` 的候选库；不要把本机的旧库回滚副本一同上传。

用户数据库恢复时先停止 Web，保留当前文件，对候选备份执行 `PRAGMA quick_check`，
替换后重启并验证登录和个人空间。公开数据库由云盘快照和重建流程保护，不进入每日
应用备份。模板不自动上传 OSS；跨设备备份应使用 ECS RAM 角色，不应在仓库或 env
文件中保存长期 AK。
