# 抖音无水印解析（DTK）部署

> 2026-11-18 实际状态：腾讯云已有 DTK v5（`/home/ubuntu/douyin-pwa/dtk`），已把 `dtk-api-1` 改到 8000 并导入抖音身份；微信 VM 通过 `127.0.0.1:18000` SSH 隧道访问。下面的 v4 单容器方案仅作为没有 v5 栈时的备用，不再是当前部署路径。细节见 [抖音私信 → 微信媒体桥](../../docs/DOUYIN-WECHAT-BRIDGE.md#130-本轮实际部署2026-11-18)。

本目录只解决“把 DTK 起在哪里、怎么让媒体桥安全地调到它”。媒体桥本身只认 `DTK_BASE_URL`，不关心 DTK 跑在容器、虚拟机还是另一台服务器。

## 选型

| 版本 | 资源 | 建议 |
|---|---|---|
| v4.1.2 单容器 | Docker + 一个容器，无 PostgreSQL/Redis | 腾讯云 3.6GB 轻量机；本项目默认方案 |
| v5.1.x | Docker Compose + PostgreSQL 17/TimescaleDB + Redis + worker（+ 可选 browser） | 8GB+ 内存、愿意维护完整控制台/API Key 时 |

**不要把 DTK 和微信图形会话塞进同一台 3.8GB 的虚拟机。** 微信 + Chromium + Agent 已经占用约 1.7GB；DTK 的浏览器身份池会明显挤占内存。

## 方案 A：腾讯云 v4 单容器

```bash
# 在腾讯云服务器上
mkdir -p ~/dtk-v4 && cd ~/dtk-v4
# 把本目录的 install-v4.sh 传上来后执行
bash install-v4.sh
```

第一次运行会拉取镜像、导出 `douyin-web.yaml`，然后退出。编辑该文件的 `Cookie` 行（从已登录抖音的浏览器复制），再运行一次脚本。服务只绑定 `127.0.0.1:8080`，不暴露公网。

从微信虚拟机建隧道：

```bash
ssh -N -L 8080:127.0.0.1:8080 user@example.com
```

然后在虚拟机 `.env` 填：

```dotenv
DTK_BASE_URL=http://127.0.0.1:8080
DTK_API_KEY=
```

v4 没有 API Key；**不要**把 8080 改成 `0.0.0.0` 或开放安全组。

## 方案 B：v5 Compose

按官方文档在 `docker/compose.yml` 起 `api worker migrate`（可选 `--profile browser`），创建管理员和只带 `douyin:read` 的 API Key：

```dotenv
DTK_BASE_URL=http://127.0.0.1:8000
DTK_API_KEY=dtk_xxx
```

v5 的 `POST /api/v1/parse` 默认异步：先返回 `202 + task_id`，再查 `/api/v1/tasks/{id}`；也可以加 `?wait=20`。媒体桥两种都支持。

## 验证

```bash
# v4：文档页可打开
curl -fsS http://127.0.0.1:8080/docs >/dev/null && echo docs-ok

# 实际解析（v4 路径因版本而异；以容器内 /docs 为准）
curl -sS -X POST "http://127.0.0.1:8080/api/hybrid/video_data?url=https://v.douyin.com/xxxx/" | head

# v5
curl -sS -X POST "http://127.0.0.1:8000/api/v1/parse?wait=20" \
  -H "Authorization: Bearer $DTK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://v.douyin.com/xxxx/"}' | head
```

验收时只看两件事：

1. 返回里有可下载的视频流；
2. 结果里 `watermark` 为 `false`，或至少不是 `true`。媒体桥只接受“明确不是带水印”的流；字段缺失会记录 `watermark=null`，不会声称已逐帧验证。

## 安全

- v4 无鉴权：只绑回环，靠 SSH 隧道；不要把端口开到公网。
- Cookie 和 API Key 只放服务器/本机 `.env`，不写进仓库、日志或回答。
- DTK 的 Cookie 会过期；解析开始返回 400/401 时先更新 Cookie。
- 平台风控、验证码、限流由 DTK 自己报告；媒体桥不会绕过，也不会重试到成功为止。
