# Changelog

## v1.2.2 (2026-08-08) — Webhook Receiver Endpoint & CI Build Management & Type Safety Hardening & Deploy Error Passthrough

### New Features
- **Webhook Receiver Endpoint**: Added `/api/webhooks/receive/{token}` public endpoint for receiving external events such as CI build completion
  - Supports any POST push from Jenkins / GitLab CI / custom CI
  - Auto-persisted to `cd_webhook_events`, with paginated event history browsing
  - Optional auto-forward to DingTalk/WeCom/custom bots via linked Bot, with template placeholders
  - Supports manual event forwarding, enable/disable webhook, and event deletion
  - 32-char random token auth (URL path), no login required
- **CI Build Management**: Added "Build Management" menu, proxying CI service via HTTP API
  - Trigger builds, view build history, view build logs (Jenkins/GitLab CI dual engine)
  - `backend/services/ci_client.py`: JWT token cache + auto-refresh + retry
  - Backend routes: `/api/ci/*` (project list, build history, trigger build, build logs, build variables, branch list, health check)
- **Public Info Endpoint**: `/api/info` requires no auth, returns app name, version, DB type & connection status, uptime
- **Health Check Simplified**: `/health` changed to pure `{"status":"ok"}`, suitable for Docker/K8s liveness probes

### Changes
- **Deploy Error Passthrough**: `deploy` route layer catches `ValueError` (invalid Docker Compose path, missing SSH server, etc.), pushes specific error messages to frontend via SSE stream in real-time, no longer swallowing exceptions
- **Type Safety Hardening**: Full project Pyright type check passed, unified database interface (SQLite/MySQL placeholder auto-conversion), fixed a batch of `Optional` unchecked, `None` comparison type mismatch issues
- **Database Interface Unification**: Added `conn.execute()` internal wrapper, SQLite `?` placeholder auto-converts to `%s` under MySQL driver, business code doesn't need to care about driver differences
- **CD Table Index Completion**: `cd_servers.type`, `cd_deploy_logs.deploy_id`/composite, `cd_alert_rules.enabled`/`created_at`, `cd_custom_monitors.enabled`/`created_at`, `cd_webhooks.enabled`, `cd_webhook_events.webhook_id`/`received_at`
- **Error Handling Enhancement**: Backend exception system unified with `error_key`, frontend error pages display accordingly; `cd_webhook_*` exception series fully covered (not found/already exists/create failed/forward failed)
- **Frontend i18n Fix**: `lang` parameter passing, Shell "Connected" hardcoded text moved to translation, several UI translation omissions
- **Webhook Management UI**: Frontend added "Notification Access" menu (WebhookView), supports create/edit/delete/toggle, view events, paginated browsing, manual forwarding
- **Sidebar Navigation**: Added `ciBuild`, `webhook` menu items, unified under `cd.notification-manage` permission control
- **Test Environment Optimization**: Eliminated pytest deprecation warnings (Pydantic `class Config`→`model_config`, FastAPI `on_event`→`lifespan`, installed `httpx2`)
- **Deployment Documentation**: Admin manual (CN/EN) added CI API configuration, Webhook security policy, three deployment strategies detailed explanation
- **Copyright Fix**: Frontend footer & LICENSE copyright unified from `Blues.Inc` to `jeanslw`
- **README Update**: CN/EN README added "Related Projects" section, GitHub link fixed from `Devops_Glue` to `Devops-Glue`
- **Deploy Config Simplification**: `docker-compose.yml` removed standalone `networks` block, defaults to same-host combined deployment with CI, added `image: devops-cd:latest`

### New Database Tables

| Table | Description |
| :--- | :--- |
| cd_webhooks | Webhook receiver config (id/name/token/bot_id/enabled/created_at) |
| cd_webhook_events | Webhook event log (id/webhook_id/payload/received_at/forwarded/forwarded_at) |

