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

# ── K8s Deployment ──
# FLUX_NAMESPACE=flux-system

# ── Monitoring & Alerting (seconds) ──
MONITORING_ENABLED=true
MONITOR_CACHE_SERVERS=60
MONITOR_CACHE_SYSTEM=30
MONITOR_CACHE_NODES=30
MONITOR_CACHE_PODS=30
MONITOR_CACHE_DOCKER=30
MONITOR_CACHE_POD_DETAIL=15
ALERT_CHECK_INTERVAL=60
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

#### Deploying with Devops-Glue

CD Service shares the same MySQL database (`devops_glue`) with Devops-Glue (CI). Three deployment strategies depending on host layout:

**Option A: Same-host Combined Deploy (Recommended)**

Add the cd-service block into Devops-Glue's `docker-compose.yml`. Docker DNS works automatically within the same compose file:

```yaml
# Append to Devops-Glue's docker-compose.yml:
  cd-service:
    build:
      context: ../cd_service
      dockerfile: Dockerfile
    container_name: cd-service
    env_file:
      - ../cd_service/.env
    environment:
      PYTHONUNBUFFERED: "1"
      HOST: 0.0.0.0
      PORT: 8081
    ports:
      - "8081:8081"
    restart: unless-stopped
    depends_on:
      mysql:
        condition: service_healthy
```

Set `DB_HOST=devops-mysql` (the MySQL container name in CI) in `.env`.

**Option B: Same-host Separate Deploy**

Two independent compose files on the same machine, connected via an external Docker network:

```bash
# 1. Create a shared network
docker network create devops-net
```

Both compose files join this network:

```yaml
# CD Service docker-compose.yml
networks:
  devops-net:
    external: true
```

```yaml
# Devops-Glue docker-compose.yml — append:
services:
  mysql:
    networks:
      - devops-net
  devops-glue:
    networks:
      - devops-net

networks:
  devops-net:
    external: true
```

Set `DB_HOST=devops-mysql` in the CD Service `.env`. Docker's built-in DNS resolves container names across the shared network. MySQL does not need to expose ports to the host.

**Option C: Cross-host Deploy**

CD Service and MySQL run on different machines. Container-name DNS no longer works — MySQL must expose its port and CD Service connects via the host IP.

Expose MySQL port in the CI compose file:

```yaml
# Devops-Glue docker-compose.yml
services:
  mysql:
    ports:
      - "3306:3306"   # Expose MySQL to host network
```

Ensure MySQL allows remote connections (default in the container), and open port 3306 in your firewall/security group.

CD Service `.env`:

```env
DB_DRIVER=mysql
DB_HOST=192.168.x.x    # IP of the MySQL host machine
DB_PORT=3306
DB_NAME=devops_glue
DB_USER=root
DB_PASS=your_password
```

> **Note**: In cross-host mode, the CD Service compose file can omit `networks` entirely (or use the default bridge) — `devops-net` is not needed.

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

### K8s Deployment Modes

Devops-Glue CD supports four K8s deployment modes, each with different working principles and CD roles:

#### kubectl Mode (Direct Deployment)

**Principle**: CD connects to K8s nodes/jump hosts via SSH, directly operates the cluster using `kubectl apply` + `rollout restart`.

**What CD does**:
1. SSH connection to target server (K8s node or bastion with kubectl configured)
2. Reads remote YAML file (or downloads URL YAML), replaces `{IMAGE}:{TAG}` with actual image
3. `kubectl apply -f` uploads and applies YAML to cluster
4. `kubectl rollout restart deployment/<name>` triggers rolling restart
5. `kubectl rollout status` waits for deployment to complete (default 120s timeout)
6. Verifies old/new Pod changes, determines deployment success/failure

**Use Cases**: Traditional kubectl operations without GitOps tools. Teams accustomed to directly applying YAML.

**Prerequisites**:
- kubeconfig configured on target server (~/.kube/config)
- kubectl version on server compatible with cluster
- YAML template contains `{IMAGE}:{TAG}` placeholder

#### Argo CD Mode

**Principle**: CD remotely updates Application parameters via Argo CD REST API, then triggers Sync. Argo CD handles the actual GitOps deployment.

**What CD does**:
1. Calls Argo CD API `GET /api/v1/applications/<name>` to find Application (or searches all Apps by image name if not found)
2. Selects update strategy based on Application type:
   - **Helm**: patches `spec.source.helm.parameters` with `image.tag`
   - **Kustomize**: patches `spec.source.kustomize.images` with `newTag`
