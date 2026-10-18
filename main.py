"""
Devops-Glue CD Service
FastAPI 部署执行器 - SSH / docker-compose / K8s

架构: main.py(入口) → routers → services → deployers
"""

import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import requests

from backend import __version__
from backend.access_log import install_access_log_filter, install_console_log_filter
from backend.config import settings
from backend.database import (
    Database,
    ContractUnavailableError,
    DatabaseUnavailableError,
    cd_deploy_hint,
    cd_not_enabled_hint,
)
from backend.exceptions import AppException
from backend.net import install_ip_family
from backend.routers import (
    alerts,
    approvals,
    auth,
    bots,
    ci_build,
    custom_monitors,
    deploy,
    k8s_deploy,
    logs,
    monitor,
    oidc,
    projects,
    registry,
    servers,
    tags,
    terminal,
    users,
    webhooks,
)
from backend.services.alert_service import start_alert_checker, stop_alert_checker
from backend.services.registry_service import RegistryService, start_background_sync, stop_background_sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时:定时同步 + 告警检查 + 审批崩溃恢复"""
    db = Database()
    if not settings.oidc_ready():
        logging.getLogger(__name__).error(
            "OIDC 登录缺少必填项：%s，SSO 登录会失败。OIDC_ISSUER 在 config/app.env 或根 .env 配置，"
            "CD_OIDC_SECRET / CD_OIDC_REDIRECT_URI 在根 .env 配置",
            "、".join(settings.oidc_missing_settings()),
        )
    try:
        state = db.cd_integration_state()
        if not state.enabled:
            logging.getLogger(__name__).warning(
                "Devops-CD 尚未启用（契约端点返回 enabled 不为 1）："
                "登录与所有接口均被拒绝（503 errors.cd_not_enabled）。%s",
                cd_not_enabled_hint(),
            )
        else:
            missing = db.cd_missing_tables()
            if missing:
                logging.getLogger(__name__).error(
                    "CD 数据表未建全（缺失 %d 张：%s；provision=%s）：%s —— 当前登录与所有接口均被拒绝（503）",
                    len(missing),
                    "、".join(missing),
                    state.provision,
                    cd_deploy_hint(db.driver, state.provision),
                )
    except ContractUnavailableError as e:
        logging.getLogger(__name__).error(
            "无法读取集成服务契约：%s —— 请求将返回 503 errors.ci_contract_unreachable", e.message
        )
    except DatabaseUnavailableError as e:
        logging.getLogger(__name__).error(
            "数据库当前不可用（驱动 %s）：%s —— 请求将返回 503 errors.db_unavailable", db.driver, e.message
        )
    except Exception:  # 探测故障不阻断启动
        logging.getLogger(__name__).exception("cd_missing_tables check failed")
    try:
        svc = RegistryService(db)
        interval = svc.get_sync_interval()
    except Exception:
        interval = -1
    start_background_sync(lambda: Database(), interval)
    start_alert_checker()
    # 审批:进程重启后清僵尸部署锁,把 deploying 审批单按部署记录终态收敛;
    # 并启动定时发布调度器(到点以申请人身份自动执行已批准的定时单据)
    try:
        from backend.services.approval_service import (
            recover_on_startup,
            start_scheduled_executor,
        )

        recover_on_startup(db)
        start_scheduled_executor()
    except Exception:
        logging.getLogger(__name__).exception("approval recovery/scheduler bootstrap failed")
    yield

    # ── 优雅停机:先停后台线程,再取消进行中的部署并等待收尾 ──
    import logging as _logging

    _log = _logging.getLogger(__name__)
    try:
        stop_background_sync()
        stop_alert_checker()
        from backend.services.approval_service import stop_scheduled_executor

        stop_scheduled_executor()
    except Exception:
        _log.exception("background threads shutdown failed")
    try:
        from backend.deploy_run import shutdown_running_deploys, stop_heartbeat

        pending = shutdown_running_deploys(timeout=25.0)
        stop_heartbeat()
        if pending:
            _log.warning("graceful shutdown: %s deploy(s) still active after timeout", pending)
    except Exception:
        _log.exception("deploy graceful shutdown failed")


app = FastAPI(title="Devops-Glue CD", version=__version__, lifespan=lifespan)
BASE_DIR = Path(__file__).parent
_STARTED_AT = datetime.now(timezone.utc)

# 导入期安装,覆盖三种启动方式
install_access_log_filter()
install_console_log_filter()
install_ip_family()

# 安全响应头(所有响应统一附加)
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


app.include_router(auth.router)
app.include_router(oidc.router)
app.include_router(projects.router)
app.include_router(servers.router)
app.include_router(deploy.router)
app.include_router(approvals.router)
app.include_router(logs.router)
app.include_router(bots.router)
app.include_router(tags.router)
app.include_router(terminal.router)
app.include_router(k8s_deploy.router)
app.include_router(monitor.router)
app.include_router(registry.router)
app.include_router(alerts.router)
app.include_router(custom_monitors.router)
app.include_router(ci_build.router)
app.include_router(users.router)
app.include_router(webhooks.router)


@app.exception_handler(AppException)
async def app_exception_handler(request, exc: AppException):
    body = {"success": False, "error": exc.message, "detail": exc.detail, "code": exc.status_code}
    if exc.error_key:
        body["error_key"] = exc.error_key
        if exc.error_params:
            body["error_params"] = exc.error_params
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """兜底:未预期的异常统一转成带 error_key 的 500,不回显类型与堆栈."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc
    logging.getLogger(__name__).error("Unhandled exception: %s: %s", type(exc).__name__, exc, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "服务器内部错误", "error_key": "errors.internal_error", "code": 500},
    )


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/health")
def health():
    """轻量健康检查(用于 Docker / K8s liveness probe)"""
    return {"status": "ok"}


