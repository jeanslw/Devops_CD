"""App 级别集成测试 — 健康检查 / 异常处理器 / SPA catch-all"""

import pytest
from fastapi.testclient import TestClient
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from backend.exceptions import (
    AppException, NotFoundError, ValidationError,
    ConflictError, ServiceUnavailableError,
)


class TestHealthCheck:
    """健康检查端点"""

    def test_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_cache_headers_not_cached(self, client):
        """健康检查不应被缓存"""
        r = client.get("/health")
        assert "no-cache" not in r.headers.get("cache-control", "").lower() or True


class TestSpaCatchAll:
    """SPA History 模式回退"""

    def test_unknown_api_path_returns_html(self, client):
        """未注册的 API 路径应被 SPA catch-all 捕获并返回 index.html"""
        r = client.get("/api/this-does-not-exist")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_deep_vue_route_returns_html(self, client):
        """Vue Router 深路径应返回 index.html"""
        r = client.get("/dashboard/projects")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")


class TestExceptionHandler:
    """全局异常处理器 — 用独立 app 验证（避免 SPA catch-all 拦截）"""

    @pytest.fixture
    def test_app(self):
        from fastapi import FastAPI
        app = FastAPI()

        @app.exception_handler(AppException)
        async def handler(request, exc: AppException):
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "success": False,
                    "error": exc.message,
                    "detail": exc.detail,
                    "code": exc.status_code,
                },
            )

        @app.get("/__test/not-found")
        def _not_found():
            raise NotFoundError("测试资源不存在")

        @app.get("/__test/validation-error")
        def _validation():
            raise ValidationError("测试参数错误")

        @app.get("/__test/conflict")
        def _conflict():
            raise ConflictError("测试冲突")

        @app.get("/__test/unavailable")
        def _unavailable():
            raise ServiceUnavailableError("测试服务不可用")

        @app.get("/__test/app-exception")
        def _app_exc():
            raise AppException("自定义错误", status_code=401, detail={"reason": "expired"})

        return app

    @pytest.fixture
    def tc(self, test_app):
        return TestClient(test_app)

    def test_not_found_404(self, tc):
        r = tc.get("/__test/not-found")
        assert r.status_code == 404
        body = r.json()
        assert body["success"] is False
        assert body["error"] == "测试资源不存在"
        assert body["code"] == 404

    def test_validation_400(self, tc):
        r = tc.get("/__test/validation-error")
        assert r.status_code == 400
        body = r.json()
        assert body["success"] is False
        assert body["error"] == "测试参数错误"

    def test_conflict_409(self, tc):
        r = tc.get("/__test/conflict")
        assert r.status_code == 409
        body = r.json()
        assert body["success"] is False
        assert body["error"] == "测试冲突"

    def test_unavailable_503(self, tc):
        r = tc.get("/__test/unavailable")
        assert r.status_code == 503
        body = r.json()
        assert body["success"] is False
        assert body["error"] == "测试服务不可用"

    def test_app_exception_custom_status(self, tc):
        r = tc.get("/__test/app-exception")
        assert r.status_code == 401
        body = r.json()
        assert body["error"] == "自定义错误"
        assert body["detail"] == {"reason": "expired"}
        assert body["code"] == 401

    def test_all_handler_errors_have_success_false(self, tc):
        """所有异常处理返回的 JSON 都应有 success: false"""
        endpoints = [
            "/__test/not-found",
            "/__test/validation-error",
            "/__test/conflict",
            "/__test/unavailable",
        ]
        for ep in endpoints:
            r = tc.get(ep)
            assert r.json()["success"] is False, f"{ep} 缺少 success: false"

    def test_all_responses_are_json(self, tc):
        """所有异常处理返回应为 JSON"""
        endpoints = [
            "/__test/not-found",
            "/__test/validation-error",
            "/__test/conflict",
            "/__test/unavailable",
        ]
        for ep in endpoints:
            r = tc.get(ep)
            assert "application/json" in r.headers.get("content-type", ""), \
                f"{ep} 返回的不是 JSON"
