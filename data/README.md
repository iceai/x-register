# 运行数据目录

此目录只存放 X Register 运行数据，不存放前后端源码。

- `accounts/`：账号文件和 SQLite 注册结果库。
- `screenshots/registration-failures/`：注册失败现场截图。
- `browser-cache/`：低流量模式静态资源缓存。
- `config.json`：运行配置。
- `web_auth.json`：唯一管理员的哈希认证信息。

XRegistration 账号文件只包含邮箱和密码，不保存其他登录凭据。

除本说明文件外，`data/` 内容均已由 `.gitignore` 忽略。