3. Calls API `PUT /api/v1/applications/<name>` to submit update
4. Calls API `POST /api/v1/applications/<name>/sync` to trigger Argo CD sync
5. Polls Application status (`/api/v1/applications/<name>`), waits for Health = "Healthy" (max 60s)
6. Determines deployment success/failure based on Health/Sync status

**Use Cases**: Teams using Argo CD for GitOps. CD acts as a "trigger" for Argo CD, not directly operating the cluster.

**Prerequisites**:
- Argo CD deployed and accessible via HTTPS
- CD can obtain Argo CD API Token (passed via server's password field)
- Application already exists with Helm/Kustomize image parameters configured

**What CD does NOT do**:
- Does not directly operate kubectl
- Does not create/modify Argo CD Applications
- Does not manage Git repositories or Helm Charts

#### Flux CD Mode

**Principle**: CD directly patches Flux CD's HelmRelease/Kustomization resources via SSH + kubectl, triggers Flux reconcile, and Flux handles the actual deployment.

**What CD does**:
1. SSH connection to K8s node
2. Auto-discovers Flux resources (first tries exact project name match, then scans all HelmRelease/Kustomization in `flux-system` namespace by image name)
3. Uses `kubectl patch` to update image tag:
   - **HelmRelease**: patches `spec.values.image.tag`
   - **Kustomization**: patches `spec.images[].newTag`
4. Uses `kubectl annotate` to add `reconcile.fluxcd.io/requestedAt` annotation, forcing Flux to reconcile immediately
5. Polls for Flux reaction (max 90s):
   - Detects new Pod appearing or old Pod terminating
   - Checks if Flux resource's Ready condition shows errors
6. Finds corresponding Deployment name, executes `kubectl rollout status` to wait for rollout completion
7. Determines success/failure based on rollout status result

**Use Cases**: Teams using Flux CD for GitOps. CD acts as a "trigger" for Flux, enabling rapid iteration via kubectl patch + annotate.

**Prerequisites**:
- Flux CD deployed in `flux-system` namespace
- Cluster accessible via SSH + kubectl
- HelmRelease/Kustomization resources already exist with correct images referenced

**What CD does NOT do**:
- Does not directly create Pods/Deployments
- Does not manage Git repositories
- Does not install/configure Flux CD

#### Mode Comparison

| Aspect | kubectl | Argo CD | Flux CD |
|--------|---------|---------|---------|
| Cluster Interaction | SSH + kubectl CLI | REST API (HTTPS) | SSH + kubectl CLI |
| Deployment Executor | CD directly executes kubectl | Argo CD executes | Flux CD executes |
| Image Update | Render YAML → apply | API patch Application params | kubectl patch HelmRelease/Kustomization |
| Trigger Method | Direct apply | API call sync | annotate triggers reconcile |
| Deployment Time | Fastest (~30s) | Medium (~60s, includes sync wait) | Longer (~90s, includes Flux reconcile) |
| Network Required | SSH access to K8s node | HTTPS access to Argo CD | SSH access to K8s node |
| Additional Components | None | Argo CD + Token | Flux CD + kubectl |
| Suitable Teams | Traditional kubectl ops | Argo CD GitOps | Flux CD GitOps |

## 6. Security

### Password Encryption

Server passwords and SSH private keys are encrypted with Fernet symmetric encryption. `ENCRYPTION_KEY` is auto-generated on first run and written to `.env`. **Do not modify** — existing encrypted data will become unreadable.

### User Roles

| Role | Permissions | Note |
|------|-------------|------|
| `admin` | Full access: deploy, server management, user management, system config | Assigned by CI system only; CD cannot create/delete/modify admin |
| `deployer` | Deploy operations, view monitoring, manage servers | Created by CD admin |
| `viewer` | Read-only: view projects, deployment logs, monitoring data | Created by CD admin |

> **CD/CI Login Isolation**: The `admin_users` table has a `systems` column (comma-separated). CD validates login by checking for `"cd"` in this field. Only users with `"cd"` in `systems` can log in to CD. CI manages the full account lifecycle (creating admins, assigning systems), while CD only manages deployer/viewer.

### Authentication

- Login: POST `/api/login` with username + password → returns Bearer Token
- Token format: Base64 encoded, contains username
- Protected endpoints require `Authorization: Bearer <token>` header
