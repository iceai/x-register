# X Register

基于 FastAPI、React 与浏览器自动化的 Web 注册管理系统。项目保留完整管理后台，并提供独立的 **XRegistration**（xAI/Grok）和 **X.com 账号注册**模块。

两个注册模块都只保存账号邮箱与密码，不读取、保存或导出第三方登录凭据。

## 功能

- Web 管理后台：工作概览、账号管理、任务监控和系统设置
- XRegistration：注册任务、并发控制、实时日志、停止与异常浏览器清理
- X.com 账号注册：通过官方注册页创建账号；除页面控件外，完整复用 xAI/Grok 的邮箱、验证码、重试、浏览器和结果验证逻辑
- Camoufox（默认）和 CloakBrowser 两种浏览器后端
- Cloudflare、DuckMail / Mail.tm、YYDS、MailNest、OutlookEmail、CloudMail 邮箱来源
- 注册结果 SQLite 持久化，支持失败截图和关联文件清理
- 出口 IP 风险记录
- 首次访问创建唯一管理员账号
- Docker Compose 部署

## Docker 快速启动

```bash
git clone https://github.com/iceai/x-register.git
cd x-register
cp .env.example .env
docker compose build
docker compose up -d
```

访问：`http://服务器IP:8787`

```bash
docker compose ps
docker compose logs -f x-register
curl http://127.0.0.1:8787/api/health
```

## 本机运行

要求 Python 3.10+、Node.js 22+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch

cd front
npm install
npm run build
cd ..

cp config.example.json config.json
./start-web.sh
```

Windows：

```powershell
.venv\Scripts\python.exe -m backend.web.cli --host 127.0.0.1 --port 8787
```

## 主要配置

建议在“系统配置”中修改：

| 配置项 | 说明 |
| --- | --- |
| `email_provider` | 邮箱服务商 |
| `register_count` | 默认注册数量 |
| `register_workers` | 并发浏览器数量 |
| `account_interval` | 账号间隔，支持固定秒数或 `60-120` 区间 |
| `proxy` | 注册使用的 HTTP(S) 代理 |
| `browser_engine` | `camoufox` 或 `cloakbrowser` |
| `browser_headless` | 本机无头运行 |
| `browser_low_traffic_mode` | 静态资源缓存和低流量模式 |
| `outlookemail_disable_after_registration_success` | 注册成功后停用 OutlookEmail accounts 来源邮箱 |

完整模板见 [config.example.json](config.example.json)。

## 数据目录

```text
data/
├── config.json
├── web_auth.json
├── accounts/
│   ├── registration_results.sqlite3
│   ├── <email>.txt
│   └── x/<email>.txt
└── screenshots/registration-failures/
```

账号文件格式：

```text
email@example.com----password
```

## 项目结构

```text
backend/
  x_registration/   # XRegistration 对 Web 暴露的独立服务边界
  x_account_registration/ # X.com 账号注册流程与服务边界
  registration/     # 注册引擎、页面流程和结果仓储
  automation/       # 浏览器生命周期与页面适配
  mailbox/          # 邮箱服务
  integrations/     # 代理、连通性与出口 IP
  web/              # FastAPI 管理后台
front/               # React 管理界面
```

详细架构见 [WEB.md](WEB.md)，部署说明见 [DEPLOYMENT.md](DEPLOYMENT.md)。

X.com 注册与 xAI/Grok 共用同一注册编排引擎，只保留独立页面适配器，并固定单浏览器顺序执行。它不会绕过 CAPTCHA、Arkose 等人工安全挑战；站点要求手机号或人工验证时，该账号会以明确失败原因结束并保留现场截图。
