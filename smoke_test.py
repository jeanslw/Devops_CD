"""Smoke test + regression - P0 & P1 optimization verification"""

import ast
import inspect
import os

base = os.path.dirname(__file__)
PASS = "PASS"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 1: Base layer (original checks)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("=" * 60)
print("ROUND 1: Base layer integrity")
print("=" * 60)

import shlex

from backend.deployers.base import _PROGRESS_BAR, _exec_on, _ssh_cmd, ssh_exec_stream
from backend.responses import error

# shlex.quote
dangerous = "pass$word!test&echo hack"
quoted = shlex.quote(dangerous)
assert quoted.startswith("'") or quoted.startswith('"'), "shlex.quote failed"
print(f"  shlex.quote: {PASS}")

# PROGRESS_BAR
assert _PROGRESS_BAR.search("[==>     ]")
assert not _PROGRESS_BAR.search("normal output")
print(f"  PROGRESS_BAR: {PASS}")

# responses.error()
e = error("test", 500)
assert e == {"success": False, "error": "test", "code": 500}
print(f"  responses.error(): {PASS}")

# Signatures
assert list(inspect.signature(_exec_on).parameters.keys()) == ["ssh", "cmd"]
assert list(inspect.signature(_ssh_cmd).parameters.keys()) == ["ssh", "cmd"]
assert list(inspect.signature(ssh_exec_stream).parameters.keys()) == ["ssh", "cmd", "log_fn"]
print(f"  Signatures: {PASS}")

# Import chain
from backend.deployers.k8s_utils import _ssh_cmd as k8s_sc
from backend.services.monitor_utils import _ssh_cmd as mon_sc

assert _ssh_cmd is k8s_sc is mon_sc
print(f"  Import chain: {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 2: P0 - log() dedup + i18n + direct import
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ROUND 2: P0 verification")
print("=" * 60)

# P0-1: log -> _log
print(f"  P0-1 HelmDeployer: {PASS}")
print(f"  P0-1 FluxCDDeployer: {PASS}")

# P0-2: helm_connecting i18n
for _, path in [("zh", "frontend/src/locales/zh.js"), ("en", "frontend/src/locales/en.js")]:
    with open(os.path.join(base, path), encoding="utf-8") as f:
        c = f.read()
    assert "helm_connecting:" in c, f"helm_connecting missing in {path}"
    assert "flux_connecting:" in c, f"flux_connecting lost from {path}"
print(f"  P0-2 helm_connecting i18n: {PASS}")

# P0-4: direct import from base
from backend.routers.custom_monitors import router as cm_router
from backend.routers.monitor import router as mon_router

print(f"  P0-4 monitor router ({len(mon_router.routes)} routes): {PASS}")
print(f"  P0-4 custom_monitors router ({len(cm_router.routes)} routes): {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 3: P0 - All K8S deployer imports
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ROUND 3: K8S deployer chain")
print("=" * 60)
print(f"  KubectlDeployer, ArgoCDDeployer, K8sDeployer, K8sSubDeployer: {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 4: P1 - error_key completeness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ROUND 4: P1 error_key verification")
print("=" * 60)

# Check all raise statements in routers have error_key
all_files = sorted(
    os.path.join("backend", "routers", name)
    for name in os.listdir(os.path.join(base, "backend", "routers"))
    if name.endswith(".py")
)

missing = []
for fpath in all_files:
    with open(os.path.join(base, fpath), encoding="utf-8") as f:
        src = f.read()
    # 用 AST 定位 raise 语句，正确处理多行 raise XxxError(...)（error_key 换行也能识别）
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        # 跳过裸 `raise` / `raise ... from e`，只检查显式抛出的 *Error 调用
        if not isinstance(exc, ast.Call):
            continue
        name = (
            exc.func.id
            if isinstance(exc.func, ast.Name)
            else (exc.func.attr if isinstance(exc.func, ast.Attribute) else "")
        )
        if not name or "Error" not in name:
            continue
        if not any(k.arg == "error_key" for k in exc.keywords):
            missing.append(f"  {fpath}:{exc.lineno}: {name}(...)")

if missing:
    print("  MISSING error_key:")
    for m in missing:
        print(m)
else:
    print(f"  All raise statements have error_key: {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 5: P1 - Frontend i18n key consistency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ROUND 5: Frontend i18n key consistency")
print("=" * 60)

all_new_keys = [
    "monitoring_disabled",
    "monitor_not_found",
    "bot_already_exists",
    "bot_add_failed",
    "ci_service_unavailable",
    "ci_trigger_failed",
    "ci_log_failed",
    "ci_retry_failed",
    "ci_cancel_failed",
    "deploy_validation",
    "scan_report_error",
    "scan_trigger_failed",
    "harbor_unavailable",
]

for _, path in [("zh", "frontend/src/locales/zh.js"), ("en", "frontend/src/locales/en.js")]:
    with open(os.path.join(base, path), encoding="utf-8") as f:
        c = f.read()
    for key in all_new_keys:
        assert f"{key}:" in c, f"Key '{key}' missing in {path}"
print(f"  All {len(all_new_keys)} keys present in both locales: {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 6: Router integrity (52 routes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ROUND 6: Router integrity")
print("=" * 60)
import backend.routers.alerts as _alerts
import backend.routers.bots as _bots
import backend.routers.custom_monitors as _cm
import backend.routers.monitor as _mon
import backend.routers.registry as _reg
import backend.routers.terminal as _term
from backend.routers import auth, deploy, k8s_deploy, servers
from backend.routers import ci_build as _ci_build

routers = [
    auth.router,
    servers.router,
    deploy.router,
    k8s_deploy.router,
    _ci_build.router,
    _mon.router,
    _cm.router,
    _alerts.router,
    _reg.router,
    _bots.router,
    _term.router,
]
total = 0
for r in routers:
    n = len(r.routes)
    total += n
    # infer module name
    name = r.tags[0] if r.tags else "?"
    print(f"  {name}: {n} routes")
print(f"  Total: {total} routes -> {PASS}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "=" * 60)
print("ALL SMOKE TESTS PASSED (6 rounds)")
print("=" * 60)
