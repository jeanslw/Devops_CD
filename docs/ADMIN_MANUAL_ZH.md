# Devops-Glue CD — 管理员配置手册

> 面向系统管理员：环境要求、配置说明、部署运维。

## 1. 环境要求

### Python 依赖

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 运行环境 |
| fastapi | 0.115+ | Web 框架 |
| uvicorn | 0.34+ | ASGI 服务器 |
| paramiko | 3.5+ | SSH / SFTP 连接 |
| kubernetes | 32.0+ | K8s Python 客户端 |
| pymysql | 1.1+ | MySQL 驱动 |
| pydantic-settings | 2.0+ | 环境配置管理 |
| bcrypt | 4.2+ | 密码验证 |
| requests | 2.31+ | HTTP 客户端 |
| python-multipart | 0.0.9+ | 文件上传 |

### 前端

| 组件 | 说明 |
|------|------|
| Vue 3 + Vite | 主控制台 SPA |
| Vue Router 4 | 前端路由（Web History 模式） |
| vue-i18n 9 | 国际化（中/英文切换） |
| xterm.js 5.3 | Web Shell 终端（按需加载） |

### 服务器端（可选）

| 组件 | 说明 |
|------|------|
| Docker / docker-compose | 单机 & Compose 部署目标 |
| Kubernetes 1.19 ~ 1.31 | K8s 集群（详见 K8s 版本策略） |
| metrics-server | 资源监控前置依赖 |
| Argo CD v2.9+ / Flux CD | GitOps CD（可选） |
| Helm 3+ | K8s 包管理（可选） |
| Ansible | 自动化部署（可选） |
| MySQL 8.0+ / MariaDB 10.4+ | 数据库（推荐生产环境） |

## 2. 环境变量配置

```env
# ── 数据库（必填，必须与 Devops-Glue API 一致）──
DB_DRIVER=sqlite
DB_PATH=../Devops-Glue/config/data/data.db

# MySQL 模式（推荐生产环境）：
# DB_DRIVER=mysql
# DB_HOST=127.0.0.1
# DB_PORT=3306
# DB_NAME=devops_glue
# DB_USER=root
# DB_PASS=

# ── Harbor 镜像仓库 ──
HARBOR_BASE_URL=https://hub.example.com
HARBOR_USER=admin
HARBOR_PASSWORD=

# ── CI API 集成（可选，构建管理功能）──
CI_API_URL=http://127.0.0.1:8080      # Devops-Glue API 地址（含端口）
CI_API_TOKEN=dg_xxx                   # API Token（dg_ 前缀，服务账号/第三方）推荐
# CI_ADMIN_USER=admin                 # 未配置 token 时回退：CI 系统管理员账号
# CI_ADMIN_PASS=                      # 未配置 token 时回退：CI 系统管理员密码

# ── SSH 自动信任（开发环境 fallback，生产保持 false）──
SSH_AUTO_TRUST=false

# ── 加密密钥（首次运行自动生成，勿修改）──
ENCRYPTION_KEY=

# ── SSH ──
SSH_TIMEOUT=30
SSH_DEFAULT_USER=root

# ── Docker 部署 ──
CONTAINER_RESTART_POLICY=always

# ── K8s 部署 ──
# FLUX_NAMESPACE=flux-system

# ── 监控与告警（秒）──
MONITORING_ENABLED=true
MONITOR_CACHE_SERVERS=60
MONITOR_CACHE_SYSTEM=30
MONITOR_CACHE_NODES=30
MONITOR_CACHE_PODS=30
MONITOR_CACHE_DOCKER=30
MONITOR_CACHE_POD_DETAIL=15
ALERT_CHECK_INTERVAL=60
REGISTRY_SYNC_INTERVAL=3600

# ── 日志截断（字符）──
LOG_TRUNCATE_CHARS=2000
NOTIFY_TRUNCATE_CHARS=200
```

## 3. 数据库说明

