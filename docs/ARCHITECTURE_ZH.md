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
		│  ci_pipeline_artifacts ← CI 写/CD 读 │
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

## CD 表清单（部署闸门）

CD 是否启用，只由代码里的唯一清单 `backend/database.py::CD_REQUIRED_TABLES` 决定（与清单张数无关）：清单里的表**全部存在**即视为已部署，少任何一张都当作未部署 —— 登录、所有已持有会话的请求、公开 Webhook 接收、WebShell 握手统一 503 `errors.cd_not_deployed`，应用只报警、**绝不代为建表**。这份清单就是「CD 有没有启用」的分水岭。

**增删一张 CD 表 = 只改三处**（闸门判定、错误文案、启动告警、张数全部自动跟随，不写死数字）：

| # | 位置 | 说明 |
|---|------|------|
| 1 | `backend/database.py::CD_REQUIRED_TABLES` | 唯一清单；顺序即错误提示里的展示顺序 |
| 2 | `database/init_mysql.sql` | MySQL 建表脚本（生产启用 CD 的唯一手段，幂等可重跑） |
| 3 | `backend/database.py::_ensure_cd_tables` | SQLite 随库自动建表；结尾有**清单自检**，漏改即首次连库报错 |

清单漂移有三道防线：`backend/tests/test_cd_deploy_gate.py` 对 1↔2 双向校验、对 1↔3 做集合覆盖断言（张数取 `len(清单)`，不写死），运行时再由 `_ensure_cd_tables` 的清单自检兜底。

**两个驱动的区别**：

- **MySQL**：CD 表只能由管理员导入 `database/init_mysql.sql` 建立，应用零 DDL（只对**已存在**的表做缺失列幂等迁移）；未建全 → 闸门 503，提示导入脚本。
- **SQLite**：CD 表随库自动建全，开发零摩擦；运行期若被手工 `DROP TABLE`，闸门**立刻** 503（不静默补建），**重启服务即自动建全自愈**；提示按驱动给出正确动作（`cd_deploy_hint()` 读 `Database.driver`），不会指错方向。

- **数据库不可用 ≠ 未部署**：连不上、建不了表（库文件只读/磁盘满/被占用）、取不到连接 → `DatabaseUnavailableError` → 503 `errors.db_unavailable`（`driver` / `reason` 随 `error_params` 下发；排查连接、配置、权限、磁盘）；只有「清单里的表没建全」才是 `errors.cd_not_deployed`（去补齐建表）。同为 503，但 error_key 不同、提示的动作不同 —— 混用会把运维指向错误方向。

## 解决问题

中小型企业 DevOps 工具链碎片化：
- Git 平台（GitLab/Gitee/GitHub/Gitea）→ 统一对接
- CI 引擎（Jenkins/GitLab CI）→ 双通道统一
- 镜像仓库（Harbor）→ 扫描同步
- 部署目标（SSH/Docker/K8s）→ 统一执行
- 通知（钉钉/企微）→ 自动推送
