# CD 接入 Devops-Glue 单点登录（OIDC / BFF）设计文档

> **状态**：设计冻结（v1）。L1a 实现已落地在 `release/1.6.0` 工作树，L1b 已在本机真环境完成端到端验证。
> **默认档（2026-10-12 起）**：`AUTH_MODE=oidc` 为默认登录方式（裸机与 Docker 一致），本地密码登录保留为可回退的第二轨。
> **端到端取证（本机）**：Glue `release/2.9.0`（含 P0，经 Laragon nginx 在 `127.0.0.1:8080`）与 CD（`AUTH_MODE=oidc`，`127.0.0.1:8081`）
> 走真授权码流：`/api/auth/oidc/start` → Glue 授权页 → `/api/auth/oidc/callback`（真 token 兑换 + 真 JWKS 验签）
> → `/api/me` 200（20 项权限逐项等于 Glue 权限端点）→ `/api/logout` 后 `/api/me` 401。
> 失败面实测：`systems` 不含 `cd` → 401 `errors.no_cd_access` 且会话行即时删除；不存在账号 → 401 `errors.identity_unavailable`；
> 伪造 token 与 `nonce` / `iss` / `aud` / `exp` 负例全部拒绝；运行日志无 secret / id_token / access_token。
> 后端全量单测 313 passed（基线 215 例）。分支与提交血统见评审记录，本节不再重复。
> **上游**：Devops-Glue 作为 OAuth2 / OIDC Provider（Glue 侧 `docs/单点登录.md`；上游设计文档 v1.0.4）。
> **相关**：[`ARCHITECTURE_ZH.md`](ARCHITECTURE_ZH.md)（部署闸门与 CD 表清单）、[`ADMIN_MANUAL_ZH.md`](ADMIN_MANUAL_ZH.md)（配置与切换/回退步骤）、[`FAQ_ZH.md`](FAQ_ZH.md)（登录失败排查）。

---

## 1. 背景与目标

**现状**：CD 与 Devops-Glue 共享 `admin_users` / `roles` / `role_permissions` —— 登录用本地 bcrypt 校验密码
（`backend/routers/auth.py::login` → `backend/auth.py::authenticate`），身份与权限每次请求直读共享表
（`get_current_user` → `_query_user_with_systems` + `_query_permissions`），会话是服务端不透明 token
（`cd_sessions` 表只存 SHA-256 摘要）。

**目标（分家）**：把 Devops-Glue 作为 CD 的**唯一身份源（IdP）**：

1. 登录改走 OIDC 授权码流（BFF 机密客户端，浏览器不经手任何 Glue 凭据）；
2. 身份（`username` / `role` / `systems` / `status`）与权限（`perm_key` 列表）**经 Glue HTTP API 获取**，
   请求路径上不再直读共享表；
3. 保留本地密码登录作为**可回退的第二轨**：切换与回退都是一行环境变量 + 重启，无需回滚镜像。

**非目标**：不改共享表结构；不改变 Glue 侧既有客户端（Jenkins / Harbor / GitLab / Grafana）的行为；不引入新数据库或外部缓存。

## 2. v1 决策与非目标

| 项 | v1 决策 | 依据 / 理由 |
|---|---|---|
| 授权模式 | **BFF**（服务端机密客户端 + Authorization Code） | CD 与 SPA 同源（`main.py:195/275` 托管 `static/`），会话走 HttpOnly `cd_token` cookie |
| PKCE | **不做** | 机密客户端，`code` 兑换在服务端完成，BFF 下 PKCE 无额外收益 |
| `end_session` | **不做** | Glue `OAuthController::authorizeSubmit`（`:66-127`）只签发 `code`，**不创建任何会话/cookie** —— 无物可登出。CD 登出仍是「删 `cd_sessions` 行 + 删 cookie」 |
| `refresh_token` | **不做**（转 P2） | CD 不持有用户级 Glue token（见 §5）；且 Glue `issueAccessToken:177-192` 未校验账号状态，刷新分支必须先 `refreshAccount()` 再签发 |
| SPA 直连 IdP | **不做** | 需把 `client_secret` 下发到浏览器，机密客户端语义不成立 |
| id_token 的 `systems` claim | **P2** | 准入所需的 `systems` 已由 `GET /api/rbac/users/{u}` 一次返回，claim 只是省一次调用 |
| 权限端点返回 404（角色悬空） | **fail-closed 拒绝**，不降级为「空权限」 | 404 与「角色存在但零权限」是两个不同事实：合并即把用户静默收敛成零权限、且无任何可诊断信号（Glue 侧 `RbacController::rolePermissions:281-287` 已按此收窄，§8.4） |
| 权限清单含 `"*"` | **按通配放行** | Glue 对 `super_admin` 返回 `["*"]`（其权限由代码授予、不落 `role_permissions` 表，照表返回会读成零权限），CD 必须认这个通配，不能只靠角色名比对（§7.2） |
| 用 `userinfo` / access_token 维持长会话 | **不用** | Glue 的 access_token 受 `ACCESS_TOKEN_TTL=3600` 限制，撑不起 24h CD 会话 → 会话时效由 CD 侧 `AUTH_TOKEN_TTL_HOURS` 决定，Glue 只做周期校验 |

## 3. 角色与数据流

### 3.1 登录（Authorization Code + BFF 回调）

```text
浏览器                     CD（BFF，同源）                          Devops-Glue
  │  GET /api/auth/oidc/start
  │────────────────────────>│ cd_deploy_gate() 先过闸门（未启用/未部署一律 503）
  │                         │ state = token_urlsafe(32); nonce = token_urlsafe(32)
  │                         │ Set-Cookie: cd_oidc_state=<state>.<nonce>（HttpOnly / Lax / 600s）
  │  302 → /oauth/authorize?response_type=code&client_id=cd&
  │        redirect_uri=…&scope=openid profile email&state&nonce
  │<────────────────────────│
  │  （用户登录并授权；P0-0 修复后 systems='cd' 的账号才能通过）
  │  302 → /api/auth/oidc/callback?code=…&state=…
  │────────────────────────>│ ① 校验 state（与 cookie 比对，单次使用）
  │                         │ ② POST /oauth/token（HTTP Basic: cd + secret；
  │                         │    grant_type=authorization_code&code&redirect_uri）
  │                         │───────────────────────────────────────────>│
  │                         │<───────────────────────────────────────────│ {access_token, id_token, expires_in, scope}
  │                         │ ③ 验 id_token：JWKS(RS256) + iss + aud=cd + exp + nonce → username
  │                         │ ④ GET /api/rbac/users/{username}（Bearer dg_ 服务 token）
  │                         │───────────────────────────────────────────>│ {username, role, systems, status}
  │                         │ ⑤ 准入：status≠0 且 systems 含 cd（复用 _has_system / ALLOW_EMPTY_SYSTEMS）
  │                         │ ⑥ GET /api/rbac/roles/{role}/permissions（Bearer dg_）
  │                         │───────────────────────────────────────────>│ {role, permissions:[…]}
  │                         │    （404 角色悬空 即 fail-closed，见 §8.4）
  │                         │ ⑦ _create_session() → Set-Cookie: cd_token → 302 /
  │  302 → /                │
  │<────────────────────────│
  │  失败一律 302 → /login?error=<error_key>（含 404 角色悬空 即 errors.identity_unavailable）
```

> `access_token` 拿到后**直接丢弃**：CD 后续所有 Glue 调用都用 `dg_` 服务 token（`CI_API_TOKEN`，不过期）。

