# Devops-Glue — Architecture Overview

## Overall Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│                        CODE PUSH                            │
│  GitLab / Gitee / GitHub / Gitea  →  Webhook Trigger        │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                     CI 层：Devops-Glue API (PHP)            │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐   ┌──────────────┐      │
│  │   Jenkins    │  │  GitLab CI   │   │   自定义 CI  │      │
│  │ BuildProvider│  │ BuildProvider│   │ BuildProvider│      │
│  └──────┬───────┘  └──────┬───────┘   └──────┬───────┘      │
│         └─────────────────┼───────────────-──┘              │
│                           ↓                                 │
│              Build → Docker Image → Harbor Registry         │
│                           ↓                                 │
│              scan-sync → ci_pipeline_tags                   │
│                                                             │
│              CI 构建完成事件 ─────┐                         │
│           (project/tag/image/时间)│                         │
│                         POST ↓    │                         │
│              /api/webhooks/receive/{token}   │ CD 公开端点  │
└──────────────────────────┬──────────┼───────┬───────────────┘
                           ↓          ↓       │
┌─────────────────────────────────────────────────────────────┐
│                    CD 层：cd_service (Python)               │
│                                                             │
│  ┌──────────────────────┐       ┌──────────────────────┐    │
│  │  构建管理（HTTP API）│       │ Webhook 接收/转发    │    │
│  │ · 触发构建           │       │ · cd_webhooks（配置）│    │
│  │ · 构建历史/日志      │←─────→│ · cd_webhook_events  │    │
│  │ · 分支/变量          │   DB  │ · 自动转 Bot 通知    │    │
│  └──────────┬───────────┘       └──────────┬───────────┘    │
│             ↓                              │                │
│             ↓                       可选：自动转发          │
│   选择 Project + Tag  ──→  部署执行         ↓               │
│                                                             │
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
## Component Relationships

```
┌──────────────────────────────────────┐
│ 共享数据库 (SQLite / MySQL / MariaDB)│
│                                      │
│  ci_job_git_map        ← CI 只读     │
│  ci_pipeline_tags      ← CI 写/CD 读 │
│  cd_servers            ← CD 维护     │
│  cd_deploy_logs        ← CD 写       │
│  cd_bots               ← CD 维护     │
│  admin_users           ← 共享        │
└──────────┬───────────────────────────┘
           │
    ┌──────┴──────┐
    ↓             ↓
┌────────┐   ┌────────┐
│ PHP CI │   │PythonCD│
│:8080   │   │:8081   │
└────────┘   └────────┘
```

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