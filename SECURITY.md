# Security

请不要在 Issue、Pull Request 或日志中提交以下内容：

- 飞书 App Secret、MiniMax/API Key 或访问令牌；
- 真实群 ID、成员 OpenID、成员名单和群消息；
- Base token、私有表格链接、SQLite 数据库或服务器配置。

生产配置应保存在已忽略的 `.env`、`config/group_profiles.json` 或服务器环境变量中。
若发现安全问题，请先私下联系维护者，不要公开附上可利用的真实凭据。
