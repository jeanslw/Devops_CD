"""统一响应格式单元测试 — 验证 ok/items/error 的输出结构"""

from backend.responses import ok, items, error


class TestOkResponse:
    """成功响应"""

    def test_empty(self):
        r = ok()
        assert r == {"success": True}

    def test_with_message(self):
        r = ok(message="操作成功")
        assert r == {"success": True, "message": "操作成功"}

    def test_with_data(self):
        r = ok(data={"id": 1, "name": "test"})
        assert r == {"success": True, "data": {"id": 1, "name": "test"}}

    def test_with_data_and_message(self):
        r = ok(data={"id": 1}, message="创建成功")
        assert r["success"] is True
        assert r["data"] == {"id": 1}
        assert r["message"] == "创建成功"

    def test_data_none_omitted(self):
        """data=None 时不应出现在输出中"""
        r = ok(data=None, message="ok")
        assert "data" not in r
        assert r["message"] == "ok"


class TestItemsResponse:
    """分页列表响应"""

    def test_basic(self):
        r = items([{"id": 1}, {"id": 2}], total=2)
        assert r["success"] is True
        assert r["items"] == [{"id": 1}, {"id": 2}]
        assert r["total"] == 2
        assert r["page"] == 1
        assert r["page_size"] == 20
        assert r["total_pages"] == 1

    def test_pagination_calc(self):
        r = items([], total=55, page=2, page_size=20)
        assert r["total"] == 55
        assert r["page"] == 2
        assert r["total_pages"] == 3  # ceil(55/20)

    def test_empty_list(self):
        r = items([], total=0)
        assert r["items"] == []
        assert r["total"] == 0
        assert r["total_pages"] == 1  # 至少 1 页

    def test_single_page(self):
        r = items([{"id": 1}], total=1, page_size=50)
        assert r["total_pages"] == 1


class TestErrorResponse:
    """错误响应"""

    def test_default(self):
        r = error("参数错误")
        assert r == {"success": False, "error": "参数错误", "code": 400}

    def test_custom_code(self):
        r = error("未找到", code=404)
        assert r == {"success": False, "error": "未找到", "code": 404}

    def test_long_message(self):
        r = error("这个一个非常长的错误消息" * 10, code=500)
        assert r["success"] is False
        assert r["code"] == 500
