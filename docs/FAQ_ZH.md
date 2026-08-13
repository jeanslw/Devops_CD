# Devops-Glue CD — 常见问题

## 自定义监控

### Q: 测试结果数值全是 "—"，但命令能正常输出？

**原因**：`field_key` 与输出表头不匹配。

**排查**：展开「原始输出 ▾」查看实际表头，展开黄色诊断面板对比。

**解决**：
- 中文环境表头是中文（如 `已用%`、`可用`），把 `field_key` 改成中文
- 或在命令前加 `LANG=C` 强制英文输出：`LANG=C df -h --type=ext4 --type=xfs`
- 数值带单位后缀（如 `20G`、`300M`）会被自动剥离，无需处理

### Q: 命令输出只有一行数字，怎么配？

不需要配置指标。格式选「自动检测」，直接填命令（如 `ps aux | wc -l`），系统自动取第一个数字。

### Q: 输出是表格但没有表头怎么办？

在命令里用 `echo` 预加表头：
```bash
echo "col1 col2 col3"; some-command-here
```

### Q: 输出行数不固定（如 `lscpu`），匹配不稳定？

用 `grep`/`awk` 过滤到固定行：
```bash
lscpu | grep -E '^CPU\(s\)|^Thread|^Core|^Socket'
```

### Q: 想监控多个不同指标，需要创建多个监控项吗？

不需要。同一个命令输出可以在一个监控项下添加多个指标，每个对应不同列。比如 `df -h` 同时配 `Use%`、`Avail`、`Used` 三条指标。

---

## 数据库

### Q: 启动报错 "ci_pipeline_tags 表不存在"？

cd_service 必须与 Devops-Glue API 共用同一个数据库。检查 `DB_PATH`（SQLite）或 `DB_*`（MySQL）配置是否正确。

### Q: SQLite 模式下，部署容器后页面没数据？

SQLite 模式下，容器需要挂载数据库文件所在目录为共享卷。在 `docker-compose.yml` 中确保：
```yaml
volumes:
  - /path/to/Devops-Glue/config/data:/shared_data
```
且 `.env` 中 `DB_PATH=/shared_data/data.db`

### Q: 生产环境用 SQLite 还是 MySQL？

推荐 MySQL。SQLite 在 CD 和 PHP API 同时写入时会遇到锁争用问题。

---

## Harbor

### Q: Harbor 仓库列表为空？

1. 确认 `.env` 中 `HARBOR_REGISTRY`、`HARBOR_USER`、`HARBOR_PASSWORD` 配置正确
2. Harbor 必须开启 API 访问（Harbor v1.x 或 v2.x 均可，系统自动探测）
3. 首次使用需在 Harbor 面板点「同步」按钮触发全量同步
4. 检查 Harbor 网络可达性：CD 服务器上 `curl https://hub.xxx.com/api/v2.0/health`

### Q: 同步很慢？

全量同步会拉取所有项目、仓库、Tag。可通过配置 `REGISTRY_SYNC_INTERVAL=3600`（秒）定时增量同步，或在 CD 面板手动触发同步时传入 `project` 参数做增量。

---

## SSH 连接

### Q: 添加服务器后连接超时？

1. 确认目标服务器 SSH 端口（默认 22）对外开放
2. 检查防火墙规则，CD 服务器 IP 是否在白名单
3. 如果使用跳板机，确认跳板机可达
4. 可通过 Web Shell 手动测试：`ssh -p <port> <user>@<host>`

### Q: SSH 密钥认证失败？

- 确认公钥已加入目标服务器 `~/.authorized_keys`
- 私钥格式必须是 PEM（`-----BEGIN RSA PRIVATE KEY-----`），不支持 OpenSSH 新格式
- 检查私钥权限：`chmod 600 ~/.ssh/id_rsa`

### Q: 端口不是 22？

在服务器配置中填写实际端口，系统会自动使用该端口建立 SSH 连接。

---

## K8s 监控

### Q: `kubectl top nodes` 无法采集到数据？

