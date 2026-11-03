# X Register Web 管理系统

Web 控制台基于 FastAPI、React、Vite 和 Tailwind。管理后台保持完整，账号注册通过独立的 **XRegistration** 模块接入。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `backend.web` | 管理员登录、HTTP API、静态资源、任务协调 |
| `backend.x_registration` | XRegistration 稳定服务入口 |
| `backend.x_account_registration` | X.com 官方页面适配器与独立任务入口；注册编排复用 `backend.registration` |
| `backend.registration` | 注册编排、页面步骤、结果持久化 |
| `backend.automation` | Camoufox / CloakBrowser 生命周期 |
| `backend.mailbox` | 邮箱创建、收信与验证码解析 |
| `backend.integrations` | 代理、连通性和出口 IP |
| `backend.shared` | 路径与版本等基础能力 |

XRegistration 只保存账号邮箱和密码。注册完成确认基于页面跳转和注册状态，不读取或保存第三方登录凭据。

## 前端路由

- `/overview`：工作概览
- `/x-registration/new`：XRegistration 新建任务
- `/x-registration/runtime`：任务运行监控
- `/x-account-registration/new`：新建 X.com 账号注册任务
- `/x-account-registration/runtime`：X.com 注册运行监控
- `/accounts`：账号和失败记录
- `/accounts/exit-ips`：出口 IP 记录
- `/settings/registration`：注册与浏览器设置
- `/settings/mail`：邮箱服务
- `/settings/outlook`：OutlookEmail 邮箱池
- `/settings/config`：配置文件查看

旧的 `/registration/new` 和 `/registration/runtime` 会重定向到 XRegistration 路由。

## 主要 API

- `GET /api/stats`
- `GET /api/accounts`
- `GET /api/accounts/{id}`
- `POST /api/accounts/delete`
- `GET /api/accounts/{id}/failure-screenshot`
- `GET /api/config`
- `PUT /api/config`
- `GET /api/job`
- `POST /api/job/start`
- `POST /api/job/stop`
- `GET /api/job/logs`
- `GET /api/jobs`
- `GET /api/x-account/job`
- `GET /api/x-account/job/logs`
- `POST /api/x-account/job/start`
- `POST /api/x-account/job/stop`
- `POST /api/x-account/connectivity`
- `POST /api/browser/kill-all`
- `POST /api/connectivity`

## 管理员登录

首次访问时创建唯一管理员账号。密码哈希保存在 `data/web_auth.json`，不会写入 `config.json`。本机 HTTP 调试可设置：

```text
GROK_WEB_COOKIE_SECURE=0
```

## 构建前端

```bash
cd front
npm install
npm run build
```

生产静态文件输出到 `front/dist/`，FastAPI 自动托管。

X.com 注册固定单浏览器顺序执行。除页面控件定位外，它与 xAI/Grok 共用邮箱去重、验证码轮询与换邮箱重试、账号重试、浏览器重启、出口 IP、完成验证、失败截图和结果落库逻辑。若官方页面要求手机号、CAPTCHA 或 Arkose 人工挑战，系统会停止当前账号、记录失败原因并保留截图，不尝试绕过验证。
