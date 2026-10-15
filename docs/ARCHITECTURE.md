# Devops-Glue — Architecture Overview

## Overall Data Flow

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
│  │ BuildProvider│  │ BuildProvider│   │ BuildProvider│    │ (custom_push) │  │   (pusher)
│  └──────┬───────┘  └──────┬───────┘   └──────┬───────┘    └───────┬───────┘  │
│         └─────────────────┼──────────────────┼────────── ─────────┼          │
│                                   ↓                                          │
│                    Build → Docker Image → Harbor Registry                    │
│                                   ↓                                          │
│                      scan-sync → ci_pipeline_artifacts                       │
└───────────────────────────────────┬─────┬────────────────────────────────────┘
                                    ↓     ↓
		┌───────────────────────────────────────────────────────────────┐
		│                     CD 层：devops-cd (Python)                 │
		│                                                               │
		│  ┌────────────────────────┐     ┌────────────────────────┐    │
		│  │Build manage(HTTP API)  │     │ Webhook Receive/Forward│    │
		│  │ · Trigger Build        │     │ · cd_webhooks(config)  │    │
		│  │ · Deployment Execution │←───→│ · BotNotifications     │    │
		│  │ · Deployment/Build Logs│     │                        │    │
		│  └──────────┬─────────────┘     └──────────┬─────────────┘    │
		│             │                          Auto Forward           │
		│                                                               │
		│  Select Project / Tag ──→ Run Deployment                      │
		│                              ↓                                │
		│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐        │
		│   │  SSH Scripts    │Docker Compose│  │  Kubernetes  │        │
		│   │  Ansible     │  │  SFTP + up   │  │ kubectl/Helm │        │
		│   │              │  │              │  │ ArgoCD/FluxCD│        │
		│   └──────────────┘  └──────────────┘  └──────────────┘        │
		│                           ↓                                   │
		│              cd_deploy_logs (Deployment Records)              │
		│                           ↓                                   │
		│          DingTalk / WeCom / Custom Webhook Notifications      │
		└───────────────────────────────────────────────────────────────┘
```
## Component Relationships

```
		┌─────────────────────────────────────────────┐
		│ Database (SQLite / MySQL / MariaDB)         │
		│                                             │
		│  ci_pipeline_artifacts ← CI write           │
		│  cd_servers            ←  CD maintain       │
		│  cd_deploy_logs        ← CD write           │
		│  cd_approvals(_rules)  ←  CD write          │
		│  cd_sessions           ← CD write / delete  │
		│  cd_bots               ← CD maintain        │
		└──────────────────────┬──────────────────────┘
							   │
					  ┌────────┴────────┐
					  ↓                 ↓
		     ┌────────────────┐    ┌──────────┐
		     │Devops Glue API │    │Devops CD │
		     │     :8080      │    │  :8081   │
		     └────────────────┘    └──────────┘
```

> **Data ownership**: CD maintains all of its own `cd_*` tables (webhooks, monitors, alerts, registry cache, config, sessions, approvals). CI-specific build data (pipelines, mappings, build records) is fetched from CI over its HTTP API — CD never reads any `ci_*` table directly.
>
> **Ref candidates on the build page** also come from a Glue HTTP endpoint: `GET /api/git/{path}/refs` returns branches + tags in one call (`/api/build/{path}/branches` only returns branches). CD proxies it as `GET /api/ci/projects/{pid}/refs` and the build page renders two `<optgroup>`s (a branch and a tag can share a name). On an older Glue without that route the client falls back to `/branches`, so tags come back empty.

**Database Selection**: CI and CD may share one database or each use its own (CD never reads `ci_*` tables; cross-service data travels over HTTP).
- **SQLite**: Zero-config, suitable for single-host dev/test. When sharing one database, container deployments must mount a shared volume so both processes access the same `.db` file.
- **MySQL 8.0+ / MariaDB 10.4+**: Recommended for production. Supports concurrent read/write.

## Deployment Mode Matrix

| Deploy Type | Mode | Implementation |
| :--- | :--- | :--- |
| SSH (single host) | Custom Command | Shell script with `{image}` `{tag}` `{project}` placeholders |
| SSH (single host) | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | Remote YAML | `cd {path} && docker compose up -d` |
| Docker Compose | Inline YAML | SFTP upload compose YAML → auto-create dir → startup |
| K8s kubectl | SSH apply | SSH to master → `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + version verification |
| K8s Argo CD | REST API | PATCH image → sync → poll until Healthy |
| K8s Flux CD | SSH kubectl | PATCH resource → wait for ready |

## Design Patterns

- **Strategy Pattern**: `BuildProviderInterface` (PHP CI) / `Deployer` (Python CD) abstract base class + Registry
- **Factory Pattern**: `GitProviderFactory` auto-matches Git platform adapter by URL
- **Dual-driver Database**: SQLite / MySQL / MariaDB unified interface, one codebase for three modes; the `cd_*` tables may share a database or live in a dedicated one

## CD Table Inventory (Integrated-Service Switch × Deployment Gate)

Whether "CD is enabled" is decided by the integrated-service switch in the Devops-Glue console: CD reads `enabled` and `provision` through the contract endpoint `GET /api/integration/services/cd` instead of guessing from "does the table exist".

