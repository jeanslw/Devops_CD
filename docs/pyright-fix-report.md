# Pyright 静态类型检查修复报告

> 修复日期：2026-08-06
> 检查工具：Pyright (npx pyright)
> 修复结果：**54 errors → 0 errors, 0 warnings, 0 informations**

---

## 一、背景

全项目 Pyright 静态类型检查共发现 54 个错误，涉及数据库连接层、部署器注册、路由参数、服务层等多个模块。根因集中在以下几点：

1. **数据库连接类型不一致**：`Database.conn()` 对 SQLite 返回原生连接、对 MySQL 返回包装类，导致下游 `conn.execute()` 调用出现联合类型推断失败。
2. **部署器注册表类型过严**：`DeployerRegistry` 强制要求工厂返回 `Deployer` 类型，但 K8S 子模式部署器继承的是 `K8sSubDeployer`（独立 ABC），未实现 `Deployer` 接口。
3. **路由层变量遮蔽**：函数参数名 `user: dict` 与解包变量 `user: str` 同名，导致类型推断冲突。
4. **Optional 参数未处理**：`interval: int = None` 应为 `int | None`，且后续运算未做空值保护。
5. **第三方库类型不精确**：paramiko 的 `ssh.connect(**kwargs)` 参数类型与 `dict` 不完全匹配；`ssh.invoke_shell` 返回值可能为 `None`。

---

## 二、修复详情（按文件）

### 1. backend/database.py
- **问题**：`conn()` 方法对 SQLite/MySQL 返回不同类型，导致下游 `conn.execute()` 报 `union-attr` 错误。
- **修复**：重构 `_SqliteWrapper` 类，提供与 `_MysqlWrapper` 一致的 `execute/commit/rollback/close` 接口；`conn()` 始终返回 wrapper 对象。
- **影响范围**：所有调用 `conn.execute()` 的文件（deploy_service.py、registry_service.py、alert_service.py 等）。

### 2. backend/deployers/registry.py
- **问题**：`register()` / `create()` 签名要求 `Callable[[], Deployer]`，但 K8S 子模式部署器继承 `K8sSubDeployer` 而非 `Deployer`，注册时报 `reportArgumentType`。
- **修复**：将类型约束从 `Deployer` 放宽为 `Any`，移除 `from .base import Deployer` 导入。
- **理由**：`K8sSubDeployer` 与 `Deployer` 是并行的 ABC 体系，通过同一注册表管理是架构设计决策，不应强制统一接口。

### 3. backend/deployers/base.py
- **问题**：
  - `ssh.connect(**kwargs)` 报 `reportArgumentType`（paramiko 类型 stub 不精确）。
  - `ssh.get_transport()` 返回 `Transport | None`，直接调用 `.get_remote_server_key()` 报 `reportOptionalMemberAccess`。
- **修复**：
  - 两处 `ssh.connect(**kwargs)` 添加 `# type: ignore[arg-type]`。
  - `get_transport()` 结果增加 `None` 检查，抛出 `RuntimeError`。

### 4. backend/responses.py
- **问题**：`ok()` 函数中 `result = {"success": True}` 推断为 `dict[str, bool]`，后续赋值 `result["data"] = data`（Any 类型）报 `reportAssignmentType`。
- **修复**：显式标注 `result: dict[str, Any] = {"success": True}`。

### 5. backend/services/ci_client.py
- **问题**：`trigger_build()` 中 `body = {"ref": ref}` 推断为 `dict[str, str]`，后续 `body["variables"] = variables`（dict 类型）报类型不兼容。
- **修复**：显式标注 `body: dict[str, Any] = {"ref": ref}`。

### 6. backend/services/registry_service.py
- **问题**：`start_background_sync(db_factory, interval: int = None)` 中 `None` 赋给 `int` 类型参数；后续 `interval <= 0` 在 `None` 时报 `reportOptionalOperand`。
- **修复**：
  - 参数类型改为 `interval: int | None = None`。
  - 增加 `if interval is None: interval = int(...)` 与 `else: interval = int(interval)` 分支，确保后续运算类型安全。

