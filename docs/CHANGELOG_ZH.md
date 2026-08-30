# 更新日志

## v1.5.0 (2026-08-31) — 部署审批 + 一键回滚

### 新增功能
- **部署审批**：部署前新增规则化审批闸门。管理员按项目（或 `*` 全局默认）配置 `cd_approval_rules`：启用开关、目标环境（`require_envs`，匹配服务器标签）、审批人（显式用户名或 `approver_role`，默认 `cd_admin`）、通知机器人。命中规则的部署会生成 `cd_approvals` 审批单（`pending`）并通知审批人，批准后才执行。
- **审批状态机**：`pending → approved → deploying → deployed / failed`，另有 `rejected`、`cancelled`。批准是原子落库的状态迁移；执行由后台线程完成。
- **审批持久化队列**：approved 记录由幂等轮询器领取执行；进程重启后 `recover_on_startup` 清理僵尸部署锁并重投 `deploying` 审批单。
- **审批管理界面**：新增「审批」视图，含审批单列表（批准 / 驳回 / 撤销）与审批规则管理页签。
- **一键回滚**：新增 `POST /api/deploy/rollback` 与 `POST /api/deploy/rollback-stream`（SSE）。按部署模式分两种策略：
  - **原生回滚**（kubectl / helm / argocd）：以该模式最新成功记录为上下文，直接调集群原生回退命令（`kubectl rollout undo`、`helm rollback --wait`、ArgoCD `rollback` API）。
  - **重放回滚**（fluxcd / ssh / compose）：复用上一版不同 tag 成功记录的参数快照（`params_json`）重新执行。
  - 老记录（v1.5.0 前，无参数快照）不支持回滚，自动跳过。
- **回滚流式输出**：回滚改为 SSE 实时流式日志，ArgoCD / kubectl / helm / 停止兜底等回滚文案中英双语齐全。

### 变更
- 回滚同样经过审批闸门（由 `require_rollback_approval` 控制，默认开启）。
- 新增权限点 `cd.deploy.approve`，控制审批动作与规则管理。

### 新增数据库表
- `cd_approval_rules` — 按项目（或 `*` 全局）的审批规则。
- `cd_approvals` — 审批单（状态、申请人、审批人、部署参数快照）。

### 新增文件
- `backend/routers/approvals.py`、`backend/services/approval_service.py`、`backend/services/rollback_service.py`、`backend/services/deploy_executor.py`、`backend/services/k8s_deploy_service.py`
- `frontend/src/views/ApprovalsView.vue`

---

## v1.4.0 (2026-08-19) — Custom_Push 项目只读展示

### 新增功能
- **Custom_Push 项目适配** — 构建历史视图识别 `custom_push` 项目并只读展示：隐藏「触发构建」按钮并在动作层守卫禁止触发（custom_push 结果由用户 CI 推送，Devops-Glue 只接收回写）。
- **完成时间列** — 构建记录改展示完成时间（`updated_at`）而非创建时间，契合推送式上报的生命周期。

### 变更
- **文案** — 「时间」→「完成时间」（中英双语）。

---

## v1.3.1 (2026-08-17) — 部署取消机制 + 并发锁 + 结构化耗时 + UI 优化

### 新增功能
- **部署取消机制**：新增 `POST /api/deploy/cancel` 接口，支持用户手动取消进行中的部署。基于 `threading.Event` 内存信号，低层循环（ssh_exec_stream、k8s 子部署器）轮询信号及时中断长命令。`DeployCancelled` 继承 `BaseException` 以穿透 `except Exception` 兜底，冒泡到编排层统一按 `terminated` 落库。
- **部署并发锁**：同一项目同时只允许一个 `running` 记录。部署前通过 `find_running_deploy` 检查；路由层同时拒绝 `deploy_type='k8s/*'` 混入 SSH/Compose 路线（API 签名不兼容），Service 层防御性抛 `ValueError`（在插入 running 记录之前）。
- **结构化耗时与部署说明**：部署记录新增 `duration_ms`（总耗时）、`stage_times`（每台服务器每阶段耗时明细）、`deploy_note`（用户填写的自由文本说明，部署日志表新增"说明"列展示）。
- **自定义确认弹窗**：用 promise-based 的 `useConfirm` 组合式 + 全局 `ConfirmModal` 组件替换原生浏览器 `confirm()`，覆盖部署停止/取消、日志页取消等操作。

