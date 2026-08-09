# Devops-CD — User Manual

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
| CI Build Management | Trigger Jenkins/GitLab CI builds, view build history and build logs (HTTP proxy to CI API) |
| Webhook Receiver | Receive CI build/deploy events, auto-forward to DingTalk/WeCom/custom bots, browse event history |
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

## 6. CI Build Management

The **Build Management** menu proxies requests to Devops-Glue (CI) HTTP API, enabling Jenkins/GitLab CI build triggering, build history, and console logs directly from the CD dashboard.

### 6.1 Prerequisites

- Admin has configured `.env` with `CI_API_URL`, `CI_API_USER`, `CI_API_PASS` and restarted the service (see Admin Manual §7).
- Your user has CD login access (`systems` contains `"cd"`).

### 6.2 Daily Usage

1. Open **Build Management** from the sidebar.
2. Pick a project from the list — latest build status is shown at a glance.
3. Click **Trigger Build**: select branch/tag, optionally pass custom build variables, and submit.
4. Builds appear in the history list. Click the log icon to view real-time console output (streaming from CI API).

### 6.3 Notes

- Build history and logs are fetched on-demand from CI; CD stores no build artifact locally.
- Write operations are performed on CI system via API — they are governed by CI permissions.

## 7. Webhook Receiver & Event Forwarding

The **Webhooks** menu lets you create secure public endpoints for CI/Jenkins/GitLab to push build-completion events into CD, with optional auto-forwarding to notification bots.

### 7.1 Concepts

| Term | Meaning |
|------|---------|
| Webhook Config | An endpoint definition with a unique 32-char random token URL |
| Linked Bot | Optional `cd_bots` entry — every incoming event is automatically forwarded to this bot |
| Event | A received POST payload stored in `cd_webhook_events`, with timestamp and forward status |

### 7.2 Creating a Webhook

1. Open **Webhooks** → **Create** (requires `cd.notification-manage` permission).
2. Enter a **name** (e.g. `Jenkins Build OK`), and optionally select a **notification bot** for auto-forwarding.
3. After save, the system shows the public endpoint:
   ```
   https://<cd-host>:8081/api/webhooks/receive/<32-char-token>
   ```
4. Copy-paste the endpoint into Jenkins Pipeline / GitLab CI post-build step:
   ```bash
   curl -s -S -X POST "<endpoint>" \
     -H "Content-Type: application/json" \
     -d "{\\"project\\":\\"$JOB_NAME\\",\\"tag\\":\\"$TAG\\",\\"image\\":\\"$IMAGE:$TAG\\",\\"built_at\\":\\"$(date +'%Y%m%d%H%M%S')\\"}"
   ```
   > Use **double quotes** around `-d` payload so `$VAR`/`$(cmd)` shell expansions work; prefer `jq` to avoid escaping errors.

### 7.3 Payload Recommended Fields

No strict schema — any JSON is accepted. The following fields are detected and used when formatting bot messages:

`project`, `tag`, `image`, `built_at` (or `time`), `status`, `target`, `mode`.

Custom Bot templates support `{project}` `{tag}` `{image}` `{status}` `{time}` `{target}` `{mode}` placeholders.

### 7.4 Event Browsing & Actions

- Click the **Events** button on a Webhook row to browse paginated history (default 20/page, max 100).
- Events show `payload` (raw JSON), `received_at`, `forwarded` status, `forwarded_at`.
- **Manual forward**: choose a Bot and retry forwarding any event — useful when Bot was temporarily unreachable.
- **Delete event**: clean up old or oversized payloads.

### 7.5 Disable / Enable / Delete

- Use the toggle switch on the list to quickly disable a Webhook without deleting it — disabled endpoints return 404.
- Delete a Webhook permanently removes all its events.

## 8. Project Structure

```
cd_service/
├── main.py              # Entry point
├── backend/
│   ├── routers/         # API routes (16 modules: + webhooks, + ci_build)
│   ├── services/        # Business logic layer
│   └── deployers/       # Deployers (SSH/Compose/kubectl/ArgoCD/FluxCD/Helm)
├── frontend/            # Vue 3 source
├── static/              # Frontend build output
├── database/            # DB scripts
└── docs/                # Documentation
```

## 9. API Reference

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
| GET | `/api/info` | — | Public info (version, DB type/status, uptime) |
| POST | `/api/login` | — | Login, returns Token |
| GET | `/api/me` | ✅ | Current user info |
| GET | `/api/projects` | ✅ | CI project list with latest tag |
| GET | `/api/projects/{p}/pipeline` | ✅ | Project pipeline status |
| GET | `/api/projects/{p}/tags` | ✅ | All tags for a project |
| **CI Build (HTTP proxy to CI API)** | | | |
| GET | `/api/ci/projects` | ✅ | CI project list |
| GET | `/api/ci/{pid}/builds` | ✅ | Build history for a CI project |
| POST | `/api/ci/{pid}/build` | ✅ | Trigger build (branch/tag + custom variables) |
| GET | `/api/ci/{pid}/build/{bid}/log` | ✅ | Build console log (streaming) |
| GET | `/api/ci/{pid}/variables` | ✅ | CI project build variables |
| GET | `/api/ci/{pid}/branches` | ✅ | Git branch/tag list |
| GET | `/api/ci/health` | ✅ | CI API connectivity health check |
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
| **Webhooks (notification management)** | | | |
| GET | `/api/webhooks` | ✅ | Webhook config list |
| POST | `/api/webhooks` | 🔑 | Create webhook (requires `cd.notification-manage`) |
| PATCH | `/api/webhooks/{wid}` | 🔑 | Update webhook (name, linked Bot) |
| DELETE | `/api/webhooks/{wid}` | 🔑 | Delete webhook + all its events |
| POST | `/api/webhooks/{wid}/toggle` | 🔑 | Enable/disable webhook |
| GET | `/api/webhooks/{wid}/events` | ✅ | Paginated event list for a webhook |
| DELETE | `/api/webhooks/events/{eid}` | 🔑 | Delete single event |
| POST | `/api/webhooks/events/{eid}/forward` | ✅ | Manually forward event to a Bot |
| **Webhook receiver (public)** | | | |
| POST | `/api/webhooks/receive/{token}` | — | Public endpoint for CI/Jenkins/GitLab to push events (token in URL) |
| GET | `/` | — | Frontend SPA |
