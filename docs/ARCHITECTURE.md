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
		│ Shared Database (SQLite / MySQL / MariaDB)  │
		│                                             │
		│  ci_pipeline_artifacts ← CI write / CD read │
		│  v_glue_deploy_logs    ← CI read-only view  │
		│  cd_servers            ←  CD maintain       │
		│  cd_deploy_logs        ← CD write           │
		│  cd_approvals(_rules)  ←  CD write          │
		│  cd_sessions           ← CD write / delete  │
		│  cd_bots               ← CD maintain        │
		│  admin_users           ←  shared            │
		└────────────────────┬────────────────────────┘
							 │
					  ┌──────┴──────┐
					  ↓             ↓
				  ┌────────┐   ┌────────┐
				  │ PHP CI │   │PythonCD│
				  │:8080   │   │:8081   │
				  └────────┘   └────────┘
```

> **Data ownership**: besides the shared tables above, CD maintains its own `cd_*` tables (webhooks, monitors, alerts, registry cache, config, sessions, approvals). CI-specific build data (pipelines, mappings, build records) is fetched from CI over its HTTP API — CD does not read other `ci_*` tables directly.

**Database Selection**: PHP CI and CD Service must use the same database instance.
- **SQLite**: Zero-config, suitable for single-host dev/test. Container deployments must mount a shared volume so both processes can access the same `.db` file.
- **MySQL 8.0+ / MariaDB 10.4+**: Recommended for production. Supports concurrent read/write, no shared volume needed.

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
- **Dual-driver Database**: SQLite / MySQL / MariaDB unified interface, one codebase for three modes, sharing the same database instance with PHP API

## CD Table Inventory (Deployment Gate)

Whether CD is enabled is decided solely by the single inventory `backend/database.py::CD_REQUIRED_TABLES` (independent of how many tables it holds): as soon as **every** listed table exists, CD counts as deployed; if even one is missing, CD counts as not deployed — login, every request carrying a session, the public webhook receiver and the WebShell handshake all return 503 `errors.cd_not_deployed`, and the app only reports, it **never creates tables**. That inventory is the watershed between "CD enabled" and "CD not deployed".

**Adding or removing a CD table touches only three places** (gate verdict, error texts, startup warning and counts all follow automatically — nothing is hard-coded):

| # | Location | Notes |
|---|----------|-------|
| 1 | `backend/database.py::CD_REQUIRED_TABLES` | The single inventory; its order is the order shown in error messages |
| 2 | `database/init_mysql.sql` | MySQL DDL (the only way to enable CD in production; idempotent, safe to re-run) |
| 3 | `backend/database.py::_ensure_cd_tables` | SQLite auto-create; ends with an **inventory self-check** that fails the first connection if the DDL was not updated |

Inventory drift is guarded three ways: `backend/tests/test_cd_deploy_gate.py` cross-checks 1↔2 in both directions and asserts set coverage for 1↔3 (using `len(inventory)`, never a hard-coded count), with the runtime self-check in `_ensure_cd_tables` as the last line of defence.

**How the two drivers differ**:

- **MySQL**: tables come only from the administrator importing `database/init_mysql.sql`; the app issues zero DDL (only idempotent ADD COLUMN migrations on **existing** tables). Incomplete ⇒ gate 503 pointing at the import script.
- **SQLite**: tables are created automatically with the DB file, so development is frictionless; if one is manually `DROP TABLE`d at runtime the gate immediately answers 503 (no silent repair) and **restarting the service re-creates them and heals it**; the message gives the driver-correct action (`cd_deploy_hint()` reads `Database.driver`), so nobody is sent to the wrong fix.

- **Database unavailable ≠ not deployed**: cannot connect, cannot create tables (DB file read-only / disk full / locked), cannot get a connection ⇒ `DatabaseUnavailableError` ⇒ 503 `errors.db_unavailable` (`driver` / `reason` travel in `error_params`; check connectivity, configuration, permissions, disk). Only an incomplete inventory yields `errors.cd_not_deployed` (go complete the tables). Both are 503 but with different error keys and different actions — mixing them points operators the wrong way.

## Problem Statement

Fragmented DevOps toolchain for SMBs:
- Git platforms (GitLab/Gitee/GitHub/Gitea) → Unified integration
- CI engines (Jenkins/GitLab CI) → Dual-channel unification
- Image registry (Harbor) → Scan & sync
- Deploy targets (SSH/Docker/K8s) → Unified execution
- Notifications (DingTalk/WeCom) → Auto-push