> **登录入口与失败呈现**（实现见 §7.1）：
> · 入口统一为 `/login`：落地页导航栏与登录页的登录按钮都指向 `/login`，由登录页按 `/api/info` 的 `auth_mode`
>   决定展示 SSO 按钮还是本地表单，避免两处入口分叉。
> · 模式未知（`/api/info` 未返回）时登录页不渲染任何一态，避免首帧先画本地表单、随后切成 SSO 卡片的闪动；
>   探测结束（`settled`）且模式不是 `oidc`（含探测失败）才渲染本地表单。
> · 失败分流：CD 按 `Accept` 区分整页导航（含 `text/html`）与 XHR / fetch / 脚本。导航下所有失败一律
>   `302 /login?error=<key>`，由登录页在卡片下方提示；XHR 下保持 400/502/503 + JSON（带 `error_key`，供 `useError` 翻译）。
>   用 `Accept` 而非 `Sec-Fetch-*`：后者在老浏览器与部分代理链路下可能缺失，缺失即误判为非导航，退化成裸 JSON 页。
> · 错误键：`errors.cd_not_enabled`（Glue 未启用 CD）/ `errors.cd_not_deployed`（表没建全）/ `errors.db_unavailable`（库不可用）/
>   `errors.oidc_not_configured`（必填项缺失，见 §6.3）/ `errors.sso_denied`（Glue 授权页取消或 Glue 会话失效）/
>   `errors.sso_state_invalid`（缺 `code` / `state`、`state` 不匹配、已重试过的 `invalid_grant`）/
>   `errors.sso_unavailable`（discovery / JWKS / token 端点不可用、错 secret、验签失败）/ `errors.user_disabled`（账号停用）/
>   `errors.no_cd_access`（`systems` 不含 `cd`）/ `errors.identity_unavailable`（账号被删 / 角色悬空）。
>   技术细节（issuer 双侧取值、上游报文）只进日志与 JSON 分支，不进用户可见文案。

### 3.2 已登录请求（惰性周期校验）

CD 不在每次请求打 Glue：`verify_token` / `get_current_user` 仍先校验 `cd_sessions` 行与会话有效期，
仅当**距上次校验超过 `OIDC_REVALIDATE_SECONDS`（默认 60）** 时，用进程内身份缓存
（key = `username`，含 `role` / `systems` / `status` / `permissions` / `checked_at`）向 Glue 复核：

```text
命中缓存且未超 60s → 直接放行（零 Glue 调用）
超 60s → GET /api/rbac/users/{username}
          ├ status=0 或 systems 不含 cd → 删会话行 + 401（前端 useAuth.handle401 → logout）
          └ role 发生变化 → 重新取该角色权限并刷新缓存
Glue 不可达 → 软失败窗口（OIDC_STALE_GRACE_SECONDS，默认 600）内用 last-known-good 放行；
              超过窗口 → fail-closed 401/503（策略可配，见 §6）
```

时钟字段只存于进程内（不落库）：CD 是单进程 uvicorn（`Dockerfile:26`，无 `--workers`），多副本场景也只是各副本各查一次，语义无损。

## 4. 关键机理（为什么这样设计）

### 4.1 身份以 `GET /api/rbac/users/{u}` 为准，而不是 id_token 的 claim

Glue 签发的 id_token claims 是：`iss` / `sub`（**= username**）/ `aud`（= `cd`）/ `iat` / `exp` / `nonce`（有则回显）/
`name` / `preferred_username`（= username）/ `email` / `email_verified` / `groups`（**= `[role]`**，角色为空则 `[]`）
—— 见 `OAuthController::buildIdTokenClaims:326-338` 与 `OidcService::signIdToken:219-241`（未列入 Glue 白名单的 claim
会被静默丢弃；见 §8.2 / §9 P2）。

**没有** `role` claim，**也没有** `status`。因此：

- `sub` / `preferred_username` 只用来确定 **username**；
- `role` / `systems` / `status` 一律以 rbac 端点为准（一次调用三项全拿到），id_token 的 `groups[0]` **v1 不用**，避免出现两个真值来源。

### 4.2 权限必须由 Glue 新增端点提供（不可选项）

`GET /api/rbac/roles` 只返回 `name` / `description`（`AdminUserRepository::listRolesForApi:119-124`），
**全系统不存在任何「角色 → 权限」的 API**：`TokenService::loadPermissions` 只在登录会话内部使用。
所以 `GET /api/rbac/roles/{name}/permissions` 是 **P0 必需项**，不是优化项 —— 否则 CD 拿不到 `perm_key`，
`require_perm` 全线失能。

**边界必须是 404 而不是 `[]`**（Glue 回签确认，见 §8.4）：该端点对不存在的角色返回 **404** `user.role_not_found`
（`RbacController:302-303`，匹配 `AdminUserRepository::roleExists:127` 的精确匹配），与「角色存在但零权限」严格区分。
CD 侧收到 404 即 **fail-closed**（拒绝 + 按角色名负缓存 + 限流日志），**不**把「查不到」当成「没有权限」——
后者会让角色名写错/角色被删变成一次**静默降权**。
另外 `super_admin` 返回 `['*']`（`RbacController:305-307`）即 CD 必须认这个通配（§7.2），
且 `role` 必须**原样透传**（Glue 只 `trim`，不做大小写归一）。

### 4.3 「停用即踢下线」「降权即时生效」为什么成立

- **Glue 侧**：`OAuthService::validateAccessToken` 每次都 `refreshAccount` 重查 `admin_users.status/role`
  （`OAuthService.php:190-193`、`:209-235`），所以 access_token 不是「权限快照」；
- **CD 侧**：惰性复核对 `status` 与 `role` 同样每次重查（§3.2），窗口 ≤ `OIDC_REVALIDATE_SECONDS`（默认 60s）。
  即 停用/降权**不需要等 CD 会话 24h 过期**，也不需要重启任何服务。

### 4.4 为什么「不再读共享表」是可靠的

OIDC 模式下，登录与每个请求的准入判据全部来自 Glue 的 HTTP 契约
（token + `/.well-known/*` + `/api/rbac/*`），CD 只在**部署闸门**里读 `integrated_services` 契约表
（这是「CD 是否启用」的开关，不是身份），其余读写的都是自有 `cd_*` 表。
即 共享表从「身份源」降级为「Glue 的内部实现」，CD 侧不再有耦合点。

### 4.5 OIDC 模式下仍会读共享表的清单（切档前必须知晓）

「不再读共享表」指的是**鉴权路径**（每请求）：`oidc` 模式下 `verify_token` / `get_current_user` / WebShell 握手
只读 `cd_sessions` + 调 Glue HTTP 接口。但下列与鉴权无关的读仍然存在，按「谁在读 / 何时读 / 是否受 `AUTH_MODE` 影响」列出：

| 共享表（Glue 侧） | 位置 | 何时读 | `AUTH_MODE=oidc` 下 |
|---|---|---|---|
| `integrated_services` | `backend/database.py`（`_CONTRACT_SQL`，`cd_integration_state()`） | **每个请求**（`cd_deploy_gate`） | **仍然读**——设计如此：契约行是「CD 是否启用」的唯一开关，**无缓存**，CI 一改下一个请求即生效 |
| `admin_users` | `backend/auth.py::_query_user_with_systems`（`:497-523`） | local 模式每次鉴权 | 不再读（改用 Glue `GET /api/rbac/users/{u}`） |
| `admin_users` | `backend/auth.py::load_user_context`（`:482`）→ `approval_service.py`（定时执行 / 崩溃恢复） | 后台执行已批准（含定时）部署单时重建执行身份 | **仍然读**（无 HTTP 上下文；改造需把 `glue_identity` 的服务 token 化 → §14 L2 第 1 项） |
| `admin_users` | `backend/routers/terminal.py::_ws_verify`（`:125`）与 `terminal()`（`:124-130`） | local 模式 WebShell 握手与权限判定 | 不再读（`_ws_verify` 已分支走 Glue 复核） |
| `roles` + `role_permissions` | `backend/auth.py::_query_permissions`（`:325`） | local 模式每次鉴权 | 不再读（权限来自 `GET /api/rbac/roles/{role}/permissions`，按角色缓存） |
| `roles` | `backend/routers/approvals.py`（`:288`） | **新增 / 编辑审批规则**时校验 `approver_role` 是否存在 | **仍然读**（业务校验，非鉴权；表不可达时跳过校验、不阻断规则写入） |
| `admin_users` | `backend/routers/approvals.py`（`:303`） | 同上，校验审批人是否存在 | **仍然读**（同上） |
| `cache` | `backend/login_guard.py`（`:101` / `:119` / `:132`） | local 模式登录失败计数（与 Glue 共用同一把锁） | 不再读（`/api/login` 已 403，`login_guard` 自然退场） |
| `information_schema` / `sqlite_master` | `backend/database.py`（建表清单自检、列/索引检查） | 启动与首次建表 | 仍然读（**元数据**查询，不涉及任何业务数据） |

