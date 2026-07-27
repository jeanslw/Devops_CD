# Devops-Glue CD — Administrator Manual

> For system administrators: requirements, configuration, deployment, and operations.

## 1. Prerequisites

### Python Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| Python | 3.10+ | Runtime |
| fastapi | 0.115+ | Web framework |
| uvicorn | 0.34+ | ASGI server |
| paramiko | 3.5+ | SSH / SFTP connectivity |
| kubernetes | 32.0+ | K8s Python client |
| pymysql | 1.1+ | MySQL driver |
| pydantic-settings | 2.0+ | Environment configuration |
| bcrypt | 4.2+ | Password verification |
| requests | 2.31+ | HTTP client |
| python-multipart | 0.0.9+ | File upload |

### Frontend

| Component | Description |
|-----------|-------------|
| Vue 3 + Vite | Main dashboard SPA |
| Vue Router 4 | Client-side routing (Web History mode) |
| xterm.js 5.3 | Web terminal (lazy-loaded) |

### Server-side (Optional)

| Component | Description |
|-----------|-------------|
| Docker / docker-compose | Single-host & Compose deployment targets |
| Kubernetes 1.19 ~ 1.31 | K8s cluster (see K8s version policy) |
| metrics-server | Prerequisite for resource monitoring |
| Argo CD v2.9+ / Flux CD | GitOps CD (optional) |
| Helm 3+ | K8s package manager (optional) |
| Ansible | Infrastructure automation (optional) |
| MySQL 8.0+ / MariaDB 10.4+ | Database (recommended for production) |

## 2. Environment Configuration

```env
# ── Database (required; must match Devops-Glue API) ──
DB_DRIVER=sqlite
DB_PATH=../Devops-Glue/config/data/data.db

# MySQL mode (recommended for production):
# DB_DRIVER=mysql
# DB_HOST=127.0.0.1
# DB_PORT=3306
# DB_NAME=devops_glue
# DB_USER=root
# DB_PASS=

# ── Harbor Registry ──
HARBOR_REGISTRY=hub.example.com
HARBOR_USER=admin
HARBOR_PASSWORD=

# ── Encryption Key (auto-generated on first run, DO NOT modify) ──
ENCRYPTION_KEY=

# ── SSH ──
SSH_TIMEOUT=30
SSH_DEFAULT_USER=root

# ── Docker Deployment ──
CONTAINER_RESTART_POLICY=always

# ── Monitoring & Alerting (seconds) ──
MONITOR_CACHE_SERVERS=60
MONITOR_CACHE_SYSTEM=30
MONITOR_CACHE_NODES=30
MONITOR_CACHE_PODS=30
MONITOR_CACHE_DOCKER=30
ALERT_CHECK_INTERVAL=300
REGISTRY_SYNC_INTERVAL=3600

# ── Log Truncation (characters) ──
LOG_TRUNCATE_CHARS=2000
NOTIFY_TRUNCATE_CHARS=200
```

## 3. Database

> **Critical**: cd_service has no independent database. It must share the same database instance as Devops-Glue API. On startup, it verifies that the `ci_pipeline_tags` table exists.

| Driver | Use Case | Notes |
|--------|----------|-------|
| `sqlite` | Dev / single instance | Must point to same `.db` file; mount shared volume for containers |
| `mysql` | Production (recommended) | Avoids SQLite write contention between CD and PHP API |

### Auto-created Tables

On first startup, the following CD tables are created automatically:

| Table | Description |
|-------|-------------|
| `cd_servers` | Deployment target servers |
| `cd_deploy_logs` | Deployment records |
| `cd_bots` | Notification bots |
| `cd_registry_repositories` | Harbor repository metadata |
| `cd_registry_artifacts` | Artifact/tag information |
| `cd_custom_monitors` | Custom monitor definitions |
| `cd_custom_monitor_metrics` | Monitor metric definitions |
| `cd_alert_rules` | Alert rules |
| `cd_alert_logs` | Alert history |

## 4. Deployment

### Direct Run

```bash
cd cd_service
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

pip install -r requirements.txt
cp .env.example .env
# Edit .env for database and Harbor

python main.py
# Open http://localhost:8081
```

### Docker Compose

```bash
cp .env.example .env
# Edit .env for database and Harbor

docker compose up -d
# Open http://localhost:8081
```

> **SQLite note**: When deploying in containers, mount the database directory as a shared volume so both CD and PHP API can access the same `.db` file.

### Frontend Build

```bash
cd frontend
npm install
npm run build    # Output to ../static/
```

## 5. Kubernetes Version Compatibility

This project interacts with K8s clusters via **SSH + kubectl CLI** (no Kubernetes Python API client), providing broad version compatibility.

| Feature | Min Version | Notes |
|------|:---:|------|
| Resource monitoring (kubectl top) | 1.8 | Requires **metrics-server** |
| Pod/Node info display | 1.2 | kubectl get -o custom-columns |
| kubectl apply / rollout | 1.2 | Basic deployment operations |
| Helm deployment | 3.0+ | Helm CLI, decoupled from K8s version |
| Argo CD deployment | 2.9+ | REST API, decoupled from K8s version |
| Flux CD deployment | 0.x / 2.x | kubectl patch, decoupled from K8s version |

| Version Range | Status | Notes |
|------|:--:|------|
| 1.8 ~ 1.18 | ⚠️ Theoretical | Not tested; early JSONPath fields may differ |
| 1.19 ~ 1.31 | ✅ Recommended | All features verified |
| 1.32+ | 🔮 Expected compatible | Core commands unchanged |

> K8s 1.24+ removed dockershim. Clusters using containerd/CRI-O: `docker stats` unavailable (does NOT affect `kubectl top pods`).

## 6. Security

### Password Encryption

Server passwords and SSH private keys are encrypted with Fernet symmetric encryption. `ENCRYPTION_KEY` is auto-generated on first run and written to `.env`. **Do not modify** — existing encrypted data will become unreadable.

### User Roles

| Role | Permissions |
|------|-------------|
| `admin` | Full access: deploy, server management, user management, system config |
| `deployer` | Deploy operations, view monitoring, manage servers |
| `viewer` | Read-only: view projects, deployment logs, monitoring data |

### Authentication

- Login: POST `/api/login` with username + password → returns Bearer Token
- Token format: Base64 encoded, contains username
- Protected endpoints require `Authorization: Bearer <token>` header
