# Devops-Glue CD — User Manual

> For daily users: developers, ops, testers. Covers system overview, deployment operations, custom monitoring, and API reference.

## 1. Overview

Devops-Glue CD is a continuous deployment service that works alongside [Devops-Glue API](https://github.com/jeanslw/Devops-Glue.git) to deploy Harbor images to Docker or Kubernetes clusters. A unified panel handles project selection, tag confirmation, deployment execution, log viewing, and notification dispatch.

```
CI (Jenkins / GitLab CI)
  → build + push → Harbor
  → scan-sync → ci_pipeline_tags

CD Panel (this project)
  → Select project + tag
  → Select deployment mode + target server
  → SSH / API-driven deployment
  → Real-time log streaming
  → Persist to cd_deploy_logs
  → Webhook notification (DingTalk / WeCom / Custom)
```

## 2. Features

| Module | Description |
|--------|-------------|
| Deployment | Select CI project → choose tag → pick deployment mode → one-click deploy |
| Deploy Modes | SSH command, Ansible, Docker Compose, K8s kubectl/Helm/ArgoCD/FluxCD |
| Monitoring | CPU, memory, disk, Docker containers, K8s nodes/pods (real-time) |
| Custom Monitors | Execute custom SSH commands, parse CSV/KV/JSON output, monitor anything |
| Alerts | Threshold-based resource alerts, push via DingTalk/WeCom/custom webhooks |
| Web Shell | In-browser SSH terminal, SFTP file upload |
| Artifact Registry | Harbor image browsing, vulnerability scanning, safe tag deletion |
| Server Management | Server CRUD, tag-based grouping, SSH/Docker/K8s types |

## 3. Deployment Modes

| Type | Mode | Description |
|------|------|-------------|
| SSH (single host) | Custom Command | Shell script with `{image}` `{tag}` `{project}` placeholders |
| SSH (single host) | Ansible Playbook | `ansible-playbook -e image={image} -e tag={tag}` |
| Docker Compose | Remote YAML | `cd {path} && IMAGE={image} TAG={tag} docker compose up -d` |
| Docker Compose | Inline YAML | SFTP upload compose YAML → auto-create dir → startup |
| K8s kubectl | SSH apply | SSH to master → `kubectl apply -f` |
| K8s Helm | SSH kubectl | `helm upgrade --install` + version verification |
| K8s Argo CD | REST API | PATCH image → sync → poll until Healthy |
| K8s Flux CD | SSH kubectl | PATCH resource → wait for ready |

## 4. Custom Resource Monitoring

> Execute arbitrary commands on target servers via SSH and parse output into structured metrics. Supports CSV (header + rows), KV (key=value), and JSON formats.

### 4.1 Setup

1. Open the "Custom Resources" page
2. Fill in **resource name**, **monitor command**, and select **output format**
3. Add **metrics**: specify field key, metric name, and unit
4. Select **target servers** (SSH or Docker type)
5. Click "Test" to verify parsing

### 4.2 Examples

**Disk space monitoring:**
```bash
LANG=C df -h --type=ext4 --type=xfs
```
Format: Auto → Metrics: `Use%` (%), `Avail` (GB)

**GPU monitoring:**
```bash
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed --format=csv
```
Format: CSV → Metrics: `temperature.gpu` (°C), `fan.speed` (%)

**Memory monitoring:**
```bash
free -m
```
Format: Auto → Metrics: `used` (MB), `available` (MB)

**Process count:**
```bash
ps aux | wc -l
```
Format: Auto (single value, no metrics needed)

### 4.3 Troubleshooting

If test results show `—` for values:
- Expand "Raw Output" to see actual headers
- Ensure `field_key` **exactly matches** the header (case-sensitive)
- For non-English locales, prefix commands with `LANG=C`
- The diagnostic panel shows available headers vs. configured keys

## 5. Alert Rules

Set threshold alerts for system resources, Docker containers, K8s pods, and custom monitors.

| Resource Type | Description |
|---------------|-------------|
| CPU | 1-minute load average ÷ core count |
| Memory | Used percentage |
| Disk | Root partition usage |
| Docker CPU/Memory | Per-container resource usage |
| Pod CPU/Memory | K8s pod resource usage |
| Process CPU/Memory | Top process monitoring |
| Custom | Custom monitor metrics (select monitor + metric) |

The system checks periodically (default: 300s) and sends notifications via configured bots when thresholds are exceeded.

## 6. Project Structure

```
cd_service/
├── main.py              # Entry point
├── backend/
│   ├── routers/         # API routes (14 modules)
│   ├── services/        # Business logic layer
│   └── deployers/       # Deployers (SSH/Compose/kubectl/ArgoCD/FluxCD/Helm)
├── frontend/            # Vue 3 source
├── static/              # Frontend build output
├── database/            # DB scripts
└── docs/                # Documentation
```

## 7. API Reference

### Auth Legend

| Symbol | Meaning |
|:------:|---------|
| — | No authentication required |
| ✅ | Bearer Token required (`Authorization: Bearer <token>`) |
| 🔑 | Admin role required |

### Endpoints

| Method | Path | Auth | Description |
|--------|------|:----:|-------------|
| GET | `/health` | — | Health check |
| POST | `/api/login` | — | Login, returns Token |
| GET | `/api/me` | ✅ | Current user info |
| GET | `/api/projects` | ✅ | CI project list with latest tag |
| GET | `/api/projects/{p}/pipeline` | ✅ | Project pipeline status |
| GET | `/api/projects/{p}/tags` | ✅ | All tags for a project |
| GET | `/api/servers` | ✅ | Server list |
| POST | `/api/servers` | ✅ | Add server |
| PUT | `/api/servers/{id}` | ✅ | Update server |
| DELETE | `/api/servers/{id}` | ✅ | Delete server |
| GET | `/api/servers/tags` | ✅ | Tag groups |
| POST | `/api/deploy` | ✅ | Docker deployment |
| POST | `/api/deploy-k8s` | ✅ | K8s deployment |
| POST | `/api/stop` | ✅ | Stop service |
| GET | `/api/deploy-logs` | ✅ | Deployment log query |
| GET | `/api/bots` | ✅ | Notification bots list |
| POST | `/api/bots` | ✅ | Add bot |
| DELETE | `/api/bots/{id}` | ✅ | Delete bot |
| GET | `/api/monitor/servers` | ✅ | Monitor server list |
| GET | `/api/monitor/system/{id}` | ✅ | Server system resources |
| GET | `/api/monitor/nodes/{id}` | ✅ | K8s node metrics |
| GET | `/api/monitor/pods/{id}` | ✅ | K8s pod metrics |
| GET | `/api/monitor/docker/{id}` | ✅ | Docker container metrics |
| GET | `/api/custom-monitors` | ✅ | Custom monitor list |
| POST | `/api/custom-monitors` | ✅ | Create custom monitor |
| PUT | `/api/custom-monitors/{id}` | ✅ | Update custom monitor |
| DELETE | `/api/custom-monitors/{id}` | ✅ | Delete custom monitor |
| POST | `/api/custom-monitors/{id}/test` | ✅ | Test run |
| GET | `/api/alerts` | ✅ | Alert rules list |
| POST | `/api/alerts` | ✅ | Create alert rule |
| PUT | `/api/alerts/{id}` | ✅ | Update alert rule |
| DELETE | `/api/alerts/{id}` | ✅ | Delete alert rule |
| GET | `/api/registry/repositories` | ✅ | Harbor repository list |
| GET | `/api/registry/artifacts/{id}` | ✅ | Repository tag/artifact list |
| GET | `/api/registry/scan/{id}/{tag}` | ✅ | Tag vulnerability scan |
| DELETE | `/api/registry/artifacts/{id}` | ✅ | Delete tag (safety check) |
| POST | `/api/registry/sync` | ✅ | Trigger Harbor sync |
| WS | `/ws/terminal/{id}` | — | Web Shell terminal |
| POST | `/api/upload/{id}` | ✅ | SFTP file upload |
| GET | `/api/users` | 🔑 | User list |
| POST | `/api/users` | 🔑 | Create user |
| DELETE | `/api/users/{name}` | 🔑 | Delete user |
| PUT | `/api/users/{name}/role` | 🔑 | Change role |
| PUT | `/api/users/{name}/password` | ✅ | Change password (self or admin) |
| GET | `/` | — | Frontend SPA |
