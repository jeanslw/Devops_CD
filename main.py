"""
Devops-Glue CD Service
FastAPI 部署执行器 - SSH / docker-compose / K8s

架构: main.py(入口) → routers → services → deployers
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.access_log import install_access_log_filter
from backend.config import settings
from backend.database import Database, DatabaseUnavailableError, cd_deploy_hint, cd_not_enabled_hint
from backend.exceptions import AppException
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
    # 认证模式:唯一总开关(见 docs/SSO_OIDC_ZH.md §6).启动日志必打,便于切档后确认(观测点见 §10).
    logging.getLogger(__name__).info("认证模式：AUTH_MODE=%s", settings.auth_mode)
    if not settings.oidc_ready():
        # 缺配置不阻断启动,但必须提示缺少项(compose 的 env-check 会在启动前中止,两者同清单).
        logging.getLogger(__name__).error(
            "AUTH_MODE=oidc 缺少必填项：%s，SSO 登录会失败。OIDC_ISSUER 在 config/app.env 或根 .env 配置，"
            "CD_OIDC_SECRET / CD_OIDC_REDIRECT_URI 在根 .env 配置（见 docs/SSO_OIDC_ZH.md §6.1）",
            "、".join(settings.oidc_missing_settings()),
        )
    # 启用 / 部署状态提示(三态,不阻断启动):/health,/api/info 与 /metrics 仍可用,便于探活排障.
    #   1) 未启用集成服务 → 登录与所有接口被拒 503 errors.cd_not_enabled(去 CI 后台开开关);
    #   2) 已启用但表没建全 → 同样被拒 503 errors.cd_not_deployed(auto 重启自愈 / manual 需导入脚本);
    #   3) 正常 → 不打日志.
    # 应用只在"已启用 + provision=auto"时建表(见 database.Database.conn 的建表闸门),
    # 因此这里的 cd_integration_state() 顺带触发首次自动建表.
    try:
        state = db.cd_integration_state()
        if not state.enabled:
            logging.getLogger(__name__).warning(
                "Devops-CD 尚未启用（集成服务契约表 integrated_services 里没有 enabled=1 的 cd 行）："
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
    except DatabaseUnavailableError as e:
        # 库当前不可用(连不上/建不了表):请求返回 503 errors.db_unavailable -- 与"未部署"区分开,
        # 排查方向是连接/权限/磁盘,而不是重跑建表脚本.
        logging.getLogger(__name__).error(
            "数据库当前不可用（驱动 %s）：%s —— 请求将返回 503 errors.db_unavailable", db.driver, e.message
        )
    except Exception:  # 探测自身的故障不该拖垮启动
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

    # ── 优雅停机(uvicorn 收到 SIGTERM 后执行)──
    # 1. 先停后台线程(镜像同步 / 告警检查 / 定时发布调度器 / 部署心跳);
    # 2. 再取消本进程所有进行中的部署并等待其收尾(置位取消信号 → 部署线程在下一个
    #    检查点抛 DeployCancelled → 按 terminated 正常落库),远端不会停留在半执行状态
    #    (如 helm 半程 upgrade).K8s 场景建议 terminationGracePeriodSeconds ≥ 30.
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


# ── 创建 app ──
app = FastAPI(title="Devops-Glue CD", version=__version__, lifespan=lifespan)
BASE_DIR = Path(__file__).parent
_STARTED_AT = datetime.now(timezone.utc)

# 访问日志脱敏:SSO 回跳 URL 里的一次性 code / state(以及 WS 的 token 回退参数)不进日志.
# 在导入期安装:`uvicorn main:app`,`python main.py`,`--workers N` 三种启动方式都覆盖
# (uvicorn 的 dictConfig 会重建 handler/level,但不会清空 logger 上已注册的 filters).
install_access_log_filter()

# ── 安全响应头(所有响应统一附加)──
# CSP:只允许同源资源 + data: 图片(前端 CSS 有 SVG data URI)+ ws:/wss:(WebShell).
# 'unsafe-inline' 仅用于 style(前端大量内联 style= 属性),脚本仍禁内联.
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


# 注册路由
app.include_router(auth.router)
# OIDC 登录(AUTH_MODE=oidc 时启用;local 模式下两条路由自身返回 404)
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


# ── 异常处理器 ──
@app.exception_handler(AppException)
async def app_exception_handler(request, exc: AppException):
    body = {"success": False, "error": exc.message, "detail": exc.detail, "code": exc.status_code}
    if exc.error_key:
        body["error_key"] = exc.error_key
        if exc.error_params:
            body["error_params"] = exc.error_params
    return JSONResponse(status_code=exc.status_code, content=body)


# 静态文件
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── 健康检查 ──
@app.get("/health")
def health():
    """轻量健康检查(用于 Docker / K8s liveness probe)"""
    return {"status": "ok"}


# ── 公开信息接口 ──
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
        # 认证模式(前端登录页据此渲染 SSO 按钮 / 本地表单):只暴露模式名,
        # 不暴露 issuer / secret / redirect_uri(信息面见 docs/SSO_OIDC_ZH.md §10).
        "auth_mode": settings.auth_mode,
    }


# ── Prometheus 指标(文本格式,供 Prometheus / Grafana 抓取,无需认证)──
def _escape_label(value: str) -> str:
    """转义 Prometheus label 值中的反斜杠/引号/换行,防止破坏文本格式."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.get("/metrics")
def metrics():
    """Prometheus 文本指标:进程运行时长 + 版本 + 各状态部署计数 + 进行中部署数.

    仅暴露聚合计数(无项目名/用户等敏感维度);DB 不可用时降级为只返回进程级指标,
    不让指标抓取因 DB 抖动而拖垮或误报服务不健康.
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


# ── SPA 路由 ──
# 必须放在 API,静态文件和健康检查之后,统一承接所有 Vue History 路由.
# HTML 显式禁用缓存:每次重读磁盘 + 每次重新拉取,避免"构建后 index.html 仍指向已删除的
# 旧 hash 资源"导致白屏(资源本身带 hash,仍由 StaticFiles 正常缓存).
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


# ── 启动 ──
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
