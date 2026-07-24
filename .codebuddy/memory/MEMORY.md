# 项目记忆

## 项目关系

- **php_api**（项目名 Devops-Glue）：PHP Slim 4，CI 层，管理构建触发、Git 平台对接、Harbor 扫描。数据库的所有者。
- **cd_service**（项目名 devops_cd）：Python FastAPI，CD 层，负责部署执行。数据库的跟随者。

## 核心架构决策

### 数据库共享
- cd_service **无独立数据库**，`DB_DRIVER` 和连接配置必须与 php_api 完全一致
- 启动时 `Database._validate_shared_db()` 校验 `ci_pipeline_tags` 表是否存在，不存在则 RuntimeError 退出
- php_api 有 2 张 CI 表（`ci_job_git_map`、`ci_pipeline_tags`），cd_service 有 3 张 CD 表（`cd_servers`、`cd_deploy_logs`、`cd_bots`），都在同一个库
- 支持 SQLite / MySQL 8.0+ / MariaDB 10.4+，类型由 php_api 决定
- SQLite 容器部署必须共享卷；生产环境推荐 MySQL/MariaDB

### 版本约束
- cd_service：Python 3.10+（Dockerfile `python:3.10-slim`）
- php_api：PHP 8.0+

### 认证
- 两项目共用 `admin_users` 表，bcrypt + Bearer token
- **所有 `/api/*` 接口均需 `verify_token` 鉴权**，包括 `/api/projects`、`/api/projects/{p}/tags`、`/api/projects/{p}/pipeline`

### 服务器标签
- 标签直接存在 `cd_servers.tags` 字段（`VARCHAR(255)`，逗号分隔），无独立标签表
- `GET /api/tags` 扫描所有服务器的 tags 字段，拆分去重排序后返回标签池
- 前端服务器表单用复选框展示，标签随服务器增删自然维护

### Harbor 镜像仓库
- HarborClient 自动探测 scheme：用户未指定时先 HTTPS 后 HTTP 回退
- 自动探测 API 版本：v2 (`/api/v2.0`) → v1 (`/api`)，每种 scheme 下独立探测
- 数据缓存到 `cd_registry_repositories` + `cd_registry_artifacts` 两张本地表
- 只同步 `ci_job_git_map` 中 status=active 的 harbor_repository
- **扫描报告从数据库缓存读取**（秒开），不再实时请求 Harbor；`POST /api/registry/scan/trigger/` 触发重新扫描
- **时间处理**：数据库统一存 UTC 时间，前端 `formatTime` 补 Z 按 UTC 解析后转本地时区显示
- **scan_overview key 动态探测**：不同 Harbor 版本 key 名不同，按 `vulnerability.report` 或 `scanner.adapter` 匹配