### 7. backend/routers/ci_build.py
- **问题**：`ref = req.ref or None` 后传入 `trigger_build(project, ref: str, ...)`，`None` 不可分配给 `str`。
- **修复**：改为 `ref = req.ref or ""`，既满足 `str` 类型要求，又兼容 Jenkins 模式下 ref 可省略的业务逻辑。

### 8. backend/routers/k8s_deploy.py
- **问题**：
  - `_resolve_cluster()` 返回值类型未标注，Pyright 无法正确推断 tuple 解包类型，导致 `srv["host"]`（`Unknown | dict`）赋给 `host` 报 `reportAssignmentType`。
  - 函数参数 `user: dict = Depends(...)` 与函数体内 `host, port, user, pwd, ssh_key = _resolve_cluster(...)` 解包的 `user: str` 同名，类型冲突。
- **修复**：
  - `_resolve_cluster()` 添加返回类型 `-> tuple[str, int, str, str, str]`，并对 `srv` 字段显式 `str()/int()` 转换。
  - 函数参数 `user` 重命名为 `_user`，消除变量遮蔽。

### 9. backend/routers/terminal.py
- **问题**：
  - `ssh.invoke_shell()` 返回 `Channel | None`，后续 `chan.settimeout(0.0)` 报 `reportOptionalMemberAccess`。
  - `chan.send(text)` 传入 `str`，但 paramiko `send()` 要求 `bytes | bytearray`，报 `reportArgumentType`。
  - `file.filename.replace(...)` 中 `filename` 可能为 `None`，报 `reportOptionalMemberAccess`。
- **修复**：
  - `chan` 增加 `assert chan is not None` 断言。
  - `chan.send(text)` 改为 `chan.send(text.encode())` 并添加 `# type: ignore[arg-type]`。
  - `file.filename` 改为 `(file.filename or "")`。

---

## 三、修复策略总结

| 策略 | 适用场景 | 示例 |
|------|----------|------|
| **统一接口** | 同一方法返回不同类型导致下游报错 | `_SqliteWrapper` 对齐 `_MysqlWrapper` |
| **放宽泛型约束** | 架构上合理的并行类型体系 | `DeployerRegistry` 改用 `Any` |
| **显式类型标注** | 推断结果过窄导致后续赋值冲突 | `result: dict[str, Any]` |
| **空值保护** | Optional 返回值直接使用 | `assert chan is not None`、`None` 检查分支 |
| **变量重命名** | 参数名与局部变量遮蔽 | `user` → `_user` |
| **类型转换** | 数据库 Row 字段类型不确定 | `str(srv["host"])`、`int(srv["port"])` |
| **type: ignore** | 第三方库 stub 类型不精确 | paramiko `ssh.connect(**kwargs)` |

---

## 四、验证结果

```
$ npx pyright
0 errors, 0 warnings, 0 informations
```

所有修改文件均通过 `py_compile` 编译验证，无运行时语法错误。

---

## 五、修改文件清单

| 文件 | 修改类型 |
|------|----------|
| backend/database.py | 重构 _SqliteWrapper，统一 conn() 返回类型 |
| backend/deployers/registry.py | 放宽工厂类型约束为 Any |
| backend/deployers/base.py | SSH connect 类型忽略 + Transport None 检查 |
| backend/responses.py | dict 显式类型标注 |
| backend/services/ci_client.py | body dict 显式类型标注 |
| backend/services/registry_service.py | interval 参数 Optional + 空值保护 |
| backend/routers/ci_build.py | 补充导入 + ref 空值校验 |
| backend/routers/k8s_deploy.py | 返回类型标注 + 变量重命名 + 类型转换 |
| backend/routers/terminal.py | chan 断言 + bytes 编码 + filename 空值保护 |
| backend/services/deploy_service.py | 移除临时 type: ignore（因 conn 类型已统一） |
| backend/services/alert_service.py | 移除临时 type: ignore（因 conn 类型已统一） |