### Design Notes
- **Webhook token auth**: Uses URL path `{token}` approach (not Header), convenient for Jenkins and other tools to POST directly without complex signatures. Token is 32-char random (`secrets.token_urlsafe(24)`), database unique index.
- **Auto-forward mechanism**: Receive event → persist to DB → if `bot_id>0` then auto-invoke bot webhook → mark `forwarded=1`. Failure does not block event persistence; admins can retry manually from UI.
- **Message placeholders**: Bot template supports `{project}{tag}{image}{status}{built_at}{target}{mode}`, defaults to field-name formatting when no template, falls back to raw JSON output when unparseable.
- **Data ownership**: CD only reads CI database tables (`ci_pipeline_tags`, `ci_job_git_map`), never writes; "Build Management" uses HTTP API, "Tag List/Deploy Flow" continues via direct DB reads — two layers do not interfere.
- **`/api/info`**: Fully public, no auth dependency, convenient for monitoring systems and external tools to query CD runtime status.
- **`error_key`**: Exceptions carry i18n keys, frontend can map to corresponding language error messages based on key.

---

## v1.2.1 (2026-07-29) — RBAC Permission Adaptation & Deploy Log Optimization

### Changes
- Adapted to Devops-Glue API RBAC permission system (`enforce_deploy_perm` deploy secondary auth)
- Deploy log optimization (deploy success/failure info deduplication, running version comparison display)
- Some variable configs migrated to `docker-compose.yml`
- Documentation updates

### New/Modified Files

| File | Change |
| :--- | :--- |
| backend/auth.py | Modified — `enforce_deploy_perm` service layer secondary permission check |
| backend/deployers/*.py | Modified — Deploy log format unified, running version comparison |
| docker-compose.yml | Modified — Config variable migration |
| docs/* | Modified — Documentation sync update |

---

## v1.2.0 (2026-07-28) — Landing Page & Login i18n & CD/CI Permission Isolation

### New Features
- **Landing Page**: Particle background + Hero title + 6 feature cards + footer, shown when not logged in
- **Multi-language Support**: Chinese/English toggle, Landing page and Login page both have independent language switch buttons
- **CD/CI Login Isolation**: `admin_users` table added `systems` field (comma-separated), CD side validates `systems` contains `"cd"` before allowing login
- **CD Permission Narrowing**: Cannot create/delete/modify admin role users, admin not shown in user list; new user form removed "Admin" option
- **Frontend Vue 3 Refactor**: Complete replacement of old jQuery frontend, Vue 3.5 + Vite 6 + Vue Router 4

### Fixes
- `locales/index.js` operator precedence bug (`||` vs `&&`)
- `vite.config.js` added `/static` proxy path
- `node_modules` cleanup added to `.gitignore`
- `main.py` removed deprecated `ensure_role_column` call

---

## v1.1.0 (2026-07-24) — Harbor Image Registry Integration & Config Enhancement

### New Features
- **Harbor Image Registry Integration**: Supports connecting to Harbor registry, browsing projects/images/tags, viewing vulnerability scan reports, one-click Tag selection for deployment
- **Configurable Sync Interval**: Frontend dropdown + API + DB persistence + background thread dynamic restart, no service restart needed
- **Harbor Unreachable Friendly Prompt**: Added `HarborUnavailableError` exception, route layer returns 503, avoids backend crash
- **Server Password/Certificate Encrypted Storage**: Fernet symmetric encryption, managed via `ENCRYPTION_KEY` env var
- **Server Tag Management**: Tag aggregation endpoint (`/api/tags`), supports filtering servers by tag
- **Legacy Data Migration Tool**: `migrate_encrypt.py` one-click encrypt existing plaintext fields

### Fixes
- Config endpoint (`/api/registry/config`) added exception handling, avoids returning 500 HTML when DB unavailable
- `cd_config` config table unified via `database/init_mysql.sql` creation, removed redundant auto-creation in code

---

## v1.0.0 (2026-07-23) — Base CD Service

### Core Features
- SSH / Docker Compose / Kubernetes three deployment modes
- Server management (password/certificate login)
- Deployment history records with pagination
- DingTalk/WeCom Webhook notifications
- Real-time log viewing
- Project management
- Dual-driver database support (SQLite / MySQL / MariaDB)