### 变更与优化
- **SSH 部署页**：移除停止按钮（自定义 SSH 命令场景无停止语义）；取消按钮改为常驻显示（非部署中灰色禁用），不再中途消失。
- **K8s 部署页**：移除"查看资源占用"按钮（跳转外部监控，价值低）；取消按钮同样常驻显示。
- **Docker / K8s 停止**：保留，配合自定义确认弹窗做二次确认。

### Bug 修复
- **K8sSubDeployer 缺 `validate` 方法**：`deploy_type='k8s/kubectl'` 误调 SSH/Compose 接口时抛 `AttributeError`（K8s 子部署器继承链缺 validate）。在 `K8sSubDeployer` 补充双签名兼容的 `validate`。
- **异常时 running 记录孤儿**：`_deploy_k8s_core` 和 `DeployService.execute` 只捕获 `DeployCancelled`，其他意外异常会让 running 记录残留，并发锁死锁。补 `except Exception` 兜底，将记录更新为 `failed`。
- **`deploy_type='k8s/*'` 混入 SSH/Compose 路线**：registry 会为 SSH/Compose 路线创建 `KubectlDeployer`，但 `deploy()` 签名不兼容。路由层 + Service 层双拦截，在插入 running 记录之前就拒绝。
- **`register()` 未重置 Event 状态**：`deploy_id` 复用（罕见）时历史取消信号可能泄漏到新部署。`register()` 现在始终 `ev.clear()`。

---

## v1.3.0 (2026-08-13) - CI连接改用Devops-Glue的专用API Token，修复bug

- **更新文档** CI_API_TOKEN 配置说明,修复ruff检测的L1/L2/L3级别bug

## v1.2.2 (2026-08-08) — Webhook 接收端点 & CI 构建管理 & 类型安全加固 & 部署错误透传

### 新增功能
- **Webhook 接收端点**：新增 `/api/webhooks/receive/{token}` 公开接口，用于接收外部事件（如 CI 构建完成通知）。
  - 支持 Jenkins / GitLab CI / 自定义 CI 的任意 POST 推送
  - 自动持久化至 `cd_webhook_events` 表，支持分页浏览事件历史
  - 可选通过关联 Bot 自动转发至钉钉/企微/自定义机器人，支持模板占位符
  - 支持手动转发事件、启用/禁用 Webhook、删除事件
  - 采用 32 位随机 Token 认证（URL 路径方式），无需登录
- **CI 构建管理**：新增“构建管理”菜单，通过 HTTP API 代理 CI 服务。
  - 支持触发构建、查看构建历史、查看构建日志（兼容 Jenkins/GitLab CI 双引擎）
  - `backend/services/ci_client.py`：JWT Token 缓存 + 自动刷新 + 重试机制
  - 后端路由：`/api/ci/*`（项目列表、构建历史、触发构建、构建日志、构建变量、分支列表、健康检查）
- **公开信息接口**：`/api/info` 无需鉴权，返回应用名称、版本、数据库类型及连接状态、运行时长
- **健康检查简化**：`/health` 改为纯 `{"status":"ok"}` 响应，适配 Docker/K8s 存活探针

### 变更与优化
- **部署错误透传**：`deploy` 路由层捕获 `ValueError`（无效 Docker Compose 路径、缺失 SSH 服务器等），通过 SSE 流实时向前端推送具体错误信息，不再吞掉异常
- **类型安全加固**：全项目通过 Pyright 类型检查，统一数据库接口（SQLite/MySQL 占位符自动转换），修复一批 `Optional` 未检查、`None` 比较类型不匹配问题
- **数据库接口统一**：新增 `conn.execute()` 内部封装，SQLite 的 `?` 占位符在 MySQL 驱动下自动转换为 `%s`，业务代码无需关心驱动差异
- **CD 表索引补全**：为 `cd_servers.type`、`cd_deploy_logs.deploy_id`（复合索引）、`cd_alert_rules.enabled/created_at`、`cd_custom_monitors.enabled/created_at`、`cd_webhooks.enabled`、`cd_webhook_events.webhook_id/received_at` 添加索引
- **错误处理增强**：后端异常体系统一使用 `error_key`，前端错误页据此展示对应文案；`cd_webhook_*` 异常系列全覆盖（未找到/已存在/创建失败/转发失败）
- **前端国际化修复**：修复 `lang` 参数传递问题，Shell “Connected” 硬编码文本移入翻译文件，修复多处 UI 翻译遗漏
- **Webhook 管理界面**：前端新增“通知接入”菜单（WebhookView），支持创建/编辑/删除/启停、查看事件、分页浏览、手动转发
- **侧边栏导航**：新增 `ciBuild`、`webhook` 菜单项，统一归入 `cd.notification-manage` 权限控制
- **测试环境优化**：消除 pytest 弃用警告（Pydantic `class Config`→`model_config`、FastAPI `on_event`→`lifespan`、安装 `httpx2`）
- **部署文档完善**：管理员手册（中/英）新增 CI API 配置、Webhook 安全策略、三种部署策略详解
- **版权信息修正**：前端页脚及 LICENSE 版权均由 `Blues.Inc` 统一为 `jeanslw`
- **README 更新**：中/英 README 新增“相关项目”章节，GitHub 链接由 `Devops_Glue` 修正为 `Devops-Glue`
- **部署配置简化**：`docker-compose.yml` 移除独立 `networks` 块，默认与 CI 同机合并部署，新增 `image: devops-cd:latest`