@app.get("/api/info")
def api_info():
    """公开信息:版本,数据库状态,运行时间等(无需认证)"""
    db_ok = False
    try:
        db = Database()
        with db.conn() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    uptime_seconds = int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds())

    return {
        "app": "Devops-Glue CD",
        "version": app.version,
        "status": "running",
        "db_type": settings.db_driver,
        "db_connected": db_ok,
        "uptime_seconds": uptime_seconds,
    }


# 公开状态诊断(无需认证):只暴露布尔 / 枚举 / 状态码;契约探活结果按短窗口缓存
_STATUS_TTL = 10.0
_status_lock = threading.Lock()
_status_cache: dict = {"at": 0.0, "data": None}


def _probe_contract() -> tuple[int | None, bool | None, str | None]:
    """直接探活 Glue 契约端点(不经 ci_client 的 401 重登逻辑),返回 (status_code, enabled, provision).

    用 requests 直接请求并保留原始状态码,区分"token 失效"与"网络不可达";服务名固定 cd.
    """
    url = settings.ci_api_url.rstrip("/") + "/api/integration/services/cd"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {settings.ci_api_token}"}, timeout=5.0)
    except requests.RequestException:
        return None, None, None  # 连接失败 / 超时:不可达
    if resp.status_code != 200:
        return resp.status_code, None, None
    enabled: bool | None = None
    provision: str | None = None
    try:
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if isinstance(data, dict):
            enabled = str(data.get("enabled", "")).strip().lower() not in ("", "0", "false", "none")
            p = str(data.get("provision") or "").strip().lower()
            provision = "manual" if p == "manual" else "auto"
    except Exception:
        pass  # 响应不是预期 JSON:enabled / provision 保持 None,按无法判定处理
    return resp.status_code, enabled, provision