> **关键**：cd_service 没有独立数据库，必须与 Devops-Glue API 共用同一个数据库实例。启动时会校验 `ci_pipeline_tags` 表是否存在。

| 驱动 | 适用场景 | 注意 |
|------|----------|------|
| `sqlite` | 开发/单机测试 | 必须指向同一个 `.db` 文件，容器部署需挂载共享卷 |
| `mysql` | 生产环境（推荐）| 避免 SQLite 并发写入问题，支持 CD 和 PHP API 同时读写 |

### 自动建表

首次启动时，系统自动创建以下 CD 表：

| 表名 | 说明 |
|------|------|
| `cd_servers` | 部署目标服务器 |
| `cd_deploy_logs` | 部署记录 |
| `cd_bots` | 通知机器人 |
| `cd_registry_repositories` | Harbor 仓库元数据 |
| `cd_registry_artifacts` | 制品/Tag 信息 |
| `cd_custom_monitors` | 自定义监控项 |
| `cd_custom_monitor_metrics` | 监控指标定义 |
| `cd_alert_rules` | 告警规则 |
| `cd_alert_logs` | 告警历史 |
| `cd_config` | 系统配置键值对 |
| `cd_webhooks` | Webhook 接收配置（token + 关联 Bot） |
| `cd_webhook_events` | Webhook 收到的事件记录（原始 payload） |

## 4. 快速部署

### 直接运行

```bash
cd cd_service
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 配置数据库和 Harbor

python main.py
# 访问 http://localhost:8081
```

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env 配置数据库和 Harbor

docker compose up -d
# 访问 http://localhost:8081
```

> **SQLite 注意**：容器部署时需将数据库目录挂载为共享卷，确保 CD 和 PHP API 能访问同一个 `.db` 文件。

#### 与 Devops-Glue 部署关系

CD Service 和 Devops-Glue（CI）共用同一个 MySQL（`devops_glue` 库），根据是否同主机分为三种方式：

**方式一：同主机合并部署（推荐）**

将 cd-service 服务块加入 Devops-Glue 的 `docker-compose.yml`，同一 compose 内 DNS 自动互通：

```yaml
# Devops-Glue 的 docker-compose.yml 中追加：
  cd-service:
    build:
      context: ../cd_service
      dockerfile: Dockerfile
    container_name: cd-service
    env_file:
      - ../cd_service/.env
    environment:
      PYTHONUNBUFFERED: "1"
      HOST: 0.0.0.0
      PORT: 8081
    ports:
      - "8081:8081"
    restart: unless-stopped
    depends_on:
      mysql:
        condition: service_healthy
```

`.env` 中设置 `DB_HOST=devops-mysql`（CI 中 MySQL 的容器名）。

**方式二：同主机分开部署**

两个 compose 独立运行在同一台机器上，通过 Docker 外部网络互通：

```bash
# 1. 创建共享网络
docker network create devops-net
```

两个 compose 文件都加入该网络：

```yaml
# CD Service 的 docker-compose.yml
networks:
  devops-net:
    external: true
```

```yaml
# Devops-Glue 的 docker-compose.yml 中追加
services:
  mysql:
    networks:
      - devops-net
  devops-glue:
    networks:
      - devops-net

networks:
  devops-net:
    external: true
```

CD Service 的 `.env` 中 `DB_HOST=devops-mysql`，同一网络内 Docker 内置 DNS 自动解析容器名。MySQL 无需暴露端口到宿主机。

**方式三：分主机部署**

CD Service 和 MySQL 运行在不同机器上。此时无法用 Docker DNS 解析容器名，需要 MySQL 暴露端口并通过宿主机 IP 连接。

Devops-Glue 的 MySQL 需要暴露端口（在 CI 的 compose 中取消注释或添加）：

```yaml
# Devops-Glue 的 docker-compose.yml
services:
  mysql:
    ports:
      - "3306:3306"   # 暴露端口到宿主机
