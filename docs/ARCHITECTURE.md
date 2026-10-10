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

## Problem Statement

Fragmented DevOps toolchain for SMBs:
- Git platforms (GitLab/Gitee/GitHub/Gitea) → Unified integration
- CI engines (Jenkins/GitLab CI) → Dual-channel unification
- Image registry (Harbor) → Scan & sync
- Deploy targets (SSH/Docker/K8s) → Unified execution
- Notifications (DingTalk/WeCom) → Auto-push