> 结论：切到 `oidc` 后，**每请求**级的共享表读只剩 `integrated_services` 契约行（闸门）与数据库元数据；
> `admin_users` / `roles` / `role_permissions` 的**业务读**只剩「审批规则保存校验」与「后台身份重建」两处 ——
> 两者都不在登录 / 鉴权路径上，因此不存在「切档后仍按共享表判权限」的风险。

## 5. 零 schema 变更（本设计最关键的工程约束）

推理链（每一步都有代码依据）：

1. CD **不需要保存用户级 Glue token**：登录只需验 `id_token` 拿身份，之后所有 Glue 调用都用 `dg_` 服务 token
   即 没有 `access_token` / `refresh_token` 要落库，**不需要新增列**；
2. 「这是不是一个 OIDC 会话」也不需要落库标记：`AUTH_MODE` 是**全局开关**而非混合模式（§6），
   OIDC 模式下所有会话都来自 OIDC；
3. 「上次校验时间」放**进程内缓存**即可（单进程 uvicorn，重启后重查一次即可）；
4. 即 **`CD_REQUIRED_TABLES` 不动**。

**为什么必须避开加表**：闸门语义是「少任何一张清单表 = 未部署」，加一张表就会让**所有既有部署**立刻 503
（`manual` 模式还要运维重跑 `database/init_mysql.sql`）。即 本次改造不引入任何 DDL，升级即是「换镜像 + 改开关」。

## 6. 双轨开关与回退

### 6.1 开关

```ini
# config/app.env（业务参数，随镜像发布；容器内改完重启生效）
AUTH_MODE=oidc                     # oidc（默认，登录方式）| local（本地密码登录，可回退）
OIDC_ISSUER=                       # Glue 对外地址，须与其 discovery 的 issuer 完全一致；按环境在根 .env 覆盖
OIDC_REVALIDATE_SECONDS=60         # 惰性复核窗口
OIDC_STALE_GRACE_SECONDS=600       # Glue 不可达时的软失败窗口（0 = 立即 fail-closed）
OIDC_ID_TOKEN_MAX_SKEW=60          # iat/exp 时钟偏移容忍
```

```ini
# 根 .env（部署层，与 CI_API_TOKEN 同类，不进镜像）
CD_OIDC_SECRET=                    # 必须与 Glue oauth_clients['cd']['secret'] 完全一致
CD_OIDC_REDIRECT_URI=              # 必须与 Glue 侧登记值逐字符一致
```

### 6.2 行为矩阵

| 行为 | `AUTH_MODE=local` | `AUTH_MODE=oidc` |
|---|---|---|
| `POST /api/login`（密码） | 现状不变 | **403** `errors.local_login_disabled`（代码保留，回退即恢复） |
| `/api/auth/oidc/start` / `callback` | XHR/脚本 404；**整页导航 302 `/login`**（不改可用性判据：local 下路由不存在，但整页访问不给用户看裸 JSON 404） | 启用 |
| `verify_token` / `get_current_user` / WebShell `_ws_verify` | 现状：读 `admin_users` + `roles/role_permissions` | 只校验 `cd_sessions` + 惰性复核 Glue |
| 共享 `cache` 表登录失败锁（`login_guard`） | 现状不变 | 自然退场（`/api/login` 不再执行） |
| `/api/me` 响应结构 | `{username, role, systems, permissions}` | **同左（不变）**；但前端仍有**一处**改动：OIDC 登录是**整页跳转**回 SPA（`sessionStorage` 无本地标志）即 启动需自举一次（见 §7.2 末行） |
| 登录入口（前端） | 登录页本地表单（直接访问 `/login`） | 登录页显示 SSO 按钮（整页跳转 `/api/auth/oidc/start`）；落地页导航栏与登录页入口统一为 `/login`（§3.1） |
| 必填项缺失 | 不涉及 | 启动日志记 ERROR；compose 预检中止启动；点 SSO → `302 /login?error=errors.oidc_not_configured&missing=...`，XHR / 脚本 → 503 + `error_key`（§6.3） |
| 登录失败的呈现 | 登录页 `error` 框 + `useError` toast | **同左，只是入口统一**：导航失败一律 `302 /login?error=<key>` 在卡片下方弹提示；XHR/脚本仍拿 400/502/503 + JSON。**不再出现被浏览器渲染成裸文本的 JSON 错误页** |
| SPA `index.html` 缓存 | 常规 | **`Cache-Control: no-cache, no-store, must-revalidate`**：构建后旧 HTML 不会继续指向已删除的 hash 资源（避免白屏） |
| 回退代价 | — | 改回 `local` + 重启（≤30 秒，无需回滚镜像） |

### 6.3 必填项缺失的处理（fail-closed）

`AUTH_MODE=oidc` 且 `OIDC_ISSUER` / `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` 任一为空时，三处提示同一份缺失清单：

| 位置 | 行为 |
|---|---|
| compose 预检（`env-check` 服务） | 启动前中止，打印中英双语缺失清单（与 `SECRET_KEY` 检查同构） |
| 应用启动（`main.py`） | 记 ERROR 日志，不中止启动（裸机路径仍需可探活排障） |
| 登录页 | 点 SSO → `302 /login?error=errors.oidc_not_configured&missing=...`，在卡片下方列出缺失项；XHR / 脚本 → 503 + `error_key` |

理由与 Glue 的客户端过滤器一致：半配的 OIDC 会用公开默认密钥带病运行。

### 6.4 切换 / 回退步骤（运维动作）

| 动作 | 命令 / 步骤 |
|---|---|
| 切到 OIDC（默认档，无需改开关） | ① 根 `.env` 填 `OIDC_ISSUER` / `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI`；② 确认 `config/app.env` 的 `AUTH_MODE=oidc`；③ `docker compose up -d --build`；④（建议）清空在途本地会话：`DELETE FROM cd_sessions;` |
| 回退到本地 | `AUTH_MODE=local` + 重启。旧 OIDC 会话在 TTL 内仍有效（可同样清 `cd_sessions` 强制重新登录） |
| 只验证不切换 | 保持 `local`，直接访问 `/api/auth/oidc/start` 走通整条链路（start/callback 与 `/api/login` 互不影响） |

> 清会话是**唯一**需要运维执行的动作，一行 SQL、无 schema 变更（因实现零 DDL，不需要导入任何脚本）。

## 7. 实现清单（文件级）

> 行锚点基于 **v1.6.0（`45fc553`）** 基线。本次起 `AUTH_MODE=oidc` 为默认档（见文首）；改回 `local` 即完全回到 v1.6.0 行为。

### 7.1 新增

