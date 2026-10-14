"""backend/tests 公共夹具."""

from __future__ import annotations

import pytest

from backend.database import invalidate_contract_cache

from cd_contract import reset_contract_stub


@pytest.fixture(autouse=True)
def _fresh_contract_stub():
    """每个用例前把契约替身复位为'未启用'并清空契约缓存.

    替身(backend/tests/cd_contract.py)是进程级状态,而各用例的临时库互不相干:
    生产语义里"临时库没有契约行 = 未启用",替身必须同样以未启用为默认值,
    否则前一个用例的启用状态会泄漏进"从未调用 enable_cd"的用例.
    """
    reset_contract_stub()
    invalidate_contract_cache()
    yield
    reset_contract_stub()