| Contract reply | What CD does | Gate |
|--------------------|--------------|------|
| not enabled (`enabled<>1`) | zero DDL | 503 `errors.cd_not_enabled` (enable it in the CI console under "Integrated Services") |
| `enabled=1` + `provision='auto'` | on first connect: create every listed table + add missing columns + indexes (MySQL and SQLite alike) | incomplete => 503 `errors.cd_not_deployed` (restart CD to self-heal) |
| `enabled=1` + `provision='manual'` | zero DDL (schema owned by the DBA: MySQL imports `database/init_mysql.sql`) | missing table => 503 `errors.cd_not_deployed` (import the DDL script) |

Disabling the integration service only freezes admission: the CD tables and their data stay as they are — never `DROP`ped. The gate covers login, every request carrying a session, the public webhook receiver and the WebShell handshake.

**How the contract is read**: the only channel is Glue's narrow read-only endpoint `GET /api/integration/services/cd` (the existing service token's `build.read` scope is enough). When it cannot be reached (older Glue → 404/405, network/auth failure, `CI_API_URL` unset), CD fails closed as "contract unreachable" (503 `errors.ci_contract_unreachable`) — never treated as "not enabled". The channel is cached for a window (`CI_CONTRACT_TTL`, 30s by default; `0` = ask every request), so a switch flipped in the console takes effect within one window.

The CI→CD deployment-log direction mirrors it: CI pulls deployment records over the read-only HTTP endpoint `GET /api/integration/deploy-logs` (inbound static token `CD_API_TOKEN` + Bearer; 10 columns + filters + pagination). The shared `v_glue_deploy_logs` view is removed.

**Adding or removing a CD table touches four places** (gate verdict, error texts, startup warning and counts all follow automatically — nothing is hard-coded):

| # | Location | Notes |
|---|----------|-------|
| 1 | `backend/database.py::CD_REQUIRED_TABLES` | The single inventory; its order is the order shown in error messages |
| 2 | `database/init_mysql.sql` | DDL for `manual` mode (idempotent, safe to re-run) |
| 3 | `backend/database.py::_MYSQL_TABLE_DDL` (with `_MYSQL_INDEXES` / `_MYSQL_COLUMN_MIGRATIONS`) | MySQL auto-create for `auto` mode: `.dockerignore` excludes `database/`, so the ops script never reaches the image and the DDL must be embedded |
| 4 | `backend/database.py::_ensure_cd_tables` | SQLite auto-create for `auto` mode; ends with an **inventory self-check** that fails the first connection if the DDL was not updated |

Inventory drift is guarded four ways: `backend/tests/test_cd_schema_sources.py` locks table sets, per-table columns and indexes across 1↔2↔3↔4 (reflecting the real SQLite DDL), `backend/tests/test_cd_deploy_gate.py` locks the runtime behaviour (three contract states × two drivers), and the inventory self-check inside `_ensure_mysql_tables` / `_ensure_cd_tables` is the last line of defence.

**How the modes and drivers differ**:

- **`auto`**: CD creates its tables on first connect (MySQL from the embedded `_MYSQL_TABLE_DDL`, SQLite via `_ensure_cd_tables`) and adds missing columns to existing tables; when tables are incomplete the gate answers 503 and tells you to restart CD.
- **`manual`**: CD issues zero DDL (not even ALTER); MySQL is imported by an administrator from `database/init_mysql.sql`, SQLite is completed by an operator (or CI is switched back to auto-migrate and CD re-enabled).
- **Hints tell the truth per `provision`**: `cd_deploy_hint(driver, provision)` and `cd_not_enabled_hint()` (see `backend/database.py`) are the only outlets, so operators are never sent to the wrong fix. For "not enabled" the **user-visible notice is a single sentence** (the `errors.cd_not_enabled` i18n string — login box and toast share one path, `error_key` → i18n: neither appends extra text, both follow the UI language) — the action detail travels in `detail.hint` and the startup log.

- **Contract unreachable / database unavailable ≠ not enabled / not deployed**: contract endpoint unreachable => `ContractUnavailableError` => 503 `errors.ci_contract_unreachable` (`reason` travels in `error_params`; check Glue / `CI_API_URL` / the CI API token); cannot connect, cannot create tables (DB file read-only / disk full / locked), cannot get a connection => `DatabaseUnavailableError` => 503 `errors.db_unavailable` (`driver` / `reason` travel in `error_params`; check connectivity, configuration, permissions, disk). Only "not enabled" is `errors.cd_not_enabled` (go flip the switch) and only an incomplete inventory is `errors.cd_not_deployed` (go complete the tables). All are 503 but with different error keys and different actions — mixing them points operators the wrong way.

## Problem Statement

Fragmented DevOps toolchain for SMBs:
- Git platforms (GitLab/Gitee/GitHub/Gitea) → Unified integration
- CI engines (Jenkins/GitLab CI) → Dual-channel unification
- Image registry (Harbor) → Scan & sync
- Deploy targets (SSH/Docker/K8s) → Unified execution
- Notifications (DingTalk/WeCom) → Auto-push