| 文件 | 职责 | 关键点 |
|---|---|---|
| `backend/oidc.py` | OIDC 客户端：discovery / JWKS 缓存、`authorize` URL 组装、`token` 兑换、id_token 验签 | 用**已在依赖里的 `cryptography`** 手写 RS256 校验（不引入 PyJWT）；未知 `kid` 只刷新 JWKS 一次；iss/aud/exp/iat-skew/nonce 全验 |
| `backend/services/glue_identity.py` | 身份与权限的 Glue 客户端 + 进程内 TTL 缓存 + 惰性复核 | 复用 `ci_client.py` 的 `requests.Session` + Retry 形态；**不加 `?format=json`**；只按 HTTP 状态码判错；软失败窗口后 fail-closed；**角色权限 404 ≠ 空权限**：fail-closed + 按角色名负缓存（TTL 同复核窗口）+ 限流 WARN（含角色名）；`permissions` 含 `"*"` 时通配放行；`role` **原样透传**（不 lower），仅 username 做 `strip().lower()` |
| `backend/routers/oidc.py` | `GET /api/auth/oidc/start`、`GET /api/auth/oidc/callback` | 先过部署闸门；state/nonce 单次消费并清 cookie；**失败按 `Accept` 分流**：整页导航 → `302 /login?error=<error_key>`（登录页弹提示），XHR/脚本 → 400/502/503 + JSON（见 §3.1「失败分流」） |
| `backend/access_log.py` | uvicorn 访问日志脱敏：`/api/auth/oidc/*` 与 `/ws/terminal` 的**敏感参数值**替换为 `<masked>` | `code` / `state` / `nonce` / `token` 等键才替换，路径、其余参数（如 `?retry=1`）、状态码照常记录；只在确有敏感参数时改写 record（普通路由零分配）；挂在 `uvicorn.access` logger 上，三种启动方式都覆盖（见 §10） |
| `backend/tests/test_oidc_client.py` | OIDC 客户端（discovery / JWKS / token / id_token 验签）回归 | 授权码流与 id_token 校验：state 重放/缺失、**token 请求必须含 `redirect_uri`（mock 断言）**、iss/aud/exp/nonce/签名各失败分支、`invalid_grant` 语义、cookie 属性 |
| `backend/tests/test_oidc_login.py` | 身份缓存 / 登录路由 / 双轨鉴权回归 | 60s 窗口命中/未命中、降权即时生效、停用踢下线（清会话行）、软失败窗口、fail-closed、Glue 恢复；路由层：local 模式 404、state 单次使用、`invalid_grant` 静默重走一次、准入失败回 `/login?error=<error_key>`、**导航 vs XHR 的失败呈现分流（9 例）** |
| `backend/tests/test_access_log.py` | 访问日志脱敏回归 | 敏感值被替换、`<masked>` 之外的路径/参数/状态码保留、非 SSO 路由零改写、WS dict 形态入参不被就地改写、`install_access_log_filter()` 幂等 |

### 7.2 修改（含行锚点）

| 文件 | 位置 | 改动 |
|---|---|---|
| `backend/config.py` | `:48`（`allow_empty_systems`）之后 | 新增 `auth_mode` / `oidc_issuer` / `oidc_client_id="cd"` / `oidc_client_secret` / `oidc_redirect_uri` / `oidc_scopes` / `oidc_revalidate_seconds` / `oidc_stale_grace_seconds` / `oidc_id_token_max_skew`；`AUTH_MODE` 白名单校验；`oidc_ready()` 自检 |
| `backend/auth.py` | `:155-187` `verify_token` | 增 `AUTH_MODE=oidc` 分支：只校验 `cd_sessions` 行 + 惰性复核；local 分支原样保留 |
| `backend/auth.py` | `:190-228` `get_current_user` | 同上分支；**返回结构不变**（`{username, role, systems, permissions}`），权限来自 Glue（按角色缓存） |
| `backend/auth.py` | `:78-95` | `_check_cd_access` / `_check_disabled` 保留给 local；OIDC 侧复用同一 `_has_system` / `CD_SYSTEM` 语义（`ALLOW_EMPTY_SYSTEMS` 行为一致） |
| `backend/auth.py` | `:244-253` `require_perm`、`:290-302` `enforce_deploy_perm` | 追加**通配放行**：权限清单含 `"*"` 即 直接放行。Glue 对 `super_admin` 返回 `["*"]`，而 `config/AppConfig.php:59` 的 `ROLE_SUPER_ADMIN='super_admin'` 虽与 CD `SUPER_ADMIN_ROLE` 同名（现有角色名判断也命中），但**不能只靠名字**——`SUPER_ADMIN_ROLE` 是运维可改配置，且角色名一旦改名就失效。local 模式权限永不含 `*`，零影响 |
| `backend/auth.py` | `:331-369` `authenticate` | **不改**（local 专用） |
| `backend/routers/auth.py` | `:37-43` 之间 | 在 `cd_deploy_gate` 之后、登录失败锁查询**之前**插入模式守卫：`oidc` → 403 `errors.local_login_disabled`（保证不触碰共享 `cache` 表） |
| `backend/routers/terminal.py` | `:58-95` `_ws_verify` | **必须同步分支**（WebShell 是独立鉴权路径，否则 OIDC 模式下 WS 会去读 `admin_users`） |
| `main.py` | `:164` 附近 | `app.include_router(oidc.router)` |
| `main.py` | `:44-99` `lifespan` | 启动日志打印 `AUTH_MODE`；OIDC 必填项缺失时记 ERROR 并列出缺失项与配置位置（不中止启动） |
| `main.py` | `:206-235` `/api/info` | 追加 `"auth_mode": settings.auth_mode`（**只暴露模式，不含任何密钥**），供登录页决定渲染形态 |
| `config/app.env` | `:38`（`SUPER_ADMIN_ROLE`）之后 | 新增 §6.1 的 5 个非密项（含注释说明） |
| `.env.example` | `:65`（`CI_API_TOKEN`）之后 | 新增 `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` 及「留空 = 未启用」语义说明 |
| `docker-compose.yml` | `:45-62` 启动脚本区；`:79-90` env 区 | ① `AUTH_MODE=oidc` 而 OIDC 关键项为空 → **启动中止**（与 `SECRET_KEY` 检查同构，中英双语）；② 透传 `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` |
| `frontend/src/views/LoginView.vue` | 模板 + `onMounted` | 卡片形态随模式切换：`oidc` 渲染「使用 Devops-Glue 登录」（`<a href="/api/auth/oidc/start">`，整页跳转）并折叠本地表单；`local` 渲染密码表单。挂载时读 `route.query.error`（必填项缺失时另带 `missing` 清单）→ 复用 `te(key)/t(key)` 策略并插值，随后清除 query；模式未知（`settled=false`）时不渲染任何一态；错误框置于两种形态之外、两态共用。后端全部失败键都落到这个提示框 |
| `frontend/src/views/LandingView.vue` + `frontend/src/composables/useAppInfo.js` | `useAppInfo` 暴露 `authMode`（读 `/api/info.auth_mode`，失败静默）与 `settled`（探测是否已有结论，成功/失败都置位） | 落地页导航栏登录按钮固定为 `router-link to="/login"`：登录入口单一化，形态由登录页决定（`authMode` 只服务登录页） |
| `main.py` | `:145-148`（app 创建后） | `install_access_log_filter()`：uvicorn 访问日志脱敏（§10） |
| `main.py` | `:294-306` SPA 路由 | `index.html` 追加 `Cache-Control: no-cache, no-store, must-revalidate`（+ `Pragma` / `Expires`）：构建后旧 HTML 不再被浏览器复用，避免它继续指向已删除的 hash 资源而白屏 |
| `frontend/src/locales/zh.json` / `en.json` | — | **新增 4 个 error key**：`errors.oidc_not_configured`（含 `{items}` 插值，登录页列出缺失项）/ `errors.sso_denied` / `errors.sso_state_invalid` / `errors.sso_unavailable`（中英同步）。此前已加的 5 个 key（`login.ssoBtn` / `login.ssoHint` / `login.localDisabled` / `errors.local_login_disabled` / `errors.identity_unavailable`）与既有 `errors.cd_not_enabled` / `cd_not_deployed` / `db_unavailable` / `no_cd_access` / `user_disabled` 全部复用 |
| `backend/tests/test_auth_login.py` | `:95` 之后 | 追加「OIDC 模式下 `/api/login` 403 且不查锁/不计数」1 例 |
| `frontend/src/composables/useAuth.js`、`frontend/src/App.vue`、`frontend/src/router/index.js` | `useAuth.js` 新增 `bootstrap()`；`App.vue` 启动时调用；`router/index.js` 非公开路径先 `await bootstrap()` | **OIDC 登录是整页跳转**（Glue → 302 → `/`），新文档里 `sessionStorage.cd_authed` 为空 即 仅凭本地标志会把已登录用户误判成未登录：渲染落地页、深链被守卫踢到 `/401`（真机日志实测出现 `/401` + `/api/info` 循环）。改为启动时用一次 `/api/me` 让 cookie 会话自证：**200 即 进入已登录态并补写本地标志；401 即 维持未登录渲染**。local 模式仅在「本来就要被踢」的路径上多一次 `GET /api/me`（401），结果不变 |
| `docs/` | 见 §13 | 本文档 + `ADMIN_MANUAL(_ZH)` / `ARCHITECTURE(_ZH)` / `FAQ(_ZH)` / `USER_MANUAL(_ZH)` / `CHANGELOG(_ZH)` 同步 |

### 7.3 不改（避免无谓回归面）

| 部分 | 原因 |
|---|---|
| `backend/auth.py::_create_session` / `logout` | 会话机制与登出语义（删行 + 删 cookie）在两种模式下都正确；**不调 Glue `end_session`**（§2） |
| WebSocket 客户端 | `/api/me` 结构与 `cd_token` cookie 均不变，无需改动。（`useAuth` / 路由守卫**不在**本表 —— OIDC 整页回跳需要启动自举，见 §7.2 末行） |
| `backend/login_guard.py`、共享 `cache` 表 | local 模式继续使用；OIDC 模式下自然不再进入（L2 可删） |
| `backend/services/approval_service.py::load_user_context` | 定时执行/崩溃恢复在后台重建身份，无 HTTP 上下文 —— 见 §14 的 L2 清单 |

## 8. 接口冻结契约（CD ↔ Glue）

> CD 侧实现严格按本表；「不带 `?format=json`」「错误只按状态码」是**硬约束**。

### 8.1 `POST /oauth/token`

| 项 | 值 |
|---|---|
| 认证 | **HTTP Basic**（`client_secret_basic`；Glue `OAuthController:144-153` 支持） |
| Body | `grant_type=authorization_code`、`code`、**`redirect_uri`（必须逐字符重传：`consumeCode($code,$clientId,$redirectUri)` 精确比对）**、`scope=openid profile email` |
| 成功 200 | `{"access_token":…,"token_type":"Bearer","expires_in":3600,"id_token":…,"scope":…}`（`Cache-Control: no-store`） |
|  | **`scope` 不含 `openid` 就不下发 `id_token`**（`hasOpenidScope:302-309`）——不报错，只是没有身份 |
|  | `expires_in` 是**字面量 3600**（`:179`），与 access_token 真实 TTL 可能漂移 即 **CD v1 不依赖该字段** |
| 失败 | `{"error":"invalid_client"}` 401（密钥错）/ `{"error":"invalid_grant"}` 400（code 无效或已用、**redirect_uri 不匹配**）/ `unsupported_grant_type` 400 |

### 8.2 id_token claims（实锤）

| claim | 值 | CD 用途 |
|---|---|---|
| `iss` | discovery 的 issuer | 必须校验 |
| `sub` | **= username** | username 取值 |
| `aud` | **= `cd`** | 必须校验 |
| `iat` / `exp` | `exp = now + OIDC_ID_TOKEN_TTL`（默认 3600） | 校验（容忍 `OIDC_ID_TOKEN_MAX_SKEW`） |
| `nonce` | 授权请求原样回显 | **必须校验** |
| `preferred_username` / `name` | = username | username 取值（优先 `preferred_username`） |
| `email` / `email_verified` | 可选；**可能是占位值** `username@devops-glue.local` 且 `email_verified=false`（`resolveEmailInfo:289-299`） | CD 不用 —— **不可作为身份标识** |
| `groups` | `= [role]` | v1 不用（role 以 rbac 端点为准）；`role` **不在 id_token**，只在 `/oauth/userinfo`（且需 **OAuth** Bearer token，不能用 `dg_`；该 token 也**不能**调 `/api/*`）即 CD v1 不调 userinfo |

### 8.3 `GET /api/rbac/users/{username}`

```text
请求：GET /api/rbac/users/{username}          Authorization: Bearer dg_…
       不要带 ?format=json           username 先 strip().lower()（Glue 侧 strtolower 归一）
成功 200（裸对象，无包裹）：
      {"username":"zhangsan","role":"dev","systems":"cd","status":1}
      · systems：逗号分隔字符串（可能为 ""）；status：1=启用 / 0=停用（int）
失败：404 {"code":404,"message":"<已翻译文本>"}；其余错误同格式
       硬约束①：`role` **原样透传**（Glue 精确匹配 roles.name、只 trim，见 §4.2）——不要小写归一
       硬约束②：404（`user.not_found`）= 身份已消失 即 与角色 404 同等对待：
                   登录阶段拒绝、复核阶段删会话 + 401，且**不缓存成功结果**
```

### 8.4 `GET /api/rbac/roles/{role}/permissions`（**Glue 新增，P0**）

```text
请求：GET /api/rbac/roles/dev/permissions      Authorization: Bearer dg_
成功 200（裸对象）：{"role":"dev","permissions":["cd.deploy","cd.deploy.approve","cd.registry.read"]}
```

| 场景 | HTTP | Body | CD 侧处理 |
|---|---|---|---|
| 正常 | 200 | `{"role":"dev","permissions":[…真实 perm_key…]}` | 按 `permissions` 判权 |
| 角色存在但零权限 | 200 | `{"role":"x","permissions":[]}` | deny-by-default（**这是真实事实**，正常放行为「无权限」） |
| `super_admin` | 200 | `{"role":"super_admin","permissions":["*"]}` | **通配放行**（§7.2）——该角色权限由代码授予、不落 `role_permissions` 表 |
| **角色不存在** | **404** | `{"code":404,"message":"角色不存在"}`（key = `user.role_not_found`；`message` 已翻译，**不可解析**） | **fail-closed**：拒绝 + 按角色名负缓存 + 限流 WARN（含角色名），**不**降级为空权限 |
| `role` 为空 / 非 API token / 内部错误 | 400 / 403 / 500 | `{"code":…,"message":…}` | fail-closed（与软失败窗口策略一致，§6） |

**为什么 CD 接受 404 而不是「200 + `[]`」**：两者是两个不同事实。若把「角色名写错 / 角色被删」也返回 `[]`，
CD 就会把「查不到」当成「没有任何权限」——结果是**静默降权**（用户已登录、界面正常、所有操作 403），
且**没有任何可诊断信号**。所以：

- CD 侧遇 404 即 视为「身份不可用」：登录阶段 → `302 /login?error=errors.identity_unavailable`；
  已登录复核阶段 → 删会话行 + 401（前端回登录页，错误文案仍是 `errors.identity_unavailable`）；
- **负缓存 + 限流**：同一角色名的 404 在 `OIDC_REVALIDATE_SECONDS` 窗口内只查一次、只打一条 WARN，
  避免「降权瞬间刷日志」；角色行恢复后 ≤ 一个窗口自动恢复；
- 排查辅助（可选）：需要人工核对角色目录时用 `GET /api/rbac/roles`（只回 `name` / `description`）。

实现建议：复用 `TokenService::loadPermissions($role)`；**零新 scope**（`ApiScopeResolver:45-47` 对 `/api/rbac/*` 全前缀判定 `rbac.user.write`，子路径自动覆盖）。
另：`role` **原样透传**（Glue `rolePermissions:297` 只 `trim`、`roleExists:127` 精确匹配 `roles.name`），
**不要**对它做小写归一（只有 username 需要，见 §8.3）。

### 8.5 `/.well-known/openid-configuration` 与 `/.well-known/jwks.json`

| 字段 | CD 用途 |
|---|---|
| `issuer` | 与配置的 `OIDC_ISSUER` 比对（**不一致即拒绝**，不静默信任）；不一致时日志**同时打印** discovery 的 issuer 与期望值（该坑最耗时，见 §12 F） |
| `authorization_endpoint` / `token_endpoint` | 拼请求（**只信 discovery**，不硬编码路径） |
| `jwks_uri` | 取公钥；支持 `kid` 轮换（未知 `kid` 刷新一次） |
| `id_token_signing_alg_values_supported` | 必须含 `RS256` |
| JWKS `keys[]` | `kty=RSA` / `use=sig` / `alg=RS256` / `kid` / `n` / `e`（base64url） |

### 8.6 CD 的调用白名单（Glue 侧需放行）

| 端点 | 方法 | 凭据 | scope |
|---|---|---|---|
| `/oauth/authorize` | GET | — | 公开 |
| `/oauth/token` | POST | Basic(`cd`, secret) | — |
| `/.well-known/openid-configuration`、`/.well-known/jwks.json` | GET | — | 公开 |
| `/api/rbac/users/{username}` | GET | `dg_` 服务 token | `rbac.user.write` |
| `/api/rbac/roles/{role}/permissions` | GET | `dg_` 服务 token | `rbac.user.write` |

> Glue **从不回调 CD**（即 §3.1 的方向是单向的：只有 CD → Glue）。运维只需按上表放行
> **CD → Glue 的 5 个入站路径**；其中 `/oauth/authorize` 的 **GET 与 POST 都公开**（表单页与提交均无会话前置检查），
> 且它失败时返回的是 **401 + HTML**（§8.7 第 9 条）。

### 8.7 十个易错点（双方共用）

| # | 坑 | 现象 |
|---|---|---|
| 1 | 加 `?format=json` | 响应被包成 `{"data":{…}}`，解析拿空 |
| 2 | 用 `message` 文本判错 | Glue 文案/语言一变，逻辑静默失效（`message` 是已翻译文本，不是稳定 key） |
| 3 | scope 漏 `openid` | **响应里没有 `id_token`**（不报错） |
| 4 | `redirect_uri` 漏传或不一致 | `invalid_grant`（看起来像 code 问题） |
| 5 | `iss` / `jwks_uri` 用内网推导值 | 验签失败（易误判为密钥问题） |
| 6 | username 未小写归一 | rbac 端点 404 |
| 7 | 把 `expires_in` 当 access_token 真实 TTL | 它只是「令牌寿命」口径，与 CD 会话（`AUTH_TOKEN_TTL_HOURS`）无关。Glue 已按 CD 建议改引 `ACCESS_TOKEN_TTL` 常量（`OAuthService:27` / `68181a4`，不再漂移），但 **CD v1 仍不读该字段** |
| 8 | 对 `role` 做小写归一 | Glue `roleExists:127` 精确匹配 `roles.name`、`rolePermissions:297` 只 `trim` 即 归一即 404（**只有 username 需要 `strip().lower()`**） |
| 9 | 解析 `/oauth/authorize` 的错误体 | 它失败时是 **401 + HTML**（`authorizeSubmit:110`），不是 JSON 即 CD 只在 `/oauth/token` 上按 JSON + 状态码判错 |
| 10 | 忽略授权码 TTL（**仅 60s**，`OAuthService::CODE_TTL:19`） | 用户在 Glue 授权页停留过久 即 `invalid_grant`；CD 遇到它应**静默重走 `/api/auth/oidc/start`**（一次跳转），不要当错误页展示 |

## 9. Glue 侧前置项

| # | 项 | 状态 | 影响 |
|---|---|---|---|
| **P0-0** | 按 OAuth 客户端决定 `systemType`（`oauth_clients['cd']` 增加 `'system' => env('CD_OIDC_SYSTEM','cd')`；`OAuthController:98` 用 `clientSystem($clientId) ?: AppConfig::SYSTEM_CI`） | ✅ **已实现**（Glue `d75bde7`；**已于 2026-10-12 本地快进合入 Glue `release/2.9.0`，零推送** —— 远端仍 `a08eb74`，切档前须确认目标环境实际部署的构建包含 P0） | 不修则**所有** CD 账号登录必失败。且这是**默认路径而非边缘场景**：CD 侧无任何建号入口（`routers/users.py` 只读、`ci_client.rbac_users/rbac_roles` 只读、前端 21 个视图无用户管理、`ADMIN_MANUAL_ZH.md:464/476` 明确「CD 不提供账号管理」），而 Glue 两个建号入口都落 `systems='cd'`（`AdminController::userCreate` 默认 `SYSTEM_CD`、`RbacController::userCreate:99` 硬编码）。未注册 `system` 键的既有客户端默认落 CI 即 Jenkins/Harbor/GitLab/Grafana **零行为变化** |
| **P0-1** | 注册 `cd` 客户端（`secret` + `redirect_uri` 与 CD 侧 `CD_OIDC_REDIRECT_URI` **逐字符一致**） | ✅ 已实现（同上） | 否则 `invalid_client` / `invalid_grant` |
| **P0-2** | `cd` 客户端密钥按既有方式存储与校验（sha256） | ✅ 已实现（同上） | 密钥校验不过 |
| **P0-4** | 新增 `GET /api/rbac/roles/{role}/permissions`（§8.4） | ✅ 已实现（同上；形状与 §8.4 一致，含 404 / `['*']` 语义） | CD 拿不到 `perm_key`，`require_perm` 全线失能 |
| **P0-5** | **显式设置 `OIDC_ISSUER=<对外地址>`**（**Devops-Glue 仓库的** `config/app.env:164` 现为空 = 运行时从请求 host 推导） | ✅ 已落地（`68181a4`：`config/app.env:164` / `config/app.env.example:190` 注释 + 中英 SSO 手册同步；**有意不读 `X-Forwarded-*`**——可伪造头会污染 discovery，取向与 `ClientIp` 只在可信反代时才信 XFF 一致） | 推导值可能是内网地址 即 ① CD 的 `iss` 校验失败；② discovery 的 `jwks_uri` 指向内网 即 取不到公钥（表现为「验签失败」，极易误判为密钥问题） |

> **联调第 0 项（必做）**：`curl -s <issuer>/.well-known/openid-configuration | jq .issuer,.jwks_uri` ——
> 两者**都必须是对外地址**；否则表现为「验签失败」，极易误判成密钥问题（§12 F）。

**P2（v1 不依赖）**：

- **id_token `systems` claim**：必须**同时**改 **4 处**，否则**静默丢弃**（不报错）——① `OAuthController::buildIdTokenClaims:326-338`（组装）、
  ② `OidcService::OPTIONAL_CLAIMS:46` 的**透传白名单**（关键，`signIdToken:232-238` 是唯一消费点）、
  ③ `OidcService::CLAIMS_SUPPORTED:30-33`（discovery 声明）、④ **`OAuthController::userinfo:221-230` 的独立返回数组**。
  Glue 已于 `68181a4` 把白名单结构化为 `OPTIONAL_CLAIMS`（`OidcService:46`，其注释即列出这 4 处）并加 **tripwire 测试**：
  `tests/Unit/OidcServiceTest.php:188`（`testClaimOutsideWhitelistIsSilentlyDropped`）当前断言 `systems` 被丢弃，
  P2 落地时该测试必须变红，强制四处同改——这是本次回签最有价值的一条护栏。
