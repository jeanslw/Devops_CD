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

1. Verify `.env` settings: `HARBOR_REGISTRY`, `HARBOR_USER`, `HARBOR_PASSWORD`
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