K8s 集群需安装 **metrics-server**：
```bash
kubectl get deployment metrics-server -n kube-system
```
如果不存在，参考 [metrics-server 文档](https://github.com/kubernetes-sigs/metrics-server) 安装。

### Q: K8s 1.24+ 集群，`docker stats` 不可用？

这是正常的。K8s 1.24 移除了 dockershim。Docker 级别的容器监控不可用，但 Pod 级别监控（`kubectl top pods`）不受影响。

---

## 部署

### Q: Docker Compose 部署失败，提示 image pull 失败？

1. Harbor 是否可访问：`docker login hub.xxx.com`
2. 构建时镜像 Tag 是否正确推送到了 Harbor
3. 在 CI 面板确认项目 Tag 存在

### Q: 部署日志显示中文乱码？

后端输出统一用 GBK/UTF-8 自动检测。如果 SSH 目标服务器编码特殊，可在命令中加 `export LANG=en_US.UTF-8`。

### Q: 停止服务后容器还在运行？

SSH 类型是纯透传模式，停止命令由用户自定义。检查服务器配置中的「停止命令」是否正确（如 `docker stop <container>` 或 `docker compose down`）。

### Q: Docker Compose 部署后，CD 连接 CI 报 "Connection refused"？

**原因**：`CI_API_URL` 配置错误。当 CD 和 CI 在同一个 docker-compose 里时，容器间通信用**服务名 + 容器内部端口**，不是宿主机端口。

**正确写法**：在 `docker-compose.yml` 的 `cd-service` → `environment` 下取消注释并修改：
```yaml
environment:
  # ── CI API 集成（和 PHP Devops-Glue 搭配时启用）──
  # 容器间通信用服务名，不要写 localhost
  CI_API_URL: http://devops-glue
  CI_API_TOKEN: dg_xxx      # API token（dg_ 前缀，服务账号/第三方）
  # 未配置 token 时回退账号登录：
  # CI_ADMIN_USER: root
  # CI_ADMIN_PASS: your_root_password
```

**提醒**：改完配置后必须 `docker-compose up -d` 重建容器，`restart` 不会刷新环境变量。

---

## 前端

### Q: `npm run build` 后页面空白？

FastAPI 在启动时缓存了 `static/index.html`。重新构建后需重启 CD 服务，或者通过代码确保每次请求重新读取（已修复）。

### Q: 开发模式（npm run dev）页面 404？

Vite 开发服务器在 5173 端口，需要在 `vite.config.js` 中配置代理转发 API 到 8000 端口：
```js
server: {
  proxy: { '/api': 'http://localhost:8001', '/ws': { target: 'ws://localhost:8001', ws: true } }
}
```

---

## 权限

### Q: 登录后侧栏有些菜单不显示？

权限由 CI 的 `roles` / `permissions` / `role_permissions` 三表管理，CD 只读取不写入。

**排查**：
1. 确认你的角色在 CI 的 `role_permissions` 表中分配了对应 `perm_key`
2. `super_admin` 角色隐含所有 `cd.*` 权限，无需单独分配
3. 权限清单见管理员配置手册

**没有 CI 管理后台时**，可以直接在数据库插入：
```sql
-- 查看当前角色的权限
SELECT r.name, GROUP_CONCAT(rp.perm_key) AS perms
FROM roles r
LEFT JOIN role_permissions rp ON r.id = rp.role_id
GROUP BY r.id;

-- 给 admin 角色添加所有 CD 权限
INSERT INTO role_permissions (role_id, perm_key)
SELECT r.id, p.perm_key
FROM roles r, permissions p
WHERE r.name = 'admin' AND p.perm_key LIKE 'cd.%';
```

### Q: 报错 "Permission denied: cd.xxx required"？

你的角色没有该权限 key。联系管理员在 CI 中分配，或使用 `super_admin` 账号。

### Q: CI 还没建 `roles`/`permissions` 表怎么办？

CD 会优雅降级：表不存在时，`get_current_user` 返回空权限列表。`super_admin` 角色仍然隐含全部权限（通过 `role` 字段判断），不影响超级管理员使用。建表后普通角色即可恢复正常。

### Q: 新增了权限点，前端/后端检查都失效？

新增的 `perm_key` 必须在 CI 的 `permissions` 表中插入记录，同时在 `role_permissions` 中分配给对应角色。否则 `require_perm()` 会拒绝所有用户。详见「协作原则」章节。

---

## 性能与资源

### Q: CD 服务内存占用高？

- 监控缓存可以通过环境变量调整：减小 `MONITOR_CACHE_*` 值可降低缓存驻留
- Harbor 同步是后台线程，调整 `REGISTRY_SYNC_INTERVAL` 增大间隔

### Q: 日志表增长太快？

`cd_deploy_logs` 表存储每次部署的输出。CD 服务本身不提供自动清理机制，需要运维人员自行清理。

**手动清理**（SQL）：
```sql
DELETE FROM cd_deploy_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);
```

**系统定时任务**（以 Linux crontab 为例）：
```cron
# 每天凌晨 2 点清理 30 天前的部署日志
0 2 * * * mysql -u root -p'password' devops_glue -e "DELETE FROM cd_deploy_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 30 DAY);"
```

**后台任务说明**：CD 服务自身有两个内置后台线程（`threading.Event.wait`），通过 `.env` 配置间隔：
- Harbor 同步：`REGISTRY_SYNC_INTERVAL`（默认 3600 秒 = 1 小时）
- 告警检查：`ALERT_CHECK_INTERVAL`（默认 300 秒 = 5 分钟）