- **`refresh_token`**：`issueAccessToken:177-192` 未校验账号状态 即 刷新分支必须先 `refreshAccount()`（`status=0` → `invalid_grant`）。
- ~~`expires_in => 3600` 字面量~~ —— **已由 P0 收口，不再是 P2 项**（Glue 侧要求）：`OAuthController:181` 已引用
  `OAuthService::ACCESS_TOKEN_TTL`（`:27` 提升为 `public`，`68181a4`），该值不会再漂移。CD v1 **仍不读**该字段（§8.7 第 7 条）。
- **存量 `'cd'` 账号盘点/修复**：`AdminUserRepository::updateUser($username, ?$passwordHash, ?$role, ?$email)` **没有 `systems` 参数** 即 已建成的 `'cd'`-only 账号没有 API 修复路径（只能 SQL 或重建）。

## 10. 安全不变量

| 不变量 | 保障方式 |
|---|---|
| 浏览器永不接触 Glue 凭据 | BFF：`client_secret` 只在服务端；会话是 CD 自签的 HttpOnly cookie（`cd_token`） |
| 授权码只兑一次 | Glue `consumeCode` 消费即失效；CD 侧 `state` 单次消费 |
| CSRF / 重放 | `state` + `nonce` 双校验（nonce 回显比对） |
| 令牌泄露面最小 | `id_token` 验完即丢弃；`access_token` 直接丢弃，**不落库、不落日志** |
| 越权登录 | 准入三判据：`systems` 含 `cd`（复用 `ALLOW_EMPTY_SYSTEMS` 语义）+ `status≠0` + 角色权限 deny-by-default |
| 停用/降权时效 | ≤ `OIDC_REVALIDATE_SECONDS`（§4.3），无需等待会话 TTL |
| 上游不可用 | 软失败窗口后 fail-closed；配置不全**启动即中止**（§6.3） |
| 权限同步异常 | 角色 404 / 权限端点异常 即 fail-closed（§8.4）；**不允许**降级为「空权限放行」——静默降权比拒绝登录危险得多 |
| 部署状态不可绕过 | `/api/auth/oidc/*` 与 `/api/login` 一样先过 `cd_deploy_gate` |
| 信息面 | `/api/info` 只暴露 `auth_mode`；不暴露 issuer/secret/redirect_uri |
| 用户可见文案不含技术细节 | 导航失败只带 i18n key（`/login?error=<key>`）；issuer 双侧取值、上游报文只进服务端日志与 XHR 的 JSON 分支 |
| 凭据不落日志 | **访问日志脱敏**（`backend/access_log.py`）：`/api/auth/oidc/*` 与 `/ws/terminal` 的 `code` / `state` / `nonce` / `token` 等参数**值**替换为 `<masked>`，路径、其余参数（如 `?retry=1`）、状态码照常记录；覆盖 `uvicorn main:app`、`python main.py`、`--workers N` 三种启动方式（uvicorn 的 dictConfig 不清空 logger 上已注册的 filters） |
| 前端资源一致性 | SPA `index.html` 禁缓存（`no-store`）+ 静态资源按 hash 缓存 即 不会出现「旧 HTML 指向已删除的 bundle」的白屏 |

## 11. 测试矩阵

| # | 分组 | 用例 | 文件 |
|---|---|---|---|
| 1 | 开关/回退 | `local`：`/api/login` 正常、`/api/auth/oidc/*` 404；`oidc`：`/api/login` 403 且**不查锁、不计数** | `test_auth_login.py` |
| 2 | 配置 fail-closed | `oidc` + secret/redirect_uri/issuer 缺任一 → 启动中止（compose 层）+ `oidc_ready()` 为 False | `test_oidc_client.py` + compose 脚本断言 |
| 3 | 授权码流 | `state` 不匹配/重放/缺 `code` → 400；**token 请求体必须含 `redirect_uri`（mock 断言）**；错 secret → 502；`invalid_grant`（含授权码 60s 超时）→ **静默重走 start**（一次跳转），不作为错误页展示 | `test_oidc_client.py` |
| 4 | id_token 校验 | 签名/`iss`/`aud`/`exp`/`nonce` 各失败分支逐个拒绝；未知 `kid` → 刷新 JWKS 一次后通过 | `test_oidc_client.py` |
| 5 | 准入 | `systems` 不含 `cd` → 403 `errors.no_cd_access`；`status=0` → 403 `errors.user_disabled`；成功 → 302 `/` + `cd_token` cookie（HttpOnly/Lax/Secure 跟随 `COOKIE_SECURE`） | `test_oidc_client.py` |
| 6 | 缓存/降权 | 60s 内命中缓存（零 Glue 调用）；改 `role` → 窗口后 `/api/me` 反映新 role；`status=0` → 下一请求 401 且**会话行被删** | `test_oidc_login.py` + `test_session_auth.py` |
| 7 | Glue 不可达 | 软失败窗口内用 last-known-good 放行；超窗口 fail-closed；Glue 恢复后回到正常复核 | `test_oidc_login.py` |
| 8 | WS | OIDC 模式下 WebShell 握手走 `_lookup_session` + Glue 复核；停用 → 拒绝握手 | `test_cd_deploy_gate.py` 追加 |
| 9 | 回归 | `local` 模式全套现有测试不变（含 215 例基线）——证明**双轨不互伤** | 现有全部 |
| 10 | 联调验收 | Glue 侧用例 + CD 侧 A–G（§12） | 手工 |
| 11 | 角色 404（fail-closed） | 权限端点 404 即 登录/复核**拒绝**（403 / 401），**不**按空权限放行；同角色名在窗口内只查一次、只打一条限流 WARN（含角色名）；角色恢复后 ≤ 一个窗口自动恢复 | `test_oidc_login.py` |
| 12 | `*` 通配 | 返回 `["*"]`（`super_admin`）即 `require_perm` / `enforce_deploy_perm` 全放行；把 `SUPER_ADMIN_ROLE` 配成别的名字后仍放行 | `test_session_auth.py` / `test_cd_deploy_gate.py` 追加 |
| 13 | 失败呈现分流 | 整页导航（`Accept` 含 `text/html`）下 start/callback 的各失败分支 → 302 `/login?error=<key>`（`cd_not_enabled` / `cd_not_deployed` / `sso_denied` / `sso_state_invalid` / `sso_unavailable`）且清一次性 state cookie；**同一 URL 走 XHR（`*/*`）仍为 400/502/503 + JSON**（HTTP 语义不被重定向掩盖）；导航形态的成功路径仍为 302 `/` + `cd_token` | `test_oidc_login.py` |
| 14 | 访问日志脱敏 | `code` / `state` / `token` 的值被替换，`?retry=1` 等非敏感参数与状态码保留；非 SSO 路由零改写；WS dict 形态入参不被就地改写；`install_access_log_filter()` 幂等 | `test_access_log.py` |

> **Glue 侧已就位的护栏**（L1b 期间可直接作为取证，不依赖 CD 实现）：`tests/Unit/CdOidcLoginWiringTest.php`
> （源码级接线守卫 + 真 SQL 行为断言，覆盖 P0 四项）与 `tests/Unit/OidcServiceTest.php:188`
> （P2 tripwire，另含两条白名单交叉断言）。

## 12. 验收用例（A–H）

> **用例字母对照（防口径分叉）**：Glue 设计文档（`CD接入Glue-OIDC设计文档.md` v1.0.4）把「issuer 口径 / 联调第 0 项」
> 编号为 **E** —— 即本文的 **F**；其 **F + G** 合起来对应本文的 **G**（其 F = Glue 侧响应形状，G = CD 侧端到端行为）。
> **跨方沟通一律用场景名指代，不用字母。**

