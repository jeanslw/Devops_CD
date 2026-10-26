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

### Q: CD 提示「未启用」，但数据库看着已配置？

启用状态只经 Glue 的契约端点 `GET /api/integration/services/cd` 读取；问不到按「契约不可达」fail-closed（503 `errors.ci_contract_unreachable`）。请核对 `CI_API_URL` / `CI_API_TOKEN`，以及 `DB_PATH`（SQLite）/ `DB_*`（MySQL）指向 CD 表所在的库。

### Q: 登录报错「CD 服务未启用：请在 Devops-Glue 后台『API集成服务』中启用 Devops-CD」？

「CD 启用了没有」由 Devops-Glue 后台「API集成服务」的开关决定（CD 经契约端点 `GET /api/integration/services/cd` 读取）：`enabled<>1` 一律视为**未启用**，CD 做零 DDL，登录、所有已持有会话的请求、公开 Webhook 接收与 WebShell 握手统一 503（`errors.cd_not_enabled`）。

> 问不到契约端点（Glue 版本过旧、网络 / 鉴权失败）时按「契约不可达」fail-closed（503 `errors.ci_contract_unreachable`），不当「未启用」。

**解决**：到 Devops-Glue 后台「API集成服务 → 启用」开启 Devops-CD —— CD 侧**最迟一个契约缓存窗口（`CI_CONTRACT_TTL`，默认 30s）内生效**（不需要改配置，也不需要重启；想让它立刻生效可把 `CI_CONTRACT_TTL` 设为 `0`）。

> 提示框里**只有上面这一句**：登录页 error 框与 toast 同源 —— 都读 `error_key=errors.cd_not_enabled` 走前端 i18n（所以切 EN 会输出英文，不会出现「后端中文不跟随语言」）；完整动作说明随响应体 `detail.hint` 与 CD 启动日志下发，不会拼进提示框。

### Q: 登录报错「CD 服务未部署：CD 数据表未建全（缺失：...）」？

说明 CD **已启用**，但清单 `CD_REQUIRED_TABLES`（当前 14 张；以后加表/删表这张清单就是唯一准绳，少任何一张都当作未部署）里的表没建全。补表动作取决于契约行上的 `provision`：

- `provision='auto'`（CI 处于自动迁移模式）：**CD 会自动建表** —— 重建/重启 CD 服务即会建全自愈；错误提示里就是这么写的；
- `provision='manual'`（CI 处于手动建库模式）：CD **零 DDL**（连补列都不做），必须由管理员导入建表脚本把清单建全，脚本幂等、可重复执行，升级后也请重跑一次以补齐新增表/新列：

```bash
mysql -u root -p devops_glue < database/init_mysql.sql
```

另：闸门覆盖登录、已持有会话的请求、公开 Webhook 接收与 WebShell 握手（即「即使能登录，也禁止做任何操作」）。**关闭集成服务只冻结准入、不会删除数据**（CD 表与记录原样保留）；若想彻底清空，需自行 `DROP` 那些 `cd_*` 表（会一并丢弃会话/审批数据）。

> **SQLite 模式**（开发/单机）：`provision='auto'` 时 CD 表随库文件自动创建，正常情况下不会缺表；若你手工 `DROP TABLE` 过，闸门会**立刻** 503（只报警、不补建），**重启服务即自动建全自愈**。若 CI 侧是 `manual` 而 CD 用 SQLite，CD 同样不建表 —— 请把 CI 改回自动迁移模式后重新启用，或由运维补建（提示语会这样说，**不会**叫你去导入 MySQL 脚本）。

> **另一种 503 **：`errors.db_unavailable`（SQLite 库文件只读/磁盘满/被占用、库文件打不开、MySQL 取不到连接）是**环境**问题 —— 查连接配置、数据目录写权限、磁盘空间，**不要**去重跑建表脚本（那修不了）。

### Q: SQLite 模式下，部署容器后页面没数据？

SQLite 模式下，容器需要挂载数据库文件所在目录为共享卷。在 `docker-compose.yml` 中确保：
```yaml
volumes:
  - config/data:/shared_data
```
且 `.env` 中 `DB_PATH=/shared_data/data.db`

### Q: 生产环境用 SQLite 还是 MySQL？

推荐 MySQL。SQLite 在 CD 和 PHP API 同时写入时会遇到锁争用问题。

---

## 镜像仓库（Harbor / 标准 registry:2）

### Q: 仓库列表为空？

1. 确认 `.env` 中 `REGISTRY_USER`、`REGISTRY_PASSWORD` 配置正确（仓库**地址与类型不在 CD 本地配置**，由 Glue 契约端点下发）
2. Glue 契约（`GET /api/integration/services/cd`，需 Glue ≥ 2.11）下发的 `registry_type` 决定对接的仓库类型：`harbor`（须开启 Harbor API，v1.x / v2.x 均可，系统自动探测）或 `registry`（标准 OCI Distribution / registry:2，只用 `/v2/` 标准接口）；契约取不到该字段时「镜像仓库」页显示「未知」并提示排查（常见原因：服务凭据未在 Glue「API 管理」中勾选 Harbor 查询权限），内部对接按 `harbor` 处理
3. 契约取不到 `registry_url`（端口不可达 / Glue 过旧）时会直接报「未取到镜像仓库地址」——先确认 `CI_API_URL` / `CI_API_TOKEN` 能访问该端点
4. 首次使用需在「镜像仓库」面板点「同步」按钮触发全量同步
5. 检查网络可达性：Harbor 在 CD 服务器上 `curl https://hub.xxx.com/api/v2.0/health`；标准 registry 用 `curl https://reg.xxx.com/v2/`（返回 200 或 401/403 均算可达）