### 新增数据库表

| 表名 | 说明 |
| :--- | :--- |
| cd_webhooks | Webhook 接收配置（id/name/token/bot_id/enabled/created_at） |
| cd_webhook_events | Webhook 事件日志（id/webhook_id/payload/received_at/forwarded/forwarded_at） |

### 设计说明
- **Webhook Token 认证**：采用 URL 路径 `{token}` 方式（非 Header），方便 Jenkins 等工具直接 POST 无需复杂签名。Token 为 32 位随机字符串（`secrets.token_urlsafe(24)`），数据库唯一索引约束
- **自动转发机制**：接收事件 → 持久化至 DB → 若 `bot_id>0` 则自动调用 Bot Webhook → 标记 `forwarded=1`。失败不阻塞事件持久化，管理员可从 UI 手动重试
- **消息占位符**：Bot 模板支持 `{project}{tag}{image}{status}{built_at}{target}{mode}`，无模板时默认按字段名格式化，无法解析时回退到原始 JSON 输出
- **数据归属**：CD 仅读取 CI 数据库表（`ci_pipeline_tags`、`ci_job_git_map`），绝不写入；“构建管理”走 HTTP API，“Tag 列表/部署流程”仍走直连 DB 读取——两层互不干扰
- **`/api/info`**：完全公开，无鉴权依赖，方便监控系统和外部工具查询 CD 运行状态
- **`error_key`**：异常携带国际化键，前端可根据 key 映射对应语言的错误提示

## v1.2.1 (2026-07-29) — RBAC 权限适配 & 部署日志优化

### 变更
- 适配 Devops-Glue API RBAC 权限体系（`enforce_deploy_perm` 部署二次鉴权）
- 部署日志优化（部署成功/失败信息去重、运行版本对比展示）
- 部分变量配置迁移至 `docker-compose.yml`
- 文档同步更新

### 新增/修改文件

| 文件 | 变更 |
| :--- | :--- |
| backend/auth.py | 修改 — `enforce_deploy_perm` 服务层二次权限校验 |
| backend/deployers/*.py | 修改 — 部署日志格式统一、运行版本对比 |
| docker-compose.yml | 修改 — 配置变量迁移 |
| docs/* | 修改 — 文档同步更新 |

## v1.2.0 (2026-07-28) — Landing Page & 登录国际化 & CD/CI 权限隔离

### 新增功能
- **Landing Page**：粒子背景 + Hero 标题 + 6 个功能卡片 + 页脚，未登录时展示
- **多语言支持**：中英文切换，Landing 页和登录页均有独立语言切换按钮
- **CD/CI 登录隔离**：`admin_users` 表新增 `systems` 字段（逗号分隔），CD 侧校验 `systems` 包含 `"cd"` 才允许登录
- **CD 权限收窄**：不可创建/删除/修改 admin 角色用户，admin 不在用户列表中展示；新建用户表单移除“Admin”选项
- **前端 Vue3 重构**：完整替换旧 jQuery 前端，Vue 3.5 + Vite 6 + Vue Router 4

### 修复
- `locales/index.js` 运算符优先级 bug（`||` vs `&&`）
- `vite.config.js` 补充 `/static` 代理路径
- `.gitignore` 新增 `node_modules` 清理规则
- `main.py` 移除废弃的 `ensure_role_column` 调用

## v1.1.0 (2026-07-24) — Harbor 镜像仓库集成 & 配置增强

### 新增功能
- **Harbor 镜像仓库集成**：支持对接 Harbor 仓库，浏览项目/镜像/Tag、查看漏洞扫描报告、一键选择 Tag 用于部署
- **可配置同步间隔**：前端下拉框 + API + DB 持久化 + 后台线程动态重启，无需重启服务
- **Harbor 不可达友好提示**：新增 `HarborUnavailableError` 异常，路由层返回明确错误信息