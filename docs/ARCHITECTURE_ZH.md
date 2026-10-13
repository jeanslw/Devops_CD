# Devops-Glue 架构全景图

## 整体数据流

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                CODE PUSH                                     │
│          GitLab / Gitee / GitHub / Gitea  →  Webhook Trigger                 │
└────────────────────────────────────┬─────────────────────────────────────────┘
                                     ↓
┌──────────────────────────────────────────────────────────────────────────────┐
│                     CI 层：Devops-Glue API (PHP)                             │
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐   ┌──────────────┐    ┌───────────────┐  │
│  │   Jenkins    │  │  GitLab CI   │   │   Gitea CI   │    │  Custom Push  │<──── User CI
│  │ BuildProvider│  │ BuildProvider│   │ BuildProvider│    │ (custom_push) │  │   (推送方)
│  └──────┬───────┘  └──────┬───────┘   └──────┬───────┘    └───────┬───────┘  │
│         └─────────────────┼──────────────────┼────────── ─────────┼          │
│                                   ↓                                          │
│                    Build → Docker Image → Harbor Registry                    │
│                                   ↓                                          │
│                      scan-sync → ci_pipeline_artifacts                       │
└───────────────────────────────────┬─────┬────────────────────────────────────┘
                                    ↓     ↓
		┌─────────────────────────────────────────────────────────────┐
		│                     CD 层：devops-cd (Python)               │
		│                                                             │
		│  ┌──────────────────────┐       ┌──────────────────────┐    │
		│  │  构建管理（HTTP API）│       │ Webhook 接收/转发    │    │
		│  │ · 触发构建           │       │ · cd_webhooks（配置）│    │
		│  │ · 部署执行           │←─────→│                      │    │
		│  │ · 部署/构建日志      │       │ · 自动转 Bot 通知    │    │
		│  └──────────┬───────────┘       └──────────┬───────────┘    │
		│             │                              │                │
		│                                         自动转发            │
		│   选择 Project + Tag  ──→  部署执行                         │
		│                              ↓                              │
		│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
		│   │  SSH 脚本    │  │Docker Compose│  │  Kubernetes  │      │
		│   │  Ansible     │  │  SFTP + up   │  │ kubectl/Helm │      │
		│   │              │  │              │  │ ArgoCD/FluxCD│      │
		│   └──────────────┘  └──────────────┘  └──────────────┘      │
		│                           ↓                                 │
		│              cd_deploy_logs (部署记录)                      │
		│                           ↓                                 │
		│              钉钉 / 企业微信 / 自定义 Webhook 通知          │
		└─────────────────────────────────────────────────────────────┘
```

## 组件关系

```
		┌──────────────────────────────────────┐
		│ 共享数据库 (SQLite / MySQL / MariaDB)│
		│                                      │
		│  ci_pipeline_artifacts ← CI 写       │
		│  v_glue_deploy_logs    ← CI 只读视图 │
		│  cd_servers            ← CD 维护     │
		│  cd_deploy_logs        ← CD 写       │
		│  cd_approvals(_rules)  ← CD 写       │
		│  cd_sessions           ← CD 写/删    │
		│  cd_bots               ← CD 维护     │
		│  admin_users           ← 共享        │
		└────────────────┬─────────────────────┘
				         │
			      ┌──────┴──────┐
			      ↓             ↓
			 ┌────────┐   ┌────────┐
			 │ PHP CI │   │PythonCD│
			 │:8080   │   │:8081   │
			 └────────┘   └────────┘
```

> **数据归属**：除上述共享表外，CD 自行维护其余 `cd_*` 表（webhook、监控、告警、制品缓存、配置、会话、审批）。CI 侧的构建数据（pipeline、映射、构建记录）由 CD 通过 CI 的 HTTP API 获取，不直接读取其他 `ci_*` 表。

> **数据库选型**：PHP CI 和 CD Service 必须使用同一个数据库实例。
> - **SQLite**：零配置，适合单机开发/测试。容器部署时必须挂载共享卷，确保两边进程能访问同一个 `.db` 文件。
> - **MySQL 8.0+ / MariaDB 10.4+**：推荐生产环境使用，支持并发读写，无需共享卷。

## 部署模式矩阵

| 部署类型 | 模式 | 底层实现 |
|----------|------|---------|
| SSH 单机 | 自定义命令 | Shell 脚本，支持 `{image}` `{tag}` `{project}` 占位符 |
| SSH 单机 | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | 远程 YAML | `cd {path} && docker compose up -d` |
| Docker Compose | 在线编写 | SFTP 上传 compose YAML → 自动建目录 → 启动 |
| K8s kubectl | SSH apply | SSH 到 master 执行 `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + 版本验证 |
| K8s Argo CD | REST API | PATCH image → sync → 轮询 Healthy |
| K8s Flux CD | SSH kubectl | PATCH 资源 → wait ready |

## 设计模式

