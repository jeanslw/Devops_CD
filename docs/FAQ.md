# Devops-Glue CD — FAQ

## Custom Monitoring

### Q: Test results show all values as "—" but the command outputs data?

**Cause**: `field_key` doesn't match the output headers.

**Diagnosis**: Expand "Raw Output" to see actual headers, check the diagnostic panel.

**Solution**:
- For non-English locales, headers may be in the local language. Either change `field_key` to match, or prefix the command with `LANG=C` for English output: `LANG=C df -h --type=ext4 --type=xfs`
- Values with unit suffixes (e.g. `20G`, `300M`) are automatically stripped — no manual handling needed

### Q: My command outputs a single number. How do I configure it?

No metrics needed. Choose "Auto Detect" format. The system automatically extracts the first number. Example: `ps aux | wc -l`

### Q: Output has no header row?

Prepend a header with `echo`:
```bash
echo "col1 col2 col3"; some-command-here
```

### Q: Output rows vary (e.g. `lscpu`)?

Filter with `grep`/`awk` to get stable rows:
```bash
lscpu | grep -E '^CPU\(s\)|^Thread|^Core|^Socket'
```

### Q: Can I monitor multiple metrics from one command?

Yes. Add multiple metrics under a single monitor item, each targeting a different column. E.g. for `df -h`, add `Use%`, `Avail`, and `Used` as separate metrics.

---

## Database

### Q: Startup error "ci_pipeline_tags table not found"?

cd_service must share the same database as Devops-Glue API. Verify `DB_PATH` (SQLite) or `DB_*` (MySQL) settings are correct.

### Q: SQLite mode — container deployment shows empty pages?

Mount the database directory as a shared volume in `docker-compose.yml`:
```yaml
volumes:
  - /path/to/Devops-Glue/config/data:/shared_data
```
And set `.env`: `DB_PATH=/shared_data/data.db`

### Q: SQLite or MySQL for production?

MySQL is strongly recommended. SQLite experiences lock contention when CD and PHP API write concurrently.

---

## Harbor

### Q: Harbor repository list is empty?

1. Verify `.env` settings: `HARBOR_BASE_URL`, `HARBOR_USER`, `HARBOR_PASSWORD`
2. Harbor API must be enabled (v1.x or v2.x, auto-detected)
3. Click "Sync" in the Harbor panel for initial full sync
4. Check connectivity: `curl https://hub.xxx.com/api/v2.0/health`

### Q: Sync is too slow?

Full sync pulls all projects, repositories, and tags. Set `REGISTRY_SYNC_INTERVAL` for periodic incremental sync, or trigger sync manually with a `project` parameter.

---

## SSH Connectivity

### Q: Server connection timeout after adding?

1. Verify SSH port (default 22) is open on the target
2. Check firewall rules — is the CD server's IP whitelisted?
3. Use Web Shell to test manually: `ssh -p <port> <user>@<host>`

### Q: SSH key authentication fails?

- Ensure the public key is in `~/.authorized_keys` on the target
- Key format must be PEM (`-----BEGIN RSA PRIVATE KEY-----`), not OpenSSH format
- Check key permissions: `chmod 600 ~/.ssh/id_rsa`

### Q: My server uses a non-standard SSH port?

Enter the actual port in server settings. The system uses this port for all SSH connections.

---

## K8s Monitoring

### Q: `kubectl top nodes` returns no data?