```

同时确认 MySQL 允许远程连接（容器默认允许），并在安全组/防火墙中放行 3306 端口。

CD Service 的 `.env` 设置：

```env
DB_DRIVER=mysql
DB_HOST=192.168.x.x    # MySQL 所在宿主机的 IP
DB_PORT=3306
DB_NAME=devops_glue
DB_USER=root
DB_PASS=your_password
```

> **注意**：分主机部署时，CD Service 的 compose 中 `networks` 可以完全去掉（或用默认 bridge），不依赖 `devops-net`。

### 前端构建

```bash
cd frontend
npm install
npm run build    # 输出到 ../static/
```

## 5. Kubernetes 版本兼容

本项目通过 **SSH + kubectl CLI** 与 K8s 集群交互（不使用 Kubernetes API 客户端），版本兼容面较宽。

| 功能 | 最低版本 | 说明 |
|------|:---:|------|
| 资源监控（kubectl top） | 1.8 | 需安装 **metrics-server** |
| Pod/Node 信息展示 | 1.2 | kubectl get -o custom-columns |
| kubectl apply / rollout | 1.2 | 基础部署操作 |
| Helm 部署 | 3.0+ | Helm CLI，与 K8s 版本解耦 |
| Argo CD 部署 | 2.9+ | REST API，与 K8s 版本解耦 |
| Flux CD 部署 | 0.x / 2.x | kubectl patch，与 K8s 版本解耦 |

| 版本范围 | 状态 | 说明 |
|----------|:--:|------|
| 1.8 ~ 1.18 | ⚠️ 理论可用 | 未测试，早期 JSONPath 字段可能不一致 |
| 1.19 ~ 1.31 | ✅ 推荐 | 所有功能验证通过 |
| 1.32+ | 🔮 预期兼容 | 基础命令不变 |

> K8s 1.24+ 移除了 dockershim。使用 containerd/CRI-O 的集群，`docker stats` 不可用（不影响 `kubectl top pods`）。

### K8s 部署模式详解

Devops-Glue CD 支持四种 K8s 部署模式，每种模式的工作原理和 CD 角色不同：

#### kubectl 模式（直接部署）

**原理**：CD 通过 SSH 连接到 K8s 节点/跳板机，用 `kubectl apply` + `rollout restart` 直接操作集群。

**CD 做了什么**：
1. 通过 SSH 连接目标服务器（CD 配置的 K8s 节点或有 kubectl 的跳板机）
2. 读取远程 YAML 文件（或下载 URL YAML），将 `{IMAGE}:{TAG}` 替换为实际镜像
3. `kubectl apply -f` 上传并应用 YAML 到集群
4. `kubectl rollout restart deployment/<name>` 触发滚动重启
5. `kubectl rollout status` 等待部署完成（默认 120s 超时）
6. 验证新旧 Pod 变化，判断部署成败

**适用场景**：没有 GitOps 工具的纯 kubectl 运维环境，团队习惯直接 apply YAML。

**前置要求**：
- 目标服务器已配置好 kubeconfig（~/.kube/config）
- 服务器上 kubectl 版本与集群兼容
- YAML 模板中包含 `{IMAGE}:{TAG}` 占位符

#### Argo CD 模式

**原理**：CD 通过 Argo CD REST API 远程更新 Application 的镜像参数，然后触发 Sync，由 Argo CD 完成实际的 GitOps 部署。

**CD 做了什么**：
1. 调用 Argo CD API `GET /api/v1/applications/<name>` 查找 Application（找不到则遍历所有 App 按镜像名匹配）
2. 根据 Application 类型选择更新策略：
   - **Helm**：patch `spec.source.helm.parameters` 中的 `image.tag`
   - **Kustomize**：patch `spec.source.kustomize.images` 中的 `newTag`
3. 调用 API `PUT /api/v1/applications/<name>` 提交更新
4. 调用 API `POST /api/v1/applications/<name>/sync` 触发 Argo CD 同步
5. 轮询 Application 状态（`/api/v1/applications/<name>`），等待 Health = "Healthy"（最多 60s）
6. 根据 Health/Sync 状态判断部署成败

**适用场景**：已使用 Argo CD 做 GitOps 的团队，CD 仅作为 Argo CD 的"触发器"，不直接操作集群。

**前置要求**：
- Argo CD 已部署并可通过 HTTPS 访问
- CD 能获取 Argo CD 的 API Token（通过服务器的 password 字段传入）
- Application 已存在，且配置了 Helm/Kustomize 的镜像参数

**CD 不做的事**：
- 不直接操作 kubectl
- 不创建/修改 Argo CD Application
- 不管理 Git 仓库或 Helm Chart

#### Flux CD 模式

**原理**：CD 通过 SSH + kubectl 直接 patch Flux CD 的 HelmRelease/Kustomization 资源，触发 Flux reconcile，由 Flux 完成实际的部署。

**CD 做了什么**：
1. 通过 SSH 连接 K8s 节点
2. 自动发现 Flux 资源（先精确匹配项目名，找不到则扫描 `flux-system` namespace 下所有 HelmRelease/Kustomization，按镜像名匹配）
3. 用 `kubectl patch` 更新镜像 tag：
   - **HelmRelease**：patch `spec.values.image.tag`
   - **Kustomization**：patch `spec.images[].newTag`
4. 用 `kubectl annotate` 给资源打 `reconcile.fluxcd.io/requestedAt` 注解，强制触发 Flux 立即协调
5. 轮询等待 Flux 反应（最多 90s）：
   - 检测新 Pod 出现或旧 Pod 终止
   - 检查 Flux 资源的 Ready condition 是否报错
6. 找到对应的 Deployment 名，执行 `kubectl rollout status` 等待滚动完成
7. 根据 rollout status 结果判断部署成败

**适用场景**：已使用 Flux CD 做 GitOps 的团队，CD 作为 Flux 的"触发器"，通过 kubectl patch + annotate 实现快速迭代。

**前置要求**：
- Flux CD 已部署在 `flux-system` namespace
- 集群可通过 SSH + kubectl 访问
- HelmRelease/Kustomization 资源已存在，且引用了正确的镜像

**CD 不做的事**：
- 不直接创建 Pod/Deployment
- 不管理 Git 仓库
- 不安装/配置 Flux CD

#### 三种模式对比

| 对比项 | kubectl | Argo CD | Flux CD |
|--------|---------|---------|---------|
| 集群交互方式 | SSH + kubectl CLI | REST API（HTTPS） | SSH + kubectl CLI |
| 部署执行者 | CD 直接执行 kubectl | Argo CD 执行 | Flux CD 执行 |
| 镜像更新方式 | 渲染 YAML → apply | API patch Application 参数 | kubectl patch HelmRelease/Kustomization |
| 触发方式 | 直接 apply | API 调用 sync | annotate 触发 reconcile |
| 部署耗时 | 最快（~30s） | 中等（~60s，含同步等待） | 较长（~90s，含 Flux 协调） |
| 网络要求 | 能 SSH 到 K8s 节点 | 能访问 Argo CD HTTPS | 能 SSH 到 K8s 节点 |
| 额外组件 | 无 | Argo CD + Token | Flux CD + kubectl |
| 适合团队 | 传统 kubectl 运维 | Argo CD GitOps | Flux CD GitOps |

## 6. 审批与回滚

### 审批规则

部署可被审批流程管控。规则存于 `cd_approval_rules`，在「审批」视图中管理（需 `cd.deploy.approve` 权限）：

- **项目范围**：规则作用于指定项目（逗号分隔多个）或 `*` 全局默认。
- **启用**：总开关。
- **目标环境**（`require_envs`）：逗号分隔的环境标签；仅当目标服务器命中至少一个标签时才需审批。留空表示所有环境都需审批。
- **审批角色**（`approver_role`，默认 `cd_admin`）与/或**显式审批人**（`approvers`，逗号分隔用户名，优先于角色）。
- **通知机器人**（`notify_bot_id`）：审批请求/结果通知的机器人。
- **回滚审批**（`require_rollback_approval`，默认开启）：回滚是否也需审批。

命中规则的部署会生成 `cd_approvals` 审批单（`pending`）并通知审批人。审批状态机为 `pending → approved → deploying → deployed / failed`，另有 `rejected`、`cancelled`。批准是原子落库迁移；执行由后台线程完成，并带持久化队列，进程重启后已批准未执行的审批单不会丢失。

### 回滚

回滚用于重新部署上一版成功版本。从部署页触发，同样经过审批闸门（若启用）：

- **原生回滚**（kubectl / helm / argocd）：`kubectl rollout undo`、`helm rollback --wait` 或 ArgoCD `rollback` API。
- **重放回滚**（fluxcd / ssh / compose）：复用上一版成功部署存储的参数快照（`cd_deploy_logs.params_json`）。

回滚通过 SSE 实时流式输出日志（`POST /api/deploy/rollback-stream`）。v1.5.0 之前的老部署记录无参数快照，无法回滚。

## 7. 安全配置

### 密码加密

服务器密码和 SSH 私钥通过 Fernet 对称加密存储。`ENCRYPTION_KEY` 首次运行时自动生成并写入 `.env`，**请勿修改**，否则已加密数据无法解密。

### 用户角色

| 角色 | 权限 | 说明 |
|------|------|------|
| `admin` | 全部权限：部署、服务器管理、用户管理、系统配置 | 由 CI 系统统一分配，CD 侧不允许创建/删除/修改 admin |
| `deployer` | 部署操作、查看监控、管理服务器 | CD 管理员可创建 |
| `viewer` | 只读查看：项目、部署记录、监控数据 | CD 管理员可创建 |

> **CD/CI 登录隔离**：`admin_users` 表新增 `systems` 字段（逗号分隔），CD 侧登录时校验是否包含 `"cd"`。仅 `systems` 含 `"cd"` 的用户可登录 CD 系统。CI 负责账号全生命周期管理（创建 admin、分配 systems），CD 仅管理 deployer/viewer。

### 认证

- 登录：POST `/api/login`，用户名 + 密码 → 返回 Bearer Token
- Token 格式：Base64 编码，含 username 信息
- 受保护端点需在 Header 中携带 `Authorization: Bearer <token>`

## 8. CI 构建管理集成（可选）

CD 系统新增"构建管理"面板，通过 HTTP API 调用 Devops-Glue（CI）接口，实现在 CD 面板直接触发 Jenkins/GitLab CI 构建、查看构建历史和构建日志。

### 配置步骤

1. 在 CI 系统「API 管理」中创建一个 API Token（按需勾选 `build.read`、`build.write` 等 scope），然后在 `.env` 中配置：
   ```
   CI_API_URL=http://ci-host:8080
   CI_API_TOKEN=dg_xxx
   ```
   未配置 API Token 时，可回退到管理员账号登录（不推荐）：
   ```
   CI_API_URL=http://ci-host:8080
   CI_ADMIN_USER=admin
   CI_ADMIN_PASS=your_ci_password
   ```
2. 重启 CD 服务。账号模式下启动时会自动拉取 CI JWT Token 并缓存；API Token 模式免登录、无需缓存/续期。
3. 打开 CD → 「构建管理」页面，选择 CI 项目即可触发构建。

### 工作原理

- `backend/services/ci_client.py` 支持两种认证：API Token 模式（固定 Bearer token，免登录）与账号模式（JWT Token 缓存，过期前自动续期）。请求失败自动重试（指数退避）。
- 数据归属：CD 只读不写 CI 数据库表（`ci_pipeline_tags` / `ci_job_git_map`）；"构建管理"走 HTTP API，"Tag 清单/部署流程"继续走 DB 直读，两层互不干扰。
- 构建历史、构建日志通过 CI API 实时获取，不在 CD 本地落地。

## 9. Webhook 接收端点 & 安全策略

v1.2.2 新增 **Webhook 接收端点**，用于接收 CI 构建完成、部署成功等外部事件，并可选自动转发到通知机器人。

### 接收端点（公开，无需登录）

```
POST /api/webhooks/receive/{token}
Content-Type: application/json
```

**Jenkins 示例（Pipeline post 段）：**
```bash
curl -s -S -X POST "http://cd-host:8081/api/webhooks/receive/42kYUmGU0yZHMiXdusSNb0lckWx43cna" \
  -H "Content-Type: application/json" \
  -d "{\"project\":\"$JOB_NAME\",\"tag\":\"$TAG\",\"image\":\"$IMAGE:$TAG\",\"built_at\":\"$(date +'%Y%m%d%H%M%S')\"}"
