# ECS 部署资产

这些模板适用于单台 Ubuntu ECS：FastAPI 监听 `0.0.0.0:8888`，三个
SQLite 数据库放在 `/var/lib/masterstock`。安全组只需放行 TCP 8888，
数据库端口不对外暴露。当前为直连 HTTP；未配置 HTTPS 时，不应在不可信网络中输入密码。

如后续改用 HTTPS，再替换 Nginx 模板中的域名和证书路径。基础部署需要创建：

- 系统账号 `masterstock`；
- `/opt/masterstock` 代码与项目虚拟环境；
- `/var/lib/masterstock` 及 `/var/backups/masterstock`，所有者为 `masterstock`；
- `/etc/masterstock/masterstock.env`，权限 `0600`，至少包含数据采集所需的密钥；
  直连 HTTP 时设置 `MASTERSTOCK_SECURE_COOKIES=0`；切换到 HTTPS 后改为 `1`。

安装模板后执行 `systemctl daemon-reload`，先启动 Web 并检查 `/healthz`，再启用
`masterstock-daily.timer` 和 `masterstock-backup.timer`。首次备份必须手动运行
`masterstock-backup.service`，确认用户数据库备份通过 `quick_check`。

部署前使用 README 中的 `database-optimize` 生成公开数据库候选文件，并用 `database-validate` 对旧快照库逐日校验。只上传返回 `EQUIVALENT` 且通过 `PRAGMA quick_check` 的候选库；不要把本机的旧库回滚副本一同上传。

用户数据库恢复时先停止 Web，保留当前文件，对候选备份执行 `PRAGMA quick_check`，
替换后重启并验证登录和个人空间。公开数据库由云盘快照和重建流程保护，不进入每日
应用备份。模板不自动上传 OSS；跨设备备份应使用 ECS RAM 角色，不应在仓库或 env
文件中保存长期 AK。
