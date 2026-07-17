# Devops-Glue CD Service

FastAPI 持续部署服务，与 [Devops-Glue PHP API](https://github.com/jeanslw/Devops-Glue) 配套使用，负责将 Harbor 镜像部署到 Docker 或 Kubernetes 集群。

## 基础

	- **语言**: Python 3.11+
	- **框架**: FastAPI + uvicorn
	- **数据库**: SQLite / MySQL 8.0+（通过 DB_DRIVER 切换）
	- **端口**: 8081
	- **认证**: 与 PHP CI 系统共享 `admin_users` 表，bcrypt + Bearer token

## 环境要求

	**Python 依赖**

	| 组件 | 版本 | 说明 |
	|------|------|------|
	| Python | 3.11+ | 运行环境 |
	| fastapi | 0.115+ | Web 框架 |
	| uvicorn | 0.34+ | ASGI 服务器 |
	| paramiko | 3.5+ | SSH/SFTP 连接 |
	| kubernetes | 32.0+ | K8s Python 客户端 |
	| pymysql | 1.1+ | MySQL 驱动 |
	| pydantic-settings | 2.0+ | 环境配置管理 |
	| bcrypt | 4.2+ | 密码验证 |
	| requests | 2.31+ | HTTP 客户端 |
	| python-multipart | 0.0.9+ | 文件上传 |

	**前端**

	| 组件 | 说明 |
	|------|------|
	| xterm.js 5.3 | Web Shell 终端（CDN 按需加载，不影响登录速度） |

	**服务器端（可选）**

	| 组件 | 说明 |
	|------|------|
	| Docker / docker-compose | 单机 & compose 部署目标 |
	| Kubernetes 1.28+ | K8S 集群 |
	| Argo CD v2.9+ | GitOps CD（可选） |
	| Flux CD | GitOps CD（可选） |
	| Helm 3+ | K8S 包管理（可选） |
	| Ansible | 自动化部署（可选） |
	| MySQL 8.0+ | 数据库（可选，默认 SQLite） |

## 架构

	```
	cd_service/
	├── main.py                    # 入口 (52行)
	├── app/
	│   ├── config.py              # Pydantic BaseSettings
	│   ├── database.py            # SQLite/MySQL 双驱动 + 建表
	│   ├── auth.py                # 共享登录
	│   ├── models/requests.py     # Pydantic 请求模型
	│   ├── routers/
	│   │   ├── auth.py            # POST /api/login
	│   │   ├── projects.py        # GET /api/projects, /api/projects/{p}/pipeline, /api/projects/{p}/tags
	│   │   ├── servers.py         # CRUD /api/servers + tags 分类
	│   │   ├── deploy.py          # POST /api/deploy, /api/stop (Docker)
	│   │   ├── k8s_deploy.py      # POST /api/deploy-k8s (kubectl/ArgoCD/FluxCD)
	│   │   ├── logs.py            # GET /api/deploy-logs
	│   │   ├── bots.py            # CRUD /api/bots (钉钉/企微/自定义)
	│   │   └── terminal.py        # WS /ws/terminal/{id} + POST /api/upload/{id}
	│   ├── services/
	│   │   ├── ci_service.py      # 读 CI DB: 项目/Tag/Pipeline
	│   │   ├── deploy_service.py  # Docker 部署编排 + 批量
	│   │   └── notification.py   # 钉钉/企微 webhook
	│   └── deployers/
	│       ├── base.py            # Deployer 抽象基类 + DeployTarget + ssh_connect
	│       ├── registry.py        # DeployerRegistry (工厂模式)
	│       ├── ssh.py             # 自定义命令 / Ansible Playbook
	│       └── compose.py         # docker-compose 部署 + SFTP 上传 YAML
	├── templates/dashboard.html   # Jinja2 SPA
	├── static/
	│   ├── style.css
	│   └── app.js
	└── .env                       # 环境变量配置
	```

	### 设计模式

	- **Strategy 模式**: `Deployer` 抽象基类 + `DeployerRegistry` 注册表，对齐 PHP 项目的 `BuildProviderInterface`
	- **Sequence Counter**: 前端请求去重，切项目时旧请求结果丢弃
	- **Lazy CDN**: xterm.js 仅在打开 Web Shell 时加载，不影响登录速度

	### 部署模式矩阵

	| 部署类型 | 执行模式 | 说明 |
	|----------|---------|------|
	| SSH 单机 | 自定义命令 | 用户编写 Shell 脚本，`{image}` `{tag}` `{project}` 占位符 |
	| SSH 单机 | Ansible Playbook | `ansible-playbook {path} -e image=...` |
	| docker-compose | 远程 YAML | `cd {path} && IMAGE_TAG={tag} docker compose up -d` |
	| docker-compose | 在线编写 YAML | SFTP 上传 + 自动创建目录 + docker compose up |
	| docker-compose | 自定义命令 | 自定义 compose 脚本 |
	| K8S kubectl | SSH apply | SSH 到 master `kubectl apply -f` |
	| K8S Helm | SSH kubectl | helm upgrade --install → 版本验证 |
	| K8S Argo CD | REST API | PATCH image → sync → 轮询 Healthy |
	| K8S Flux CD | SSH kubectl | PATCH Kustomization/HelmRelease → wait ready |

	### 数据流

	```
	CI (Jenkins/GitLab)
	  → build + push Harbor
	  → scan-sync → ci_pipeline_tags (tag=v20260716)

	CD Panel
	  → 选项目 + 选 tag
	  → 选部署模式 + 目标服务器
	  → SSH/API 执行部署
	  → 读取输出 (docker compose / kubectl get pods)
	  → 写入 deploy_logs
	  → Webhook 通知
	```

	### 数据库表结构

	```
	# CI 表 (PHP 维护，CD 只读)
	ci_job_git_map       - 项目映射 (job_name ↔ git ↔ harbor)
	ci_pipeline_tags     - 构建 Tag 列表
	ci_platform_versions - 平台 API 版本

	# 共享表
	admin_users          - 管理员账号
	cache                - 查询缓存

	# CD 表 (Python 维护)
	servers              - 部署目标 (host/user/password/tags/type)
	deploy_logs          - 部署记录 (project/tag/output/status)
	bots                 - 通知机器人 (钉钉/企微/自定义)
	```

	### 索引策略

		| 索引 | 表 | 列 |
		|------|------|-----|
		| idx_pt_project | ci_pipeline_tags | project |
		| idx_pt_created | ci_pipeline_tags | created_at |
		| idx_jgm_path | ci_job_git_map | current_path |
		| idx_dl_project | deploy_logs | project |
		| idx_dl_created | deploy_logs | created_at |

## 快速开始

	```bash
	cd cd_service
	python -m venv venv
	source venv/bin/activate  # Windows: venv\Scripts\activate
	pip install -r requirements.txt

	# 配置 .env
	cp .env.example .env
	# DB_DRIVER=sqlite (默认，共享 ../php_api/config/data/data.db)
	# 或 DB_DRIVER=mysql (独立部署，需先建库)

	python main.py
	# 访问 http://localhost:8081
	```

## 配置说明

	```env
	# ── 数据库（必填: sqlite 或 mysql）──
	DB_DRIVER=sqlite
	DB_PATH=../php_api/config/data/data.db

	# MySQL（DB_DRIVER=mysql 时生效）
	DB_HOST=127.0.0.1
	DB_PORT=3306
	DB_NAME=devops_glue
	DB_USER=root
	DB_PASS=

	# Harbor
	HARBOR_REGISTRY=192.168.137.5
	HARBOR_USER=admin
	HARBOR_PASSWORD=

	# SSH
	SSH_TIMEOUT=30
	SSH_DEFAULT_USER=root
	```

## API 端点

	| 方法 | 路径 | 认证 | 说明 |
	|------|------|:---:|------|
	| GET | `/health` | - | 健康检查 |
	| POST | `/api/login` | - | 登录 |
	| GET | `/api/projects` | - | CI 项目列表 + tag |
	| GET | `/api/projects/{p}/pipeline` | - | Pipeline 状态 |
	| GET | `/api/projects/{p}/tags` | - | 项目所有 Tag |
	| GET | `/api/servers` | ✅ | 服务器列表 |
	| POST | `/api/servers` | ✅ | 添加服务器 |
	| DELETE | `/api/servers/{id}` | ✅ | 删除服务器 |
	| POST | `/api/deploy` | ✅ | Docker 部署 |
	| POST | `/api/deploy-k8s` | ✅ | K8S 部署 |
	| POST | `/api/stop` | ✅ | 停止服务 |
	| GET | `/api/deploy-logs` | - | 部署记录 |
	| GET | `/api/bots` | ✅ | BOT 列表 |
	| POST | `/api/bots` | ✅ | 添加 BOT |
	| DELETE | `/api/bots/{id}` | ✅ | 删除 BOT |
	| WS | `/ws/terminal/{id}` | - | Web Shell |
	| POST | `/api/upload/{id}` | ✅ | SFTP 文件上传 |
	| GET | `/` | - | Dashboard |

## 联系作者
	如有建议可在 GitHub 仓库提 issue ，或联系EMAIL:jeanslw@qq.com