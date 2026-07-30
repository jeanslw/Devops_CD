"""
Devops-Glue CD Service
FastAPI 部署执行器 — SSH / docker-compose / K8s

架构: main.py(入口) → routers → services → deployers
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import settings
from backend.database import Database
from backend.exceptions import AppException
from backend.routers import auth, projects, servers, deploy, logs, bots, tags, terminal, k8s_deploy, monitor, registry, alerts, custom_monitors, ci_build, users
from backend.services.registry_service import start_background_sync, RegistryService
from backend.services.alert_service import start_alert_checker

# ── 创建 app ──
app = FastAPI(title="Devops-Glue CD", version="1.2.1")
BASE_DIR = Path(__file__).parent

# ── 启动事件 ──
@app.on_event("startup")
def on_startup():
    """启动后台定时同步（DB 已存间隔优先，否则读环境变量）"""
    db = Database()
    try:
        svc = RegistryService(db)
        interval = svc.get_sync_interval()
    except Exception:
        interval = -1
    start_background_sync(lambda: Database(), interval)
    start_alert_checker()

# 注册路由
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(servers.router)
app.include_router(deploy.router)
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

# ── 异常处理器 ──
@app.exception_handler(AppException)
async def app_exception_handler(request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.message, "detail": exc.detail, "code": exc.status_code},
    )

# 静态文件
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")





# ── 健康检查 ──
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": app.version,
    }


# ── SPA 路由 ──
# 必须放在 API、静态文件和健康检查之后，统一承接所有 Vue History 路由。
# HTML 不缓存：每次请求重读磁盘，避免构建后 HTML/资源 hash 不一致。
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
def vue_spa(full_path: str):
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


# ── 启动 ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