| # | 场景 | 步骤 | 期望 |
|---|---|---|---|
| **A** | 回退到本地模式 | `AUTH_MODE=local` + 重启 | 密码登录、`/api/me`、部署、审计、WebShell 全部与 v1.6.0 一致；`/api/auth/oidc/start` → 404；后端 313 例全绿 |
| **B** | 走通 SSO（默认档） | `AUTH_MODE=oidc` + `OIDC_ISSUER` / `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` 齐全，浏览器开 `/` 并点登录 | 302 Glue → 登录授权 → 回跳 `/api/auth/oidc/callback` → 302 `/` 并进入首页；`docker logs` 中无 secret / id_token / access_token |
| **C** | 越权与停用 | ① `systems='ci,monitor'` 账号登录；② `status=0` 账号登录；③ 登录后在 Glue 停用该账号，等 > 60s 再操作 | ① → `/login?error=errors.no_cd_access`；② → `errors.user_disabled`；③ 下一次请求 401、回登录页，且 `cd_sessions` 行已被删 |
| **D** | 降权即时生效 | 登录后在 Glue 改角色 / 去掉 `cd.deploy.approve`，等 > 60s | `/api/me` 反映新角色与新权限；对应操作按钮消失，且后端 `require_perm` 拒绝（403） |
| **E** | 回退演练 | 改回 `AUTH_MODE=local` + 重启 | 密码登录立即恢复；CD 表与数据无损（零 DDL，无需导入任何脚本）；`static/` 产物无需重建 |
| **H** | 必填项缺失提示 | 清空 `OIDC_ISSUER`（或 `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI`）后重启 | compose 预检中止并列出缺失项；裸机启动日志记 ERROR；登录页点 SSO → `errors.oidc_not_configured` 且列出缺失项名（XHR → 503 + `error_key`） |
| **F** | issuer 口径（联调第 0 项） | 切档前 `curl -s <issuer>/.well-known/openid-configuration`；再故意把 `OIDC_ISSUER` 写成内网/错误地址 | `.issuer` 与 `.jwks_uri` **均为对外地址**；写成错值时登录失败且日志**同时打印** discovery 的 issuer 与期望值（一眼定位，而不是误判成密钥问题）；改回对外地址即恢复 |
| **G** | 角色悬空 fail-closed | 在 Glue 手工 `DELETE` 某角色行（模拟角色悬空），等 > 60s 后再操作 | 受影响用户：登录被拒 / 已登录者下一次请求 401 并回登录页（`errors.identity_unavailable`）；日志出现含角色名的**限流** WARN；**不得**出现「已登录但权限为空」的静默降权；角色行恢复后 ≤ 60s 自动恢复 |

## 13. 上线分期与运维

| 期 | 内容 | 风险 |
|---|---|---|
| **L1a**（已落地） | 配置项 + `oidc.py` + `glue_identity.py` + `routers/oidc.py` + 前端登录页 + 测试；`AUTH_MODE=oidc` 为默认档 | 可回退（`AUTH_MODE=local` + 重启），后端 313 例全绿 |
| **L1b** | **本地已验**（2026-10-12，真 Glue `release/2.9.0` + 真 rbac，见文首取证）；**预发切档**：先做**联调第 0 项**（discovery 的 `issuer` / `jwks_uri` 均为对外地址）→ 推送并部署含 P0 的 Glue 构建 → 填根 `.env` 三项 → `app.env` 切 `oidc` → 走 A–G，观察 60s 复核与软失败窗口 | 仅预发，可随时切回 |
| **L1c** | 生产切档：`docker compose up -d --build` + 清 `cd_sessions` + 观察 30 分钟 | 有回退开关（§6.4），无 schema 变更 |
| **L2** | 清理与增强（§14） | 单独版本 |

> **提交注意（L1a）**：**7 个**新文件当前仍为 untracked —— `backend/oidc.py`、`backend/services/glue_identity.py`、`backend/routers/oidc.py`、
> `backend/access_log.py`（访问日志脱敏过滤器）、`backend/tests/test_oidc_login.py`、`backend/tests/test_oidc_client.py`、`backend/tests/test_access_log.py`；
> 且本仓库**跟踪** `static/` 构建产物（旧 `static/assets/*.js` 已被新构建替换、`static/index.html` 同步更新）即 提交时 `git add -A`
> 必须同时覆盖这两处，否则部署后仍是旧登录页（无 SSO 按钮）且 OIDC 路由缺失；更糟的是新 `static/index.html` 指向新 bundle，
> 若新 `static/assets/*` 未入库会直接白屏。`config/app.env` 是**被跟踪**文件，提交时必须保持 `AUTH_MODE=local`（改它 = 所有 `env_file` 部署默认切档）。

**观测点**：启动日志的 `AUTH_MODE` 行；`GET /api/info` 的 `auth_mode`；复核失败/软失败日志行；401/403 计数（登录页 `error` query 参数分布）。

**本次要同步的文档**：本文档（新增）、`INDEX.md`（登记本文档）、`ARCHITECTURE(_ZH).md`（新增「认证与身份源」小节）、
`ADMIN_MANUAL(_ZH).md`（新增 env 项、切换/回退步骤、fail-closed 说明）、`FAQ(_ZH).md`（SSO 登录失败与 `?error=` 排查）、
`USER_MANUAL(_ZH).md`（登录方式说明）、`CHANGELOG(_ZH).md`（随版本条目）、`README(_ZH-CN).md` 文档清单（可选）。

## 14. L2 待办（不入 v1 范围）

| 项 | 说明 |
|---|---|
| 后台身份重建路径 | `backend/services/approval_service.py::load_user_context`（定时执行/崩溃恢复，无 HTTP 上下文）在 OIDC 模式下需改走 `glue_identity` 的服务 token + 角色权限缓存，否则该路径仍会读共享表 |
| id_token `systems` claim | Glue 侧 P2（§9），省一次 rbac 调用 |
| `refresh_token` | 若要 access_token 长期代表用户（例如未来 CD 直接调 Glue 的用户级 API） |
| 删除 local 分支 | 所有环境切完并稳定一个版本后，移除共享表读取代码与 `test_auth_login.py` 的密码用例 |
| `login_guard` / 共享 `cache` 表登录锁 | OIDC 模式下自然退场，可随 local 分支一起删 |
| 账号自助管理 | 现状 CD 不做账号管理（`ADMIN_MANUAL_ZH.md:464/476`），未来继续由 Glue 承担，不做反向代理 |

## 15. 环境变量一览

| 变量 | 默认 | 位置 | 说明 |
|---|---|---|---|
| `AUTH_MODE` | `local` | `config/app.env` | `local` \| `oidc`；唯一总开关 |
| `OIDC_ISSUER` | 空 | `config/app.env` | Glue **对外**地址；须与 discovery 的 `issuer` 完全一致 |
| `OIDC_REVALIDATE_SECONDS` | `60` | `config/app.env` | 惰性复核窗口（放权/停用生效上限） |
| `OIDC_STALE_GRACE_SECONDS` | `600` | `config/app.env` | Glue 不可达时的软失败窗口；`0` = 立即 fail-closed |
| `OIDC_ID_TOKEN_MAX_SKEW` | `60` | `config/app.env` | `iat` / `exp` 时钟偏移容忍（秒） |
| `CD_OIDC_SECRET` | 空 | 根 `.env`（部署层） | `oauth_clients['cd']['secret']`；`oidc` 模式下必填 |
| `CD_OIDC_REDIRECT_URI` | 空 | 根 `.env`（部署层） | 与 Glue 登记值**逐字符一致**；`oidc` 模式下必填 |
| `CD_OIDC_CLIENT_ID` | `cd` | 代码常量 | 不暴露为 env（避免误改成别的客户端） |

> `oidc` 模式下 `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` / `OIDC_ISSUER` 缺任一 即 容器启动中止（§6.3）。