### Q: 用标准 registry:2 时为什么没有漏洞扫描、大小和推送时间？

标准 OCI Distribution 的 `/v2/` 接口只提供 Tag 列表，不提供漏洞扫描、镜像大小与推送时间。仓库类型为 `registry` 时这些操作均不提供，前端自动隐藏「扫描结果 / 报告 / 扫描」按钮及「大小」「推送时间」列，属预期行为而非故障。

### Q: 部署报 `server gave HTTP response to HTTPS client` / 明文 HTTP 仓库拉取不了？

契约下发的 `registry_url` 是 `http://...`（明文 HTTP）时：dockerd 对**未放行**的仓库只尝试 HTTPS，于是拉取报这条错。docker CLI 没有「跳过 TLS 校验」的开关（`--tls-verify` 是 podman / skopeo 的），`insecure-registries` 只能配在**目标机的 dockerd** 上，CD 无法替目标机表达这件事。

1. CD 的默认行为（部署前自动检测，部署日志会写明）：读目标机 `/etc/docker/daemon.json`（含 `daemon.json.d/*.json`），检查该 `host[:port]` 是否已在 `insecure-registries` 中（支持 `host` / `host:port` / CIDR 条目，与 dockerd 匹配语义一致）；**未放行即中止部署**并输出可复制的修复命令，且**不回退 Docker Hub**（避免把同名公共镜像当成自己的镜像部署上线）。
2. 按输出指引在**目标机**执行（需 root）：备份并合并 `insecure-registries` → `systemctl reload docker`（不生效则 `restart docker`，会重启该机容器）→ `docker pull <registry>/<repo>:<tag>` 复验。
3. 想让 CD 自动完成第 2 步：置 `REGISTRY_INSECURE_AUTO=true`（默认 `false`：只检测、不改目标机任何文件）。
4. 更推荐：给仓库加 TLS，把 Glue 契约的 `registry_url` 改为 `https://...`，CD 侧无需任何配置。
5. `.env` 里写入的镜像名保持**不带 scheme** 的 `host[:port]/repo:tag`；带 `http://` 会被 docker 判为 `invalid reference format`。
6. 旧开关 `REGISTRY_VERIFY_SSL`（原 `HARBOR_VERIFY_SSL`）**已废弃、无任何消费者**：它只能作用于 CD 进程自身的 TLS 校验，管不到目标机 dockerd 的拉取，设置它没有任何效果。

K8S 系列部署（ArgoCD / FluxCD / 原生 K8s）的镜像拉取由**节点 kubelet / containerd** 完成，明文仓库需在节点 containerd 的 `certs.d` / insecure 配置中放行，CD 不介入（本文档的自动检测与自动修复仅覆盖 CD 自己执行 `docker pull` 的 compose 远程部署）。

### Q: 同步很慢？

全量同步会拉取所有项目、仓库、Tag。可通过配置 `REGISTRY_SYNC_INTERVAL`（单位：分钟，默认 30，0 关闭）定时增量同步，或在 CD 面板手动触发同步时传入 `project` 参数做增量。

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

v1.1.1 起 SPA 路由每次请求都从磁盘重读 `static/index.html`（HTML 不缓存），重新构建后无需重启服务。若仍空白：强制刷新浏览器（Ctrl+F5）绕过缓存的 HTML/静态资源，并确认构建产物确实输出到了 `static/`（在 `frontend/` 下执行 `npm run build`）。

### Q: 开发模式（npm run dev）页面 404？

Vite 开发服务器在 5173 端口，`vite.config.js` 已把 `/api`、`/static`、`/ws` 代理到 CD 后端的 `8000` 端口 —— 后端需以 `PORT=8000` 启动（或按实际后端端口调整代理目标）：
```js
server: {
  port: 5173,
  proxy: {
    '/api': 'http://localhost:8000',
    '/static': 'http://localhost:8000',
    '/ws': { target: 'ws://localhost:8000', ws: true }
  }
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

**后台任务说明**：CD 服务内置以下后台线程（间隔通过 `config/app.env` 配置）：
- Harbor 同步：`REGISTRY_SYNC_INTERVAL`（默认 30 **分钟**，0 关闭）
- 告警检查：`ALERT_CHECK_INTERVAL`（默认 60 秒）
- 定时发布调度器：到点自动执行已批准且设定了 `scheduled_at` 的审批单
- 部署心跳：刷新 `cd_deploy_logs.heartbeat_at`，进程重启时可据此检测并收敛中断的部署
