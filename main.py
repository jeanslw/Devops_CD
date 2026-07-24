"""
Devops-Glue CD Service
FastAPI 部署执行器 — SSH / docker-compose / K8s

架构: main.py(入口) → routers → services → deployers
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import auth, projects, servers, deploy, logs, bots, tags, terminal, k8s_deploy, monitor

# ── 创建 app ──
app = FastAPI(title="Devops-Glue CD", version="0.2.0")
BASE_DIR = Path(__file__).parent

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

# 静态文件
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── 模板预加载（启动时读一次，避免每次请求读磁盘）──
_INDEX_HTML = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
_DASHBOARD_HTML = (BASE_DIR / "templates" / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(_INDEX_HTML)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_DASHBOARD_HTML)


# ── 健康检查 ──
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.2.0",
    }


# ── 启动 ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
