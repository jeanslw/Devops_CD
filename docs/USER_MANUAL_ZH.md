# Devops-Glue CD — 用户使用手册

> 面向日常使用者：开发、运维、测试人员。涵盖系统概述、部署操作、自定义监控、API 参考。

## 1. 系统概述

Devops-Glue CD 是持续部署服务，与 [Devops-Glue API](https://github.com/jeanslw/Devops-Glue.git) 配套使用，将 Harbor 镜像部署到 Docker 或 Kubernetes 集群。通过一个统一面板完成项目选择、Tag 确认、部署执行、日志查看和通知推送。

```
CI (Jenkins / GitLab CI)
  → build + push → Harbor
  → scan-sync → ci_pipeline_tags

CD Panel (本项目)
  → 选择项目 + Tag
  → 选择部署模式 + 目标服务器
  → SSH / API 执行部署
  → 实时日志输出
  → 写入 cd_deploy_logs
  → Webhook 通知（钉钉/企微/自定义）
```

## 2. 功能概览

| 模块 | 功能 |
|------|------|
| 项目部署 | 选择 CI 项目 → 选择 Tag → 选择部署模式 → 一键部署 |
| 部署模式 | SSH 命令、Ansible、Docker Compose、K8s kubectl/Helm/ArgoCD/FluxCD |
| CI 构建管理 | 通过 HTTP 代理 CI API 触发 Jenkins/GitLab CI 构建、查看构建历史和构建日志 |
| Webhook 接收/转发 | 接收 CI 构建完成等事件，自动转发到钉钉/企微/自定义机器人，支持事件历史分页浏览 |
| 资源监控 | CPU、内存、磁盘、Docker 容器、K8s 节点/Pod 实时监控 |
| 自定义监控 | 通过 SSH 执行自定义命令，CSV/KV/JSON 多格式解析，监控任意指标 |
| 告警通知 | 资源阈值告警，钉钉/企微/自定义 Webhook 推送 |
| Web Shell | 浏览器内 SSH 终端，支持 SFTP 文件上传 |
| 制品仓库 | Harbor 镜像浏览、漏洞扫描、Tag 安全删除 |
| 服务器管理 | 服务器增删改查，Tag 标签分组，SSH/Docker/K8s 三种类型 |

## 3. 部署模式

| 部署类型 | 模式 | 说明 |
|----------|------|------|
| SSH 单机 | 自定义命令 | Shell 脚本，支持 `{image}` `{tag}` `{project}` 占位符 |
| SSH 单机 | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | 远程 YAML | `cd {path} && IMAGE={image} TAG={tag} docker compose up -d` |
| Docker Compose | 在线编写 | SFTP 上传 compose YAML → 自动建目录 → 启动 |
| K8s kubectl | SSH apply | SSH 到 master 执行 `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + 版本验证 |
| K8s Argo CD | REST API | PATCH image → sync → 轮询 Healthy |
| K8s Flux CD | SSH kubectl | PATCH 资源 → wait ready |

## 4. 自定义资源监控

> 通过 SSH 在目标服务器执行任意命令，将输出解析为结构化指标。支持 CSV（表头+行）、KV（key=value）、JSON 三种格式。

### 4.1 配置步骤

1. 进入「⚙️ 自定义资源」页面
2. 填写**资源名称**、**监控命令**、选择**输出格式**
3. 添加**指标**：指定字段名（field_key）、指标名、单位
4. 选择**目标服务器**（SSH 或 Docker 类型）
5. 点击「🧪 测试」验证解析结果

### 4.2 使用示例

**磁盘空间监控：**
```bash
LANG=C df -h --type=ext4 --type=xfs
```
格式：自动检测 → 指标：`Use%`（%）、`Avail`（GB）

**GPU 监控：**
```bash
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed --format=csv
```
格式：CSV → 指标：`temperature.gpu`（°C）、`fan.speed`（%）

**内存监控：**
```bash
free -m
```
格式：自动检测 → 指标：`used`（MB）、`available`（MB）

**进程数监控：**
```bash
ps aux | wc -l
```
格式：自动检测（单值，无需配置指标）

### 4.3 故障排查

如果测试结果数值显示 `—`：
- 展开「原始输出 ▾」查看实际表头
- 确认 `field_key` 与表头**精确匹配**（区分大小写）
- 中文环境建议在命令前加 `LANG=C` 强制英文输出
- 解析面板会显示**诊断信息**，列出可用表头和配置的 key 对比

## 5. 告警规则

支持对系统资源、Docker 容器、K8s Pod、自定义监控指标设置阈值告警。

| 资源类型 | 说明 |
|----------|------|
| CPU | 1 分钟负载 / 核数 |
| 内存 | 已用百分比 |
| 磁盘 | 根分区使用率 |
| Docker CPU/内存 | 单个容器资源使用 |
| Pod CPU/内存 | K8s Pod 资源使用 |
| 进程 CPU/内存 | Top 进程监控 |
| 自定义 | 自定义监控指标（选择监控项和指标） |

设置阈值后，系统按 `ALERT_CHECK_INTERVAL`（默认 300 秒）周期性检查，超标时通过配置的 Bot 发送通知。

## 6. CI 构建管理

「构建管理」菜单通过 HTTP API 代理到 Devops-Glue（CI）接口，实现在 CD 面板直接触发 Jenkins/GitLab CI 构建、查看构建历史和控制台日志。

### 6.1 前置条件

- 管理员已在 `.env` 配置 `CI_API_URL` 和 `CI_API_TOKEN`（或回退的 `CI_ADMIN_USER`/`CI_ADMIN_PASS`）并重启服务（详见《管理员配置手册》第 7 章）。
- 当前用户有 CD 登录权限（`admin_users.systems` 含 `"cd"`）。

### 6.2 使用流程

1. 左侧菜单进入「构建管理」。
2. 列表展示 CI 项目及最新构建状态。
3. 点击「触发构建」：选择分支/Tag，可选填写自定义构建变量，提交。
4. 构建出现在历史列表；点击日志图标查看实时控制台输出（从 CI API 流式获取）。

### 6.3 注意事项

- 构建历史、日志按需从 CI 拉取，不在 CD 本地落地。
- 写操作通过 CI API 执行，受 CI 系统自身权限控制。

## 7. Webhook 接收与事件转发

「通知接入」菜单用于创建安全的公开接收端点，供 Jenkins/GitLab CI 等外部系统 POST 构建完成等事件到 CD，并可选择自动转发到通知机器人。

### 7.1 概念说明

| 术语 | 含义 |
|------|------|
| Webhook 配置 | 一条端点定义，含系统生成的 32 字符随机 token 作为 URL 鉴权 |
| 关联 Bot | 可选关联 `cd_bots` 条目，收到事件后自动转发到该机器人 |
| 事件 | 一次 POST 接收到的 payload 记录，含时间戳和转发状态 |

### 7.2 创建 Webhook

1. 进入「通知接入」→「新建」（需 `cd.notification-manage` 权限）。
2. 填写 **名称**（如 `Jenkins 构建成功`），可选择 **关联通知机器人** 实现自动转发。
3. 保存后系统展示公开端点：
   ```
   https://<cd-host>:8081/api/webhooks/receive/<32 字符 token>
   ```
4. 将端点复制到 Jenkins Pipeline / GitLab CI 后处理脚本，例如：
   ```bash
   curl -s -S -X POST "<endpoint>" \
     -H "Content-Type: application/json" \
     -d "{\"project\":\"$JOB_NAME\",\"tag\":\"$TAG\",\"image\":\"$IMAGE:$TAG\",\"built_at\":\"$(date +'%Y%m%d%H%M%S')\"}"
   ```
   > `-d` 参数必须用 **双引号** 包裹，`$VAR` / `$(cmd)` 才能被 shell 正确展开；推荐用 `jq` 构造 JSON 避免转义错误。

### 7.3 Payload 建议字段

无严格 schema，任意 JSON 均被接受。以下字段在格式化机器人消息时会被自动识别：

`project`、`tag`、`image`、`built_at`（或 `time`）、`status`、`target`、`mode`。

自定义 Bot 模板支持 `{project}` `{tag}` `{image}` `{status}` `{time}` `{target}` `{mode}` 占位符。

### 7.4 事件浏览与操作

- 在 Webhook 行点击「事件」进入分页浏览（默认 20 条/页，最大 100 条）。
- 每条事件展示 `payload`（原始 JSON）、`received_at`、转发状态、`forwarded_at`。
- **手动转发**：选择 Bot 重发任意事件，机器人临时不可达时可事后补救。
- **删除事件**：清理过期或超大 payload。

### 7.5 启用 / 禁用 / 删除

- 列表行上的开关可快速停用 Webhook（不删除配置）；禁用的端点一律返回 404。
- 删除 Webhook 会一并删除该端点的所有事件记录。

## 8. 项目结构

```
cd_service/
├── main.py              # 入口
├── backend/
│   ├── routers/         # API 路由（16 个模块：+ webhooks, + ci_build）
│   ├── services/        # 业务逻辑层
│   └── deployers/       # 部署器（SSH/Compose/kubectl/ArgoCD/FluxCD/Helm）
├── frontend/            # Vue 3 前端源码
├── static/              # 前端构建产物
├── database/            # 数据库脚本
└── docs/                # 文档
```

## 9. API 参考

### 认证说明

| 标记 | 含义 |
|:----:|------|
| — | 无需认证 |
| ✅ | 需要 Bearer Token（`Authorization: Bearer <token>`）|
| 🔑 | 需要 Admin 角色 |

### 端点列表

| 方法 | 路径 | 认证 | 说明 |
|------|------|:----:|------|
| GET | `/health` | — | 健康检查 |
| GET | `/api/info` | — | 公开信息（版本、DB 类型/状态、运行时间） |
| POST | `/api/login` | — | 登录，返回 Token |
| GET | `/api/me` | ✅ | 当前用户信息 |
| GET | `/api/projects` | ✅ | CI 项目列表（含最新 Tag） |
| GET | `/api/projects/{p}/pipeline` | ✅ | 项目 Pipeline 状态 |
| GET | `/api/projects/{p}/tags` | ✅ | 项目所有 Tag |
| **CI 构建管理（代理 CI HTTP API）** | | | |
| GET | `/api/ci/projects` | ✅ | CI 项目列表 |
| GET | `/api/ci/{pid}/builds` | ✅ | CI 项目构建历史 |
| POST | `/api/ci/{pid}/build` | ✅ | 触发构建（分支/Tag + 自定义变量） |
| GET | `/api/ci/{pid}/build/{bid}/log` | ✅ | 构建控制台日志（流式） |
| GET | `/api/ci/{pid}/variables` | ✅ | CI 项目构建变量 |
| GET | `/api/ci/{pid}/branches` | ✅ | 仓库分支/Tag 列表 |
| GET | `/api/ci/health` | ✅ | CI API 连通性检查 |
| GET | `/api/servers` | ✅ | 服务器列表 |
| POST | `/api/servers` | ✅ | 添加服务器 |
| PUT | `/api/servers/{id}` | ✅ | 更新服务器 |
| DELETE | `/api/servers/{id}` | ✅ | 删除服务器 |
| GET | `/api/servers/tags` | ✅ | Tag 分组 |
| POST | `/api/deploy` | ✅ | Docker 部署 |
| POST | `/api/deploy-k8s` | ✅ | K8s 部署 |
| POST | `/api/stop` | ✅ | 停止服务 |
| GET | `/api/deploy-logs` | ✅ | 部署记录查询 |
| GET | `/api/bots` | ✅ | 通知机器人列表 |
| POST | `/api/bots` | ✅ | 添加机器人 |
| DELETE | `/api/bots/{id}` | ✅ | 删除机器人 |
| GET | `/api/monitor/servers` | ✅ | 监控服务器列表 |
| GET | `/api/monitor/system/{id}` | ✅ | 服务器系统资源 |
| GET | `/api/monitor/nodes/{id}` | ✅ | K8s 节点指标 |
| GET | `/api/monitor/pods/{id}` | ✅ | K8s Pod 指标 |
| GET | `/api/monitor/docker/{id}` | ✅ | Docker 容器指标 |
| GET | `/api/custom-monitors` | ✅ | 自定义监控列表 |
| POST | `/api/custom-monitors` | ✅ | 创建自定义监控 |
| PUT | `/api/custom-monitors/{id}` | ✅ | 更新自定义监控 |
| DELETE | `/api/custom-monitors/{id}` | ✅ | 删除自定义监控 |
| POST | `/api/custom-monitors/{id}/test` | ✅ | 测试运行 |
| GET | `/api/alerts` | ✅ | 告警规则列表 |
| POST | `/api/alerts` | ✅ | 创建告警规则 |
| PUT | `/api/alerts/{id}` | ✅ | 更新告警规则 |
| DELETE | `/api/alerts/{id}` | ✅ | 删除告警规则 |
| GET | `/api/registry/repositories` | ✅ | Harbor 仓库列表 |
| GET | `/api/registry/artifacts/{id}` | ✅ | 仓库 Tag/Artifact 列表 |
| GET | `/api/registry/scan/{id}/{tag}` | ✅ | Tag 漏洞扫描详情 |
| DELETE | `/api/registry/artifacts/{id}` | ✅ | 删除 Tag（安全校验） |
| POST | `/api/registry/sync` | ✅ | 触发 Harbor 同步 |
| WS | `/ws/terminal/{id}` | — | Web Shell 终端 |
| POST | `/api/upload/{id}` | ✅ | SFTP 文件上传 |
| GET | `/api/users` | 🔑 | 用户列表 |
| POST | `/api/users` | 🔑 | 创建用户 |
| DELETE | `/api/users/{name}` | 🔑 | 删除用户 |
| PUT | `/api/users/{name}/role` | 🔑 | 修改角色 |
| PUT | `/api/users/{name}/password` | ✅ | 修改密码（自己或 admin） |
| **Webhook 配置管理** | | | |
| GET | `/api/webhooks` | ✅ | Webhook 配置列表 |
| POST | `/api/webhooks` | 🔑 | 创建 Webhook（需 `cd.notification-manage`） |
| PATCH | `/api/webhooks/{wid}` | 🔑 | 更新 Webhook（名称/关联 Bot） |
| DELETE | `/api/webhooks/{wid}` | 🔑 | 删除 Webhook 及全部事件 |
| POST | `/api/webhooks/{wid}/toggle` | 🔑 | 启用/禁用 Webhook |
| GET | `/api/webhooks/{wid}/events` | ✅ | Webhook 事件分页列表 |
| DELETE | `/api/webhooks/events/{eid}` | 🔑 | 删除单条事件 |
| POST | `/api/webhooks/events/{eid}/forward` | ✅ | 手动转发事件到指定 Bot |
| **Webhook 公开接收端点** | | | |
| POST | `/api/webhooks/receive/{token}` | — | CI/Jenkins/GitLab 等外部系统的公开接收端点（URL 路径 token 鉴权） |
| GET | `/` | — | 前端 SPA |
