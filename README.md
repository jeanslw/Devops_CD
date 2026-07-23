# Devops - CD

> ❌ GitLab + K8s 全套？太重，养不起  
> ❌ Jenkins 裸奔？8 年前的 UI，配到崩溃  
> ❌ Gitee + Jenkins + Harbor 三头对不上？多窗口来回切，Tag 全靠人肉对齐  
>
> ✅ 一套 API，4 个 Git 平台 + 2 条 CI 通道 + Harbor → 一个面板全搞定  
> ✅ SQLite 零配置启动，MySQL 也可切换  
> ✅ 10 年运维老兵的实战结晶  
> ✅ 从 CI 构建到 CD 部署，全流程覆盖  
> ✅ 开源免费，GitHub/Gitee 双更新  
>
> **不是大厂的遥控器，是小团队的瑞士军刀。**

FastAPI 持续部署服务，与 [Devops-Glue API](https://github.com/jeanslw/Devops-Glue) 配套使用，将 Harbor 镜像部署到 Docker 或 Kubernetes 集群。

## 基础

- **主页**：https://github.com/jeanslw/devops_cd.git 或 https://gitee.com/jeanslw/devops_cd.git
- **语言**：Python 3.11+
- **框架**：FastAPI + uvicorn
- **数据库**：SQLite / MySQL 8.0+（通过 `DB_DRIVER` 切换）
- **端口**：8081
- **版本**：v0.2.0
- **认证**：与 Devops-Glue API 共享数据库，bcrypt + Bearer token，不可单独使用

## 环境要求

**Python 依赖**

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.11+ | 运行环境 |
| fastapi | 0.115+ | Web 框架 |
| uvicorn | 0.34+ | ASGI 服务器 |
| paramiko | 3.5+ | SSH / SFTP 连接 |
| kubernetes | 32.0+ | K8s Python 客户端 |
| pymysql | 1.1+ | MySQL 驱动 |
| pydantic-settings | 2.0+ | 环境配置管理 |
| bcrypt | 4.2+ | 密码验证 |
| requests | 2.31+ | HTTP 客户端 |
| python-multipart | 0.0.9+ | 文件上传 |

**前端**

| 组件 | 说明 |
|------|------|
| xterm.js 5.3 | Web Shell 终端（按需加载，不影响登录速度） |

**服务器端（可选）**

| 组件 | 说明 |
|------|------|
| Docker / docker-compose | 单机 & Compose 部署目标 |
| Kubernetes 1.28+ | K8s 集群 |
| Argo CD v2.9+ | GitOps CD（可选） |
| Flux CD | GitOps CD（可选） |
| Helm 3+ | K8s 包管理（可选） |
| Ansible | 自动化部署（可选） |
| MySQL 8.0+ | 数据库（可选，默认 SQLite） |

## 架构

```
cd_service/
├── main.py                     # 入口
├── app/
│   ├── config.py               # Pydantic BaseSettings
│   ├── database.py             # SQLite / MySQL 双驱动 + 自动建表
│   ├── auth.py                 # 共享登录 Token 验证
│   ├── models/requests.py      # Pydantic 请求模型
│   ├── routers/
│   │   ├── auth.py             # POST /api/login
│   │   ├── projects.py         # GET /api/projects, pipeline, tags
│   │   ├── servers.py          # CRUD /api/servers + tags 分类
│   │   ├── deploy.py           # POST /api/deploy, /api/stop (Docker)
│   │   ├── k8s_deploy.py       # POST /api/deploy-k8s (kubectl / ArgoCD / FluxCD)
│   │   ├── logs.py             # GET /api/deploy-logs
│   │   ├── bots.py             # CRUD /api/bots (钉钉 / 企微 / 自定义)
│   │   └── terminal.py         # WS /ws/terminal/{id} + POST /api/upload/{id}
│   ├── services/
│   │   ├── ci_service.py       # 读取 CI 数据：项目 / Tag / Pipeline
│   │   ├── deploy_service.py   # Docker 部署编排 + 批量
│   │   └── notification.py     # 钉钉 / 企微 Webhook 通知
│   └── deployers/
│       ├── base.py             # Deployer 抽象基类 + DeployTarget + ssh_connect
│       ├── registry.py         # DeployerRegistry 工厂模式
│       ├── ssh.py              # 自定义命令 / Ansible Playbook
│       ├── compose.py          # Docker Compose 部署 + SFTP 上传 YAML
│       └── k8s.py              # Kubernetes 部署器 (kubectl / Helm / ArgoCD / FluxCD)
├── templates/
│   ├── index.html              # 首页落地页
│   └── dashboard.html          # 主控制台 SPA
├── static/
│   ├── style.css
│   ├── app.js
│   └── vendor/xterm/           # xterm.js 本地库
├── docker-compose.yml          # Docker Compose 部署
├── Dockerfile                  # 容器构建
├── .env                        # 环境变量
└── README.md
```

### 设计模式

- **Strategy 模式**：`Deployer` 抽象基类 + `DeployerRegistry` 注册表，对齐 PHP 的 `BuildProviderInterface`
- **Sequence Counter**：前端请求去重，切换项目时旧请求结果自动丢弃
- **Lazy Load**：xterm.js 仅在打开 Web Shell 时加载，不影响页面加载速度

### 部署模式矩阵

| 部署类型 | 模式 | 说明 |
|----------|------|------|
| SSH 单机 | 自定义命令 | Shell 脚本，支持 `{image}` `{tag}` `{project}` 占位符 |
| SSH 单机 | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | 远程 YAML | `cd {path} && IMAGE_TAG={tag} docker compose up -d` |
| Docker Compose | 在线编写 YAML | SFTP 上传 + 自动建目录 + 启动 |
| Docker Compose | 自定义命令 | 自定义 Compose 脚本 |
| K8s kubectl | SSH apply | SSH 到 master 执行 `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + 版本验证 |
| K8s Argo CD | REST API | PATCH image → sync → 轮询 Healthy |
| K8s Flux CD | SSH kubectl | PATCH 资源 → wait ready |

### 数据流

```
CI (Jenkins / GitLab CI)
  → build + push → Harbor
  → scan-sync → ci_pipeline_tags (tag=v20260716)

CD Panel
  → 选择项目 + Tag
  → 选择部署模式 + 目标服务器
  → SSH / API 执行部署
  → 读取输出 (docker compose / kubectl get pods)
  → 写入 cd_deploy_logs
  → Webhook 通知
```

### 数据库表结构

```
# CI 表（PHP 维护，CD 只读）
ci_job_git_map       - 项目映射 (job_name ↔ git ↔ harbor)
ci_pipeline_tags     - 构建 Tag 列表
ci_platform_versions - 平台 API 版本

# 共享表
admin_users          - 管理员账号
cache                - 查询缓存

# CD 表（Python 维护）
cd_servers           - 部署目标 (name / host / user / password / type / tags)
cd_deploy_logs       - 部署记录 (project / tag / image / deploy_type / status / output)
cd_bots              - 通知机器人 (name / type / webhook_url)
```

### 索引策略

| 索引 | 表 | 列 |
|------|------|-----|
| idx_cdl_project | cd_deploy_logs | project |
| idx_cdl_created | cd_deploy_logs | created_at |
| idx_pt_project | ci_pipeline_tags | project |
| idx_pt_created | ci_pipeline_tags | created_at |
| idx_jgm_path | ci_job_git_map | current_path |

## 快速开始

```bash
cd cd_service
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate

pip install -r requirements.txt

# 配置 .env
cp .env.example .env
# DB_DRIVER=sqlite（默认，共享 ../php_api/config/data/data.db）
# 或 DB_DRIVER=mysql（独立部署，需先建库）

python main.py
# 访问 http://localhost:8081
```

### Docker Compose 部署

```bash
# 编辑 .env 配置数据库和 Harbor 连接
cp .env.example .env

docker compose up -d
# 访问 http://localhost:8081
```

## 配置说明

```env
# ── 数据库（必填：sqlite 或 mysql）──
DB_DRIVER=sqlite
DB_PATH=../php_api/config/data/data.db

# MySQL（DB_DRIVER=mysql 时生效）
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=devops_glue
DB_USER=root
DB_PASS=

# ── Harbor 镜像仓库 ──
HARBOR_REGISTRY=hub.abc.com
HARBOR_USER=admin
HARBOR_PASSWORD=

# ── SSH ──
SSH_TIMEOUT=30
SSH_DEFAULT_USER=root

# ── Docker 部署 ──
CONTAINER_RESTART_POLICY=always

# ── 日志 ──
LOG_TRUNCATE_CHARS=2000
NOTIFY_TRUNCATE_CHARS=200
```

## API 端点

| 方法 | 路径 | 认证 | 说明 |
|------|------|:---:|------|
| GET | `/health` | - | 健康检查 |
| POST | `/api/login` | - | 登录 |
| GET | `/api/projects` | - | CI 项目列表 + Tag |
| GET | `/api/projects/{p}/pipeline` | - | Pipeline 状态 |
| GET | `/api/projects/{p}/tags` | - | 项目所有 Tag |
| GET | `/api/servers` | ✅ | 服务器列表 |
| POST | `/api/servers` | ✅ | 添加服务器 |
| DELETE | `/api/servers/{id}` | ✅ | 删除服务器 |
| POST | `/api/deploy` | ✅ | Docker 部署 |
| POST | `/api/deploy-k8s` | ✅ | K8s 部署 |
| POST | `/api/stop` | ✅ | 停止服务 |
| GET | `/api/deploy-logs` | - | 部署记录 |
| GET | `/api/bots` | ✅ | BOT 列表 |
| POST | `/api/bots` | ✅ | 添加 BOT |
| DELETE | `/api/bots/{id}` | ✅ | 删除 BOT |
| WS | `/ws/terminal/{id}` | - | Web Shell 终端 |
| POST | `/api/upload/{id}` | ✅ | SFTP 文件上传 |
| GET | `/` | - | 首页 |
| GET | `/dashboard` | - | 控制台 |

## 联系作者

如有建议可在 GitHub 仓库提 Issue，或联系 EMAIL：jeanslw@qq.com