```

> **⚠️ 重要**：`-d` 必须用双引号包裹 JSON 字符串，否则 shell 变量不会被展开。单引号会导致 `$JOB_NAME`、`$TAG`、`$(date)` 被当作字面量。建议使用 `jq` 构造 JSON 避免引号陷阱。

### 请求 payload 建议字段（无严格校验，未识别字段原样存储）

| 字段 | 说明 | 模板占位符 |
|------|------|:-----------:|
| `project` | 项目/Job 名 | `{project}` |
| `tag` | 构建 Tag | `{tag}` |
| `image` | 完整镜像（含 Tag） | `{image}` |
| `built_at` / `time` | 构建时间戳 | `{time}` / `{built_at}` |
| `status` | 构建状态 success/failure | `{status}` |
| `target` | 部署目标 | `{target}` |
| `mode` | 部署模式 | `{mode}` |

### 创建 Webhook

1. 进入 CD → 「通知接入」页面（需 `cd.notification-manage` 权限）
2. 点击「新建」，填写名称、可选关联 Bot（关联后事件自动转发）
3. 创建成功后系统生成 32 字符随机 token，拼接到接收 URL：
   ```
   http://<cd-host>:8081/api/webhooks/receive/<token>
   ```
4. 将该 URL 配置到 Jenkins Pipeline 或 GitLab CI 后处理脚本

### 安全策略

| 项 | 说明 |
|----|------|
| **token 长度** | 32 字符（`secrets.token_urlsafe(24)`），足够防止暴力枚举 |
| **token 唯一性** | 数据库 UNIQUE 索引，重复会冲突 |
| **仅 POST 生效** | GET 访问端点返回 404，避免被误触发 |
| **启用/禁用** | 被禁用的 Webhook 直接返回 404，便于临时下线不删配置 |
| **事件大小** | 无显式限制，建议保持 < 1MB；大日志走构建日志 API 查询 |
| **自动转发失败降级** | Bot 转发失败**不影响事件入库**，可在页面手动重试 |
| **Bot 模板占位符不匹配** | 自动回退默认拼接格式，再兜底输出原始 JSON，绝不崩溃 |
| **关联 Bot 权限** | 创建/编辑 Webhook 需 `cd.notification-manage` 权限；接收端点无鉴权（靠 token） |
| **SSRF 防护** | 通知机器人 Webhook URL 校验：域名精确或后缀匹配，防止子域名绕过（如 `eviloapi.dingtalk.com`） |

### 事件管理

- 在 Webhook 详情页可分页浏览历史事件（默认 20 条/页，最大 100 条）
- 支持删除单条事件、手动转发到任意 Bot
- 自动转发标记为 `forwarded=1`，并记录 `forwarded_at` 时间戳
- 建议定期清理大体积旧事件（`DELETE /api/webhooks/events/{id}`），避免 `cd_webhook_events` 表膨胀
