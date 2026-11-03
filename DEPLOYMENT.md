# X Register 部署说明

## Docker Compose

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

默认访问地址：`http://服务器IP:8787`。

```bash
docker compose ps
docker compose logs -f x-register
curl http://127.0.0.1:8787/api/health
```

运行数据挂载到宿主机 `data/`，日志挂载到 `logs/`。

## 初始化配置

首次启动会根据 `config.example.json` 创建 `data/config.json`。也可以提前准备：

```bash
mkdir -p data
cp config.example.json data/config.json
```

建议通过 Web 管理后台修改配置。

## 反向代理

服务默认监听容器内 `0.0.0.0:8787`。使用 HTTPS 反向代理时，将 `.env` 中的 Cookie 配置设为：

```text
GROK_WEB_COOKIE_SECURE=1
```

Nginx 示例：

```nginx
server {
    listen 443 ssl;
    server_name register.example.com;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## 代理

容器内配置 `127.0.0.1` 代理时，程序会按 Docker 环境映射到 `host.docker.internal`。宿主机代理需要监听 Docker 可访问的地址。

示例：

```json
{
  "proxy": "http://127.0.0.1:7890"
}
```

## OutlookEmail 可选服务

```bash
docker compose --profile outlookemail up -d
```

主服务内使用：

```text
http://outlook-email:5000
```

OutlookEmail 数据保存在 `outlookemail-data/`。

## 更新

```bash
git pull
docker compose build
docker compose up -d
```

构建前请备份 `data/`。注册结果和管理员登录数据都位于该目录。

## 故障排查

查看日志：

```bash
docker compose logs --tail=200 x-register
```

检查健康状态：

```bash
curl -i http://127.0.0.1:8787/api/health
```

如果浏览器异常残留，可在 xAI/Grok 或 X.com 注册运行监控页使用“终止全部浏览器”。

如果前端提示未构建：

```bash
cd front
npm install
npm run build
```

然后重启后端服务。
