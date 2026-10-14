# Changelog

## v1.6.0 (2026-10-14) — Local login → OIDC single sign-on, contract read over HTTP

### Upgrade Notes: Breaking changes

- **Local login → OIDC single sign-on**: local password login is removed; the only way in is now Devops-Glue OIDC single sign-on (`/api/auth/oidc/start` → `/api/auth/oidc/callback`). Before upgrading, configure `OIDC_ISSUER` / `CD_OIDC_SECRET` / `CD_OIDC_REDIRECT_URI` in the root `.env`.
- **Enablement comes from Devops-Glue's integration-service contract**: turn Devops-CD on first under "API Integration Services → Enable" in the Devops-Glue console, otherwise CD returns 503 `errors.cd_not_enabled`.
- With `provision='auto'` (CI's `DB_AUTO_MIGRATE` on) CD creates its tables automatically; with `provision='manual'` an administrator imports `database/init_mysql.sql` (idempotent; re-run after upgrades).
- Disabling the integration service only freezes admission — CD tables and data stay.

### Added

- **Contract read over HTTP**: `Database.cd_integration_state()` has a single channel — Glue's contract endpoint `GET /api/integration/services/cd` (Glue ≥ 2.9.0). When it cannot be reached (unconfigured / 404 / 405 / 401 / 403 / 5xx / timeout / response without `enabled`), CD fails closed with 503 `errors.ci_contract_unreachable` — it never reports "not enabled" and never reads the shared table. The cache window is set by `CI_CONTRACT_TTL` (30s by default, `0` = ask every request).
- **Tags in the build-page ref picker**: new `GET /api/ci/projects/{pid}/refs` proxies Glue's `/api/git/{path}/refs` (returns `{"branches": [...], "tags": [...]}`), and the frontend renders them as "Branches" / "Tags" `<optgroup>`s. On an older Glue (404/405) it falls back to `/branches` with a 5-minute negative cache; genuine faults still propagate.
- **CI→CD deploy-log contract endpoint `GET /api/integration/deploy-logs`**: CI's DeployLogRepository now pulls over HTTP (inbound static token `CD_API_TOKEN`, Bearer, constant-time comparison) instead of reading the shared `v_glue_deploy_logs` view; 10 columns + `project` (LIKE)/`status`/`deploy_type`/`date_from`/`date_to` filters + `page`/`per_page` (≤100) pagination, availability via status codes. The shared `v_glue_deploy_logs` view is removed.
- **Automatic table creation under `provision='auto'`** (MySQL and SQLite alike): on first connect it creates every `CD_REQUIRED_TABLES` table (14 today), adds missing columns and indexes, and self-checks the inventory.

### Changed

- **The gate criterion is "contract state × inventory completeness"**: not enabled → 503 `errors.cd_not_enabled`; missing table → 503 `errors.cd_not_deployed` (the hint follows `provision`: auto restarts to self-heal / manual imports the script).
- **Zero DDL in `manual` mode**: schema upgrades belong to the DBA's DDL script.
- **Not-enabled notice is one sentence and follows the UI language**: `error_key` → frontend i18n.
- **The monitoring pages no longer wait for every host**: the server list returns from the database immediately and the frontend fills each row via concurrent `GET /api/monitor/servers/{id}/probe` calls; one unreachable host only affects its own row.
- **Outbound address family `HTTP_IP_FAMILY`** (ipv4 / ipv6 / auto, default ipv4): `backend/net.py` reorders `socket.getaddrinfo` to improve retry when the server does not listen on IPv6.
- **CI-table index ownership clarified**: CD no longer creates indexes on the shared `ci_pipeline_artifacts` / `ci_job_git_map` tables; those indexes are owned by Glue's schema script.
- **Frontend build warnings eliminated**: xterm stylesheet path `/static/xterm/` → `/xterm/`; LoginView imported statically.

### Fixed

- **WebShell auth now matches REST (privilege-escalation fix)**: added `_check_cd_access` + `_check_disabled`; the permission branch is now fail-closed, matching `require_perm`.

## v1.5.7 (2026-10-08) — Security hardening, observability & CI tooling

### Upgrade Notes
- Upgrading from v1.5.7 or earlier: **re-run `database/init_mysql.sql`** (idempotent) to pick up new tables/columns. A legacy database that only received part of the tables (or only holds `cd_sessions` created by the old self-healing logic) is fixed the same way — re-run the script and it creates whatever is missing; the table names in the 503 error are exactly the to-do list.

### Security
- **Opaque session tokens** — login tokens are now `secrets.token_urlsafe` opaque strings instead of `base64(username:hash:expires)`; a new `cd_sessions` table stores only the SHA-256 digest (a DB leak no longer exposes usable tokens), and `POST /api/logout` deletes the row for immediate revocation (the old stateless token could not be invalidated). The WebShell WS handshake verifies against the session table too.
- **Security headers & HttpOnly cookie** — the app now emits `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / CSP, and login sets an `HttpOnly` + `SameSite=Lax` cookie (`Secure` toggled via `COOKIE_SECURE`) so JS cannot read the token; auth reads the cookie first and falls back to the Bearer header. The frontend drops the `?token=` URL / `sessionStorage` token and keeps only a non-sensitive `cd_authed` flag.
- **Derived PBKDF2 salt** — `_derive_key` now derives the salt from `SECRET_KEY` (`_derive_salt` = SHA-256 of `cd-service-pbkdf2-salt-v2:` + key) instead of the fixed `cd-service-v1-salt` shared by every deployment (rainbow-table risk); unlike the short-lived v1.5.7 `.cd_secret_key.salt` file it needs no filesystem write, so it works in the `read_only` container and stays stable across restarts. Pre-upgrade `enc:` values still decrypt via `_legacy_fernet` (fixed salt) and, while a v1.5.7 salt file is still on disk, via read-only salt-file fallbacks (`_salt_file_fernets`, looking only in the CD project root next to the key file — the `DB_PATH` directory is never read or written). Decrypt failures are now graded (invalid base64 = ERROR, all keys failed = WARN).
- **Password hash auto-detection** — login verifies bcrypt and argon2id via `verify_password()` (auto-detect by `$argon2` prefix), so CI switching `PASSWORD_HASH_ALGO` to argon2id no longer crashes CD login (`ValueError: Invalid salt` → 500); any verification failure returns `False` without raising.

### New Features
- **Prometheus `/metrics`** — a no-auth endpoint exposing uptime / version / per-status deploy counts / running count in Prometheus text format.
- **Idempotent command retry** — `retry_idempotent` retries safely-rerunnable commands (`docker compose up -d`, `kubectl apply`) a bounded number of times to absorb transient SSH/network blips; configurable via `deploy_retry_attempts` / `deploy_retry_delay` (`=1` disables).
- **Container resource limits** — `docker-compose.yml` gains `deploy.resources.limits` / `.reservations` (a service-level top-level `resources:` key is silently dropped by Compose, i.e. the limits would never apply; override via `CD_MEM_LIMIT` / `CD_CPU_LIMIT` / `CD_MEM_RESERVE` / `CD_CPU_RESERVE`).

### Changed
- **Zero DDL in MySQL mode (boundary fix)** — removed the `CREATE TABLE IF NOT EXISTS cd_sessions` self-healing statement. Every CD table is created by importing `database/init_mysql.sql` by hand, and **that import is the "CD enabled" gate**: until it happens the app creates no tables and no indexes, leaving zero DDL traces in the shared database. The previous "half self-heal" was both out of bounds and incoherent — it secretly created 1 of 14 CD tables. Only idempotent ADD COLUMN migrations for **already existing** tables remain (a missing table simply skips them, zero side effects).
- **All tables required, otherwise every operation is blocked** — new `Database.cd_missing_tables()` / `cd_deployed()`: the shared database is checked table by table against `CD_REQUIRED_TABLES` (14 tables today, cross-checked in both directions against `database/init_mysql.sql` and the SQLite auto-create list) — **one missing table means "not deployed"**. The `auth.cd_deploy_gate()` gate covers login (ahead of the rate-limit counter), `authenticate()`, every `verify_token` / `get_current_user` endpoint, the public webhook receiver and the WebShell WebSocket handshake (an independent auth path, mapped to a 503 so the socket closes cleanly instead of raising during the handshake) — i.e. **"even if login were possible, no operation is allowed"**: letting a half-imported schema through only yields 500s everywhere and can corrupt data, so everything returns 503 `errors.cd_not_deployed` (`error_params.tables` names the missing tables and the frontend interpolates them, so the operator knows exactly what to create). The gate reports, it never repairs. The unauthenticated `/health`, `/api/info` and `/metrics` stay available for troubleshooting. This is a **deployment state**, not a credential failure, so it is **not counted** in the login-failure lock shared with Devops-Glue's `cache` table (a disabled deployment must not pollute — let alone lock out — an administrator account).
- **The inventory is the single source of truth** — "is CD deployed?" is decided solely by the `CD_REQUIRED_TABLES` list (independent of how many tables it holds): adding or removing a CD table touches only three places (the list + `database/init_mysql.sql` + `_ensure_cd_tables`), while the gate logic, error messages, startup warning and counts all follow automatically — no hard-coded numbers, and no gate change when a table is dropped.
- **SQLite: inventory self-check + honest, driver-specific guidance** — the CD tables are auto-created with the SQLite file, and `_ensure_cd_tables()` now ends with an **inventory self-check**: if the list gained a table but the DDL was not updated, the very first connection raises a `RuntimeError` naming the missing table (better a failed start than a half-created schema that only shows up as gate 503s in production). The gate message no longer hard-codes "import init_mysql.sql" but uses a **neutral description plus a driver-specific action** (new `cd_deploy_hint()` and a read-only `Database.driver`): SQLite is told to restart (auto-recreate heals it), only MySQL is pointed at the import script — nobody gets sent to the wrong fix. The zh/en frontend texts for `errors.cd_not_deployed` now spell out both modes.
- **Loud startup notice** — the service probes the deployment state at startup and logs an ERROR naming every missing table; startup is deliberately not blocked so `/health`, `/api/info` and `/metrics` remain usable for liveness checks and troubleshooting.
- **Frontend i18n** — added `errors.cd_not_deployed` (zh / en, with `{tables}` interpolation for the missing tables).
- **Database unavailable ≠ not deployed: a bare 500, now a proper 503** — failing to create tables (SQLite file read-only / disk full / locked), failing to open the DB file, failing to get a MySQL connection, or failing the startup shared-DB probe used to surface as a **bare 500** (whose meaning is "programming defect", sending operators to read code instead of their environment). All of them now raise `DatabaseUnavailableError` → **503 `errors.db_unavailable`** (with `error_params.driver` / `.reason` carrying the driver and the underlying cause; zh/en frontend texts for `errors.db_unavailable` added), kept strictly apart from `errors.cd_not_deployed` (incomplete inventory): the former means "check the connection, permissions and disk", the latter "complete the tables" — different error keys, different actions, so nobody is pointed the wrong way. The inventory self-check's `RuntimeError` (list vs DDL drift = a code defect) still propagates untouched and is never disguised as a 503. A resource leak was fixed along the way: when table creation/migration raised, the connection was never closed (on Windows an unclosed handle locks the DB file, so it cannot be deleted and the next connection gets worse); every failure path now calls `close()`.
- **Container hardening** — `cap_drop: ALL`, `no-new-privileges`, `read_only` root FS + `/tmp` tmpfs, and the port defaults to `127.0.0.1` (reverse-proxy deployment; `CD_BIND_IP=0.0.0.0` for direct exposure). `env-check` now requires `SECRET_KEY` for **every** DB driver (the container is unconditionally read-only, so an auto-generated `.cd_secret_key` can never be written) and aborts on the public sample values `devops_cd_2026` / `change_me_to_secret`.
- **`ALLOW_EMPTY_SYSTEMS` config** — empty `admin_users.systems` defaults to allow (legacy data); strict environments can set `false` for deny-by-default.
- **Health check asserts DB** — the compose healthcheck now hits `/api/info` and requires `status=running` + `db_connected=true`, so a dead DB no longer reports a false green.

### Fixed
- **Read-only startup no longer dies with a raw `OSError`** — when `SECRET_KEY` is unset and the key file is unwritable, `_resolve_secret()` now raises a bilingual `RuntimeError` naming the exact fix (`openssl rand -base64 32`) instead of an import-time stack trace; `env-check` aborts even earlier, for any DB driver.
- **No credential loss from the abandoned v1.5.7 salt file** — data encrypted by v1.5.7's `.cd_secret_key.salt` random salt is still decryptable: when the file is still present it is read (never written) as one extra fallback key; a missing/unparsable file is skipped silently.

### CI / Tooling
- **Full test suite in CI** — pytest full run + `--cov` coverage with `.coveragerc`; pinned `ruff==0.16.5` / `pip-audit==2.10.1` / `pytest==9.1.1` / `pytest-cov==7.1.0`.
- **Type check & lint in CI** — mypy (`mypy==1.13.0`) joins the code-quality job; ESLint 9 flat config + `eslint-plugin-vue` v10 with a new `npm run lint` step.
- **Supply-chain hardening** — all 5 workflows' `uses:` pinned from `@vX` tags to commit SHAs; `setup-python` bumped v5→v6 to drop the Node 20 deprecation warning.

### Tests
- New `backend/tests/test_cd_deploy_gate.py`: asserts the MySQL migration path issues only `ALTER TABLE` and never `CREATE TABLE` / `CREATE INDEX` (`cd_sessions` is no longer created); verifies the gate list stays **identical in both directions** with `database/init_mysql.sql` and the SQLite auto-create DDL (list drift turns the test red); covers "one missing table => not deployed, naming that table", 503 from `authenticate` / `verify_token` / `get_current_user` / the public webhook receiver, a still-401 response when no token is sent, and connection failures not being mistaken for "not deployed".
- `backend/tests/test_auth_login.py` gains deployment-gate cases (incomplete: no lock lookup / no failure count / no password check, plus the missing-table list handed to the frontend; complete: login proceeds) and fixes the call signature where `db` was passed as `response`.
- `test_cd_deploy_gate.py` gains an "inventory is the single source of truth" group: SQLite auto-create must cover every inventory entry (set comparison, no hard-coded count), a list entry without SQLite DDL raises `RuntimeError` on first connection, a runtime `DROP TABLE` blocks immediately and a **restart heals it**, and the remediation hint differs per driver (MySQL → import script, SQLite → restart, never the wrong instruction); the WebShell 503 case now also asserts the SQLite branch no longer mentions `init_mysql.sql`.
- **No-shell-wrapper & quoting regression** — asserts SSH/Ansible/Compose build commands without explicit shell wrappers and that every `image/project/tag/path/inventory` placeholder is `shlex.quote`-d (a project containing `; rm -rf …` is neutralized).
- **`v_glue_deploy_logs` contract test** — locks the Glue-facing view columns and asserts SQLite and `init_mysql.sql` expose identical columns.

## v1.5.6 (2026-10-05) — Read-only deploy-log view for Glue

### New Features
- **`v_glue_deploy_logs` contract view** — a read-only view over `cd_deploy_logs` exposing only the audit columns (`id, project, tag, image, deploy_type, target, status, triggered_by, deploy_note, created_at`) for Devops-Glue to render a "deploy logs" audit list. Large columns (`output`, `stage_times`) and CD-internal/operational columns (`runner`, `heartbeat_at`, `duration_ms`, `lock_key`, `params_json`, `rollback_type`, `artifact_*`) are intentionally excluded, so Glue depends on a single stable, minimal contract. MySQL uses `CREATE OR REPLACE VIEW`; SQLite self-heals with `DROP VIEW IF EXISTS` + `CREATE VIEW` on startup.

## v1.5.5 (2026-10-03) — Deploy-core hardening

### Fixed
- **Compose deploy permission mapping** — `_DEPLOY_PERM_MAP` gains the missing `compose` key so `resolve_deploy_perm("compose")` maps to `cd.deploy.docker`; previously it fell through to `cd.deploy.single`, mis-scoping Docker-deploy permissions.
- **Kubectl namespace when `k8s_ns` is empty** — the effective namespace is now resolved from the manifest (`effective_ns`), so `kubectl apply` without `-n` no longer verifies/restarts against the wrong namespace (false "deploy failed").
- **`/api/stop` rejects `k8s/*` deploy types** — the endpoint now guards K8s sub-modes like `/api/deploy` / `/api/deploy-stream`, instead of a `K8sSubDeployer.stop()` signature mismatch (TypeError → 500).
- **Empty `server_ids` filters out K8s clusters** — `_get_targets` no longer treats `k8s`/`argocd`/`fluxcd` servers as SSH/Compose targets when no server is selected.
- **First-deploy YAML metadata parsing** — `_get_yaml_metadata` reads and parses the remote YAML locally instead of `kubectl get -f` (which 404s before the resource exists), fixing first-deploy failures.
- **Placeholder / `ns_flag` quoting** — compose `commands` mode and helm `ns_flag` now `shlex.quote` their interpolations (matching `ssh.py` / `k8s_kubectl.py`).
- **SSH command timeouts** — `_exec_on` / `_ssh_cmd` gained a channel read timeout (default 130s) so a hung remote no longer blocks the deploy thread forever.
- **Busy / finish-record consistency** — the SSH path now raises `ValidationError(error_key="errors.deploy_busy")` like the K8s path; `finish_deploy_record`'s no-`deploy_id` fallback is scoped by `project` instead of updating every `running` record.

## v1.5.4 (2026-09-23) — Config layering: deployment config vs application parameters

### Changed
- **Two-layer configuration**: the repo-root `.env` now holds deployment values only (database, SECRET_KEY, Harbor / CI credentials, listen address, TZ, proxy hops); application parameters (timeouts / cache TTLs / intervals / truncation / namespaces / roles / monitoring switch) moved to `config/app.env`, shipped with the repo and the image.
- **Precedence**: process environment > root `.env` > `config/app.env` > code defaults. Override any application parameter by adding it to the root `.env` (compose injects it in Docker) or to the compose `environment:` block — **no image rebuild needed**.
- **Joint deployment requires no change to Devops-Glue's `docker-compose.yml`**: the commented-out `devops-cd` block mounts no volumes, so CD takes application parameters from `config/app.env` inside the image while deployment values come from that block's `env_file: .env` (`DB_HOST=mysql` resolves via the compose service name).
- Standalone Docker: `docker compose up -d --build` (application parameters come from `config/app.env` inside the image); bare metal: just run `python main.py` from the repo root — `backend/config.py` reads the root `.env` itself, so no environment pre-export and no launcher script are involved.
- Docs: admin manual §2 split into "2.1 Deployment config / 2.2 Application parameters" (both languages); `.env.example`, FAQ, README, CONTRIBUTING and UI hints updated; `config/app.env` ships with full comments.

### Fixed
- **Registry sync masked Harbor outages**: `RegistryService.sync_repo()` caught `HarborUnavailableError` inside its generic `except Exception` and returned 0, so `POST /api/registry/sync` answered `{ok: true, total: 0}` and the UI reported "sync complete" while Harbor was unreachable (the background thread logged "sync complete" too). Connection-level failures now propagate: the API returns `{ok: false, error_key: "errors.harbor_unavailable"}` and the UI shows "connection unreachable" (red, kept until a sync succeeds); per-repo non-connection errors stay tolerant. Regression tests in `backend/tests/test_registry_sync.py`.
- **Startup crash**: pydantic-settings defaults to `extra="forbid"`, so any non-field key in `.env` (`TZ`, or the Glue-side `DB_ROOT_PASS` injected during joint deployment) raised `extra_forbidden`; now explicitly `extra="ignore"`.

## v1.5.3 (2026-09-14) — Approval requester-execution workflow, scheduled release & approval fixes

### New Features
- **Approval workflow (requester manual execution)**: `pending → approved → requester execute` — after approval only the requester can claim and execute the ticket (four-eyes retained), via sync `POST /api/approvals/{id}/execute` or SSE `/execute-stream`. The background auto-execution poller was removed; restart recovery now converges `deploying` tickets by the actual deploy-record state to avoid duplicate deploys.
- **Scheduled release (定时发布)**: the requester can set an optional `scheduled_at` at deploy-submit time; a background daemon (30s) executes due `approved` tickets **as the requester**. Works with or without an approval rule (no-rule + scheduled → an auto-approved ticket executed at the due time). Invalid formats are rejected with 400; the UI adds an optional datetime picker and a ⏰ badge on the ticket.
- **Account management removed from CD**: `/api/users` write / password-change endpoints removed (managed in CI/Glue); frontend `UsersView` / `UserCreateView` deleted.
- **Deploy records & rollback**: `cd_deploy_logs.approval_id` column (with historical backfill) links tickets to deploy records; rollback supports a custom note; ArgoCD rollback summary is routed to the note column.

### Fixes
- Sync execute failure no longer wedges the approval ticket (releases the project concurrency lock).
- Cancel keeps the original approver and fires the cancel notification.
- `resolve_target_envs` is fail-closed (no silent skip of approval).
- Startup recovery no longer re-opens finished deploys.
- `/api/approvals` rule matching de-N+1'd.
- Deploy-log approval badge is now status-driven (was approver-based).
- Legacy `cd_admin` default role removed (deny-by-default; new rules default to an empty `approver_role`).
- `database/init_mysql.sql` synced: `cd_approvals.scheduled_at`, `cd_deploy_logs.approval_id`, `approver_role DEFAULT ''`.

### Tests
- Approval suite expanded (scheduled release, execution fallback, rule matching/validation, cancel notification); 114 tests total.

---

## v1.5.2 (2026-09-13) — Security hardening release (paired with Devops-Glue v2.8.1)

### Security
- **Image tag allowlist on every deploy entrypoint**: tags must now match `^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$` (OCI-compatible); invalid tags are rejected with 400 before any remote command runs. Covers Compose, SSH, kubectl/Helm/FluxCD K8s deploys (both `/deploy-k8s` and streaming variants), and manual-tag rollback. Note: historically malformed tags (spaces, semicolons, …) will now be refused.
- **Command injection eliminated in the Compose deployer**: remote `.env` updates no longer use `sed`/`echo` interpolation — the file is rewritten via `touch` + `grep -v` of old `IMAGE`/`TAG` lines + `printf` with `shlex.quote`; all `docker image inspect/manifest/pull`, `compose up/ps` and `grep` arguments are quoted and `grep` gets `--`. FluxCD invocations (namespace discovery, `flux suspend`, jsonpath, grep patterns) are now fully quoted as well.
- **Shared login-failure lockout**: login is now guarded by the same lock as Devops-Glue CI — 5 failures / 15 minutes keyed by `login_fail_` + md5(ip:lower(username)) in the shared `cache` table (HTTP 429, `errors.login_locked`); lockout is checked before password verification, disabled/no-CD-access failures also count, and success clears the counter.
- **Spoofable X-Forwarded-For removed**: new `TRUSTED_PROXY_HOPS` setting (default `0` = ignore XFF and use the TCP peer); with hops > 0 and a loopback/private/link-local peer, the real IP is taken from the rightmost XFF entry by hop count. This prevents forged XFF from bypassing the login lockout or polluting audit IPs.
- **Four-eyes approval**: a requester can no longer approve or reject their own approval ticket — `super_admin` included (403, `errors.self_approval_forbidden`); another approver must handle it. Post-approval execution still runs as the requester.
- **Weak public SECRET_KEY values rejected**: `devops_cd_2026` and `change_me_to_secret` are now treated as unconfigured and fall back to a generated key; `.env.example` ships no default secret. Deployments that used the sample key must re-enter encrypted SSH passwords/private keys after upgrading.

### Changes
- Webhook event persistence no longer uses MySQL `LAST_INSERT_ID()`; it reads the insert cursor's `lastrowid`, fixing webhook inserts on SQLite.
- Empty/blank login usernames are rejected as invalid credentials without lock lookup or failure counting, preventing a shared `md5(ip+":")` bucket.
- New i18n keys `errors.login_locked` and `errors.self_approval_forbidden` added to both frontend locales; backend error keys are now fully covered by the language packs.
- New `env-check` one-shot preflight container: with `DB_DRIVER=mysql`, missing `DB_HOST`/`DB_USER`/`DB_PASS` aborts startup with bilingual (Chinese/English) guidance before uvicorn starts; an unset `SECRET_KEY` prints a bilingual advisory (auto-generated fallback still applies).

---

## v1.5.1 (2026-09-10) — Align shared-DB check with Devops-Glue v2.8.0

### Changes
- **Startup shared-DB check**: now verifies the `ci_pipeline_artifacts` table (Devops-Glue's canonical pipeline-artifact table) instead of the removed `ci_pipeline_tags`. Requires Devops-Glue v2.8.0+.
- **Docs corrected**: CI data (mappings / tags / pipelines / builds) is fetched exclusively via the CI HTTP API; CD never reads CI tables directly. Architecture, admin/user manuals, and FAQ updated accordingly.

### Database
- `database/init_mysql.sql`: CI index bootstrap now targets `ci_pipeline_artifacts` (`project_key`, `created_at`).

### Fixes
- **Partial deploy status preserved**: `partial` remains a valid terminal state instead of being folded into `failed`; aggregate counts and frontend badge mapping keep it visible as a distinct outcome.
- **Kubernetes namespace semantics preserved**: blank `k8s_ns` still means “do not pass `-n`”, matching the existing design and YAML-defined namespace behavior; explicit namespaces are still validated and passed with the cluster command.
- **Legacy DB compatibility**: added the missing MySQL migration for registry artifact fields so older installs continue to work without manual schema patching.
- **ArgoCD rollback logic tightened**: the wait loop correctly checks whether the revision still matches the previous revision before continuing, avoiding false failures and `NameError`-style edge cases during rollback.
- **Frontend output compatibility**: the log page keeps the blue `partial` badge and the CSS line-clamp styling remains compatible with broader browser prefixes.

---

## v1.5.0 (2026-08-31) — Deployment approval workflow & one-click rollback

### New Features
- **Deployment Approval (审批)**: A rule-based approval gate now runs before deployments. Admins configure `cd_approval_rules` per project (or `*` as the global default): enable/disable, target environments (`require_envs`, matched against server tags), approvers (explicit usernames or `approver_role`, default `cd_admin`), and a notification bot. When a deployment matches a rule, a `cd_approvals` ticket (`pending`) is created and approvers are notified; the deployment runs only after approval.
- **Approval state machine**: `pending → approved → deploying → deployed / failed`, plus `rejected` and `cancelled`. Approval is an atomic, persisted transition; execution runs in a background thread.
- **Persistent approval queue**: Approved tickets are drained by an idempotent poller; on process restart `recover_on_startup` clears stale running deploy locks and re-queues `deploying` tickets.
- **Approval management UI**: A new "Approvals" view provides an approval list (approve / reject / cancel) and an approval-rules management tab.
- **One-click Rollback (回滚)**: Added `POST /api/deploy/rollback` and `POST /api/deploy/rollback-stream` (SSE). Two strategies per deployment mode:
  - **Native rollback** (kubectl / helm / argocd): calls the cluster-native undo command (`kubectl rollout undo`, `helm rollback --wait`, ArgoCD `rollback` API) using the latest successful record as context.
  - **Replay rollback** (fluxcd / ssh / compose): re-runs the previous successful deployment (a different tag) by reusing its stored parameter snapshot (`params_json`).
  - Legacy records (pre-v1.5.0, no snapshot) are skipped automatically.
- **Streaming rollback output**: Rollback now streams live logs over SSE, with fully internationalized (zh/en) rollback messages for ArgoCD, kubectl, helm, and the stop fallback.

### Changes
- Rollback goes through the same approval gate, controlled by `require_rollback_approval` (default on).
- New permission `cd.deploy.approve` controls approval actions and rule management.

### New Database Tables
- `cd_approval_rules` — per-project (or `*` global) approval rules.
- `cd_approvals` — approval tickets with status, requester, approver, and deploy params snapshot.

### New Files
- `backend/routers/approvals.py`, `backend/services/approval_service.py`, `backend/services/rollback_service.py`, `backend/services/deploy_executor.py`, `backend/services/k8s_deploy_service.py`
- `frontend/src/views/ApprovalsView.vue`

---

## v1.4.0 (2026-08-19) — Custom_Push project read-only display

### New Features
- **Custom_Push project adaptation** — Build history view now detects `custom_push` projects and renders them read-only: the "Trigger Build" button is hidden and triggering is guarded at the action layer, since custom_push results are pushed by user CI (Devops-Glue only receives the report).
- **Finish time column** — Build records now show finish time (`updated_at`) instead of create time, aligned with the push-based report lifecycle.

### Changes
- **Locale** — "Time" → "Finish Time" (EN), "时间" → "完成时间" (ZH).

---

## v1.3.1 (2026-08-17) — Deploy cancel mechanism, concurrency lock, structured timing, UI refinements

### New Features
- **Deploy Cancel Mechanism**: Added `POST /api/deploy/cancel` endpoint, allowing users to manually cancel an in-progress deployment. Uses `threading.Event` as an in-memory signal; low-level loops (ssh_exec_stream, k8s sub-deployers) poll the signal to interrupt long-running commands promptly. `DeployCancelled` inherits `BaseException` to pierce through `except Exception` fallbacks and bubble up to the orchestration layer for unified `terminated` status persistence.
- **Deployment Concurrency Lock**: Only one `running` record allowed per project at a time. Pre-deploy check via `find_running_deploy`; the route layer also rejects `deploy_type='k8s/*'` from the SSH/Compose path (incompatible API signatures) and the service layer defensively raises `ValueError` before inserting a running record.
- **Structured Timing & Deploy Note**: Deploy records now persist `duration_ms` (total elapsed), `stage_times` (per-server per-stage breakdown), and `deploy_note` (user-entered free-text audit note, displayed as a column in the deploy log table).
- **Custom Confirm Modal**: Replaced native browser `confirm()` with a promise-based `useConfirm` composable + global `ConfirmModal` component across deploy stop / cancel / log-view cancel actions.

### Changes
- **SSH Deploy Page**: Removed the Stop button (custom SSH commands have no meaningful stop semantics); Cancel button is now always visible (disabled when not deploying) to avoid it disappearing mid-deploy.
- **K8s Deploy Page**: Removed the "View Resource Usage" button (jumped to external monitor, low value); Cancel button now always visible.
- **Docker / K8s Stop**: Retained with custom confirm modal for secondary confirmation.

### Bug Fixes
- **K8sSubDeployer missing `validate`**: Calling the SSH/Compose endpoint with `deploy_type='k8s/kubectl'` raised `AttributeError` (K8s sub-deployer inheritance chain lacked `validate`). Added a dual-signature-compatible `validate` on `K8sSubDeployer`.
- **Running record orphan on exception**: `_deploy_k8s_core` and `DeployService.execute` only caught `DeployCancelled`, leaving the `running` record orphaned on other unexpected exceptions, dead-locking the concurrency lock. Added `except Exception` fallbacks to update the record to `failed`.
- **`deploy_type='k8s/*'` mixing into SSH/Compose path**: The deployer registry happily created a `KubectlDeployer` for the SSH/Compose path, but the `deploy()` signature is incompatible. Added route-layer + service-layer double guard rejecting `k8s/*` early (before inserting any running record).
- **`register()` not resetting Event state**: If a `deploy_id` was reused (rare), a previously-set cancel signal could leak into the new deployment. `register()` now always calls `ev.clear()`.

---

## v1.3.0 (2026-08-13) — CI connection switches to Devops-Glue dedicated API Token, bug fixes

- **Docs update**: CI_API_TOKEN config instructions, fixed ruff-detected L1/L2/L3 bugs

---

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