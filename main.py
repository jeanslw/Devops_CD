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
from app.routers import auth, projects, servers, deploy, logs, bots, terminal, k8s_deploy

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
app.include_router(terminal.router)
app.include_router(k8s_deploy.router)

# 静态文件
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── Dashboard ──
@app.get("/", response_class=HTMLResponse)
def home():
    template = BASE_DIR / "templates" / "index.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>首页文件丢失</h1>")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    template = BASE_DIR / "templates" / "dashboard.html"
    if template.exists():
        return HTMLResponse(template.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>模板文件丢失</h1>")


# ── 健康检查 ──
@app.get("/health")
def health():
    db_path = Path(settings.db_path) if settings.db_path else None
    return {
        "status": "ok",
        "version": "0.2.0",
        "db": str(db_path) if db_path else "",
        "db_exists": db_path.exists() if db_path else False,
    }


# ── 启动 ──
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