The K8s cluster needs **metrics-server** installed:
```bash
kubectl get deployment metrics-server -n kube-system
```
Install via [metrics-server docs](https://github.com/kubernetes-sigs/metrics-server) if missing.

### Q: K8s 1.24+ cluster — `docker stats` unavailable?

Expected behavior. K8s 1.24 removed dockershim. Docker-level monitoring is unavailable, but pod-level monitoring (`kubectl top pods`) continues to work.

---

## Deployment

### Q: Docker Compose deployment fails with image pull error?

1. Test Harbor access: `docker login hub.xxx.com`
2. Verify the image tag was pushed to Harbor during CI build
3. Confirm the project tag exists in the CI panel

### Q: Deployment logs show garbled characters?

The backend auto-detects GBK/UTF-8. For unusual server encodings, add `export LANG=en_US.UTF-8` to the deployment command.

### Q: Container still running after "Stop"?

SSH mode is pure passthrough — stop commands are user-defined. Verify the "Stop Command" in server settings (e.g. `docker stop <container>` or `docker compose down`).

### Q: CD reports "Connection refused" when connecting to CI under Docker Compose?

**Cause**: Incorrect `CI_API_URL`. When CD and CI are in the same docker-compose stack, containers communicate via **service name + internal port**, not the host-mapped port.

**Correct configuration**: Uncomment and edit in `docker-compose.yml` under `cd-service` → `environment`:
```yaml
environment:
  # ── CI API integration (enable when pairing with PHP Devops-Glue) ──
  # Use the service name for container-to-container communication, NOT localhost
  CI_API_URL: http://devops-glue
  CI_API_TOKEN: dg_xxx      # API token (dg_ prefix, service account / third-party)
  # Fallback account login when token not set:
  # CI_ADMIN_USER: root
  # CI_ADMIN_PASS: your_root_password
```

**Reminder**: After changing the config, you must run `docker-compose up -d` to recreate the container. `restart` does not refresh environment variables.

---

## Frontend

### Q: `npm run build` produces a blank page?

FastAPI caches `static/index.html` at startup. Restart the CD service after rebuilding, or ensure the code re-reads the file on each request (already fixed in v1.1.1).

### Q: Dev mode (`npm run dev`) shows 404?

Vite dev server runs on port 5173. Ensure `vite.config.js` has proxy configured for API calls to port 8001:
```js
server: {
  proxy: { '/api': 'http://localhost:8001', '/ws': { target: 'ws://localhost:8001', ws: true } }
}
```

---

## Permissions

### Q: Some sidebar menu items are missing after login?

Permissions are managed by CI's `roles` / `permissions` / `role_permissions` tables. CD only reads, never writes.

**Diagnosis**:
1. Verify your role has the corresponding `perm_key` in CI's `role_permissions` table
2. `super_admin` role has implicit access to all `cd.*` permissions — no separate assignment needed
3. See admin manual for full permission list

**Without CI admin UI**, insert directly in the database:
```sql
-- View current role permissions
SELECT r.name, GROUP_CONCAT(rp.perm_key) AS perms
FROM roles r
LEFT JOIN role_permissions rp ON r.id = rp.role_id
GROUP BY r.id;

-- Grant all CD permissions to admin role
INSERT INTO role_permissions (role_id, perm_key)
SELECT r.id, p.perm_key
FROM roles r, permissions p
WHERE r.name = 'admin' AND p.perm_key LIKE 'cd.%';
```

### Q: Error "Permission denied: cd.xxx required"?

Your role lacks that permission key. Ask an admin to assign it in CI, or use a `super_admin` account.

### Q: CI hasn't created `roles`/`permissions` tables yet?

CD degrades gracefully: when tables don't exist, `get_current_user` returns an empty permission list. The `super_admin` role still has implicit full access (via the `role` field), so super admins are unaffected. Normal roles will work after tables are created.

### Q: Added a new permission point but checks don't work?

New `perm_key` entries must be inserted into CI's `permissions` table AND assigned to roles in `role_permissions`. Otherwise `require_perm()` will deny all users. Always synchronize new permission keys with CI.

---

## Performance

### Q: High memory usage?

- Reduce monitor cache TTL via env vars: lower `MONITOR_CACHE_*` values
- Harbor sync is a background thread; increase `REGISTRY_SYNC_INTERVAL` to reduce frequency

### Q: Log table growing too fast?

`cd_deploy_logs` stores every deployment's output. The CD service does not have built-in log cleanup — this is an ops responsibility.

**Manual cleanup** (SQL):
```sql
DELETE FROM cd_deploy_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);
```

**System cron** (Linux crontab example):
```cron
# Clean up deployment logs older than 30 days, daily at 2 AM
0 2 * * * mysql -u root -p'password' devops_glue -e "DELETE FROM cd_deploy_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);"
```

**Background tasks**: The CD service has two built-in background threads (`threading.Event.wait`), configured via `.env`:
- Harbor sync: `REGISTRY_SYNC_INTERVAL` (default 3600s = 1 hour)
- Alert checking: `ALERT_CHECK_INTERVAL` (default 300s = 5 minutes)