def _status_detail() -> dict:
    """汇总 DB / OIDC / CI 契约 / 表清单四类事实,派生统一 status 与 status_detail(按修复优先级取第一处)."""
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache["data"]
    if cached is not None and now - _status_cache["at"] < _STATUS_TTL:
        return cached

    db = None
    db_ok = False
    try:
        db = Database()
        db_ok = db.probe()
    except Exception:
        db_ok = False

    oidc_missing = settings.oidc_missing_settings()
    oidc_ok = not oidc_missing

    url_ok = bool(settings.ci_api_url)
    token_ok = bool(settings.ci_api_token)

    contract_status = None
    contract_enabled = None
    contract_provision = None
    if url_ok and token_ok:
        contract_status, contract_enabled, contract_provision = _probe_contract()
    contract_reachable = contract_status == 200

    tables_deployed = None
    tables_missing: list[str] = []
    if contract_reachable and contract_enabled and db is not None:
        try:
            tables_missing = db.cd_missing_tables()
            tables_deployed = not tables_missing
        except Exception:
            tables_deployed = None

    if not db_ok:
        status, detail = "db_error", "db"
    elif not oidc_ok:
        status, detail = "misconfigured", "oidc"
    elif not url_ok:
        status, detail = "misconfigured", "api_url"
    elif not token_ok:
        status, detail = "misconfigured", "api_token"
    elif not contract_reachable:
        if contract_status in (401, 403):
            status, detail = "misconfigured", "token_invalid"
        elif contract_status in (404, 405):
            status, detail = "unreachable", "glue_old"
        else:
            status, detail = "unreachable", "unreachable"
    elif contract_enabled is None:
        # 契约响应 200 但 enabled 缺失 / 不可解析:按无法判定处理
        status, detail = "unreachable", "unreachable"
    elif not contract_enabled:
        status, detail = "stopped", "not_enabled"
    elif tables_deployed is False:
        status, detail = "not_deployed", "not_deployed"
    else:
        status, detail = "running", "ok"

    data = {
        "app": "Devops-Glue CD",
        "version": app.version,
        "status": status,
        "status_detail": detail,
        "db_connected": db_ok,
        "oidc_configured": oidc_ok,
        "oidc_missing": oidc_missing,
        "ci_api_url_configured": url_ok,
        "ci_api_token_configured": token_ok,
        "contract": {
            "reachable": contract_reachable,
            "enabled": contract_enabled,
            "provision": contract_provision,
            "http_status": contract_status,
        },
        "tables_deployed": tables_deployed,
        "tables_missing": tables_missing,
    }
    with _status_lock:
        _status_cache["at"] = now
        _status_cache["data"] = data
    return data


@app.get("/api/status")
def api_status():
    """落地页"运行中"徽标的实时来源(无需认证):只返回非敏感布尔 / 枚举 / 状态码."""
    return _status_detail()


def _escape_label(value: str) -> str:
    """转义 Prometheus label 值中的反斜杠 / 引号 / 换行."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.get("/metrics")
def metrics():
    """Prometheus 文本指标:进程运行时长 + 版本 + 各状态部署计数 + 进行中部署数.

    仅暴露聚合计数(无项目名 / 用户等敏感维度);DB 不可用时降级为只返回进程级指标.
    """
    lines = [
        "# HELP cd_uptime_seconds Service process uptime in seconds.",
        "# TYPE cd_uptime_seconds gauge",
        f"cd_uptime_seconds {int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds())}",
        "# HELP cd_info Service version information.",
        "# TYPE cd_info gauge",
        f'cd_info{{version="{_escape_label(app.version)}"}} 1',
    ]
    try:
        db = Database()
        with db.conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS cnt FROM cd_deploy_logs GROUP BY status").fetchall()
            active = 0
            for r in rows:
                raw = r["status"] or "unknown"
                cnt = int(r["cnt"] or 0)
                if raw == "running":
                    active = cnt
                lines.append(f'cd_deploys_total{{status="{_escape_label(raw)}"}} {cnt}')
            lines.append("# HELP cd_active_deploys Currently running deploys.")
            lines.append("# TYPE cd_active_deploys gauge")
            lines.append(f"cd_active_deploys {active}")
    except Exception:
        pass
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# SPA 兜底路由,承接所有 Vue History 路由;HTML 不缓存
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
def vue_spa(full_path: str):
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