- **Strategy 模式**：`BuildProviderInterface`（PHP CI）/ `Deployer`（Python CD）抽象基类 + Registry 注册表
- **工厂模式**：`GitProviderFactory` 按 URL 自动匹配 Git 平台适配器
- **双驱动数据库**：SQLite / MySQL / MariaDB 统一接口，一套代码三种模式，与 PHP API 共享同一数据库实例

## CD 表清单（集成服务开关 × 部署闸门）

「CD 是否启用」由 **Devops-Glue 的集成服务契约表 `integrated_services`** 决定（CI 是唯一写入方）：`service_name='cd'` 那一行的 `enabled` 与 `provision` 就是开关，CD 只读（`Database.cd_integration_state()`），不再用「表在不在」猜启用状态。

| 契约行状态 | CD 行为 | 闸门 |
|-----------|---------|------|
| 表 / 行缺失，或 `enabled<>1` | 零 DDL | 503 `errors.cd_not_enabled`（去 CI 后台「集成服务」启用） |
| `enabled=1` + `provision='auto'` | 首次连库自动建全清单 + 补缺失列 + 补索引 + 契约视图（MySQL / SQLite 同理） | 表没建全 → 503 `errors.cd_not_deployed`（重启 CD 自愈） |
| `enabled=1` + `provision='manual'` | 零 DDL（结构归 DBA：MySQL 导入 `database/init_mysql.sql`） | 缺表 → 503 `errors.cd_not_deployed`（导入建表脚本） |

关闭集成服务只冻结准入：CD 表与数据原样保留，不 `DROP`。闸门覆盖登录、所有已持有会话的请求、公开 Webhook 接收与 WebShell 握手。

**增删一张 CD 表 = 只改四处**（闸门判定、错误文案、启动告警、张数全部自动跟随，不写死数字）：

| # | 位置 | 说明 |
|---|------|------|
| 1 | `backend/database.py::CD_REQUIRED_TABLES` | 唯一清单；顺序即错误提示里的展示顺序 |
| 2 | `database/init_mysql.sql` | `manual` 模式的建表脚本（幂等，可重复执行） |
| 3 | `backend/database.py::_MYSQL_TABLE_DDL`（连同 `_MYSQL_INDEXES` / `_MYSQL_COLUMN_MIGRATIONS`） | `auto` 模式的 MySQL 自动建表：`.dockerignore` 排除了 `database/`，运维脚本不进镜像，只能内置 |
| 4 | `backend/database.py::_ensure_cd_tables` | `auto` 模式的 SQLite 随库建表；结尾有**清单自检**，漏改即首次连库报错 |

清单漂移有四道防线：`backend/tests/test_cd_schema_sources.py` 逐表、逐列、逐索引锁定 1↔2↔3↔4（SQLite 侧直接反射真实建表结果），`backend/tests/test_cd_deploy_gate.py` 锁定运行时行为（三种契约状态 × 两个驱动），运行时再由 `_ensure_mysql_tables` / `_ensure_cd_tables` 的清单自检兜底。

**两种模式 × 两个驱动的区别**：

- **`auto`**：CD 首次连库自动建表（MySQL 用内置 `_MYSQL_TABLE_DDL`、SQLite 用 `_ensure_cd_tables`）并补齐既有表的缺失列；表没建全时闸门 503，提示「重启 CD 自愈」。
- **`manual`**：CD 零 DDL（连 ALTER 都不发）；MySQL 由管理员导入 `database/init_mysql.sql`，SQLite 由运维补建（或请 CI 改回自动迁移模式后重新启用）。
- **提示按 `provision` 说实话**：`cd_deploy_hint(driver, provision)` 与 `cd_not_enabled_hint()` 是唯一出口（见 `backend/database.py`），避免把运维指向错误的动作；其中未启用的**用户可见提示只有一句话**（`errors.cd_not_enabled` 的 i18n 文案 —— 登录页提示框与 toast 同源：`error_key` → i18n，都不拼长文案、都随语言切换），动作细节随响应体 `detail.hint` 与启动日志下发。

- **数据库不可用 ≠ 未启用 / 未部署**：连不上、建不了表（库文件只读/磁盘满/被占用）、取不到连接 → `DatabaseUnavailableError` → 503 `errors.db_unavailable`（`driver` / `reason` 随 `error_params` 下发；排查连接、配置、权限、磁盘）；只有「没启用」才是 `errors.cd_not_enabled`（去点开关）、「清单没建全」才是 `errors.cd_not_deployed`（去补表）。同为 503，但 error_key 不同、提示的动作不同 —— 混用会把运维指向错误方向。

## 解决问题

中小型企业 DevOps 工具链碎片化：
- Git 平台（GitLab/Gitee/GitHub/Gitea）→ 统一对接
- CI 引擎（Jenkins/GitLab CI）→ 双通道统一
- 镜像仓库（Harbor）→ 扫描同步
- 部署目标（SSH/Docker/K8s）→ 统一执行
- 通知（钉钉/企微）→ 自动推送
