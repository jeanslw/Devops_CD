"""测试辅助:模拟 Devops-Glue 契约端点的答复 -- 等价于运维在 CI 后台点'集成服务 → 启用'.

CD 读契约只有一条通道:Glue 的窄只读端点(backend/database.py::_fetch_contract_http).
单测不打真 HTTP(开发机的根 .env 往往已配 CI_API_URL / CI_API_TOKEN,真连会把单测变成
环境依赖):本模块把 _fetch_contract_http 替换成进程内替身,状态由 set_cd_contract /
enable_cd 摆布 -- 语义与 Glue 端点一致(给出 enabled/provision 答复,或不给答复=未启用).
替身不读任何库:CD 生产也不读表,契约表只是 CI 侧的事实记录.

语义(CI 是契约的唯一写入方):
  - 端点无答复(替身未注册)或 enabled=0 → 零 DDL + 入口统一 503 errors.cd_not_enabled;
  - enabled=1 + provision='auto'        → CD 自动建表(MySQL / SQLite 同理);
  - enabled=1 + provision='manual'      → CD 零 DDL,只认运维预建表.
想测'未启用 / manual'时只要不调用本辅助或改用 provision='manual';每个用例开始前
conftest.py 会把替身复位为'未启用',防止上一用例的状态泄漏.

被 test_cd_deploy_gate.py / test_cd_schema_sources.py 之外的既有测试共用(审批,会话,
镜像同步),因此单独成模块,避免每个文件各写一份契约替身.
"""

import sqlite3

from backend.config import settings
from backend.database import CD_SERVICE_NAME, PROVISION_AUTO, CdIntegrationState
import backend.database as dbmod

# 单测不走契约缓存窗口:场景切换(set_cd_contract)必须对下一个闸门读立即生效,
# 与旧直读路径"每请求一次单行查询"的即时性对齐. HTTP 窗口缓存由 test_cd_contract_http.py 专测.
settings.ci_contract_ttl = 0

# 与 Devops-Glue v2.9.0 的 sqlite_init.sql 同构(测试只需服务 name/enabled/provision 三列可用)
CONTRACT_DDL = (
    "CREATE TABLE IF NOT EXISTS ci_integrated_services ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "service_name TEXT NOT NULL UNIQUE, "
    "enabled INTEGER NOT NULL DEFAULT 0, "
    "provision TEXT NOT NULL DEFAULT 'auto', "
    "api_token_id INTEGER DEFAULT NULL, "
    "updated_by TEXT DEFAULT '', "
    "created_at TEXT DEFAULT '', "
    "updated_at TEXT DEFAULT '')"
)

# 替身状态:{service_name: 端点答复}. 未注册 = 端点对该服务给出"未启用"(Glue 对缺失契约行
# 的正常化答复),不是"端点不可用"(后者由 test_cd_contract_http.py 用假 client 模拟).
_contract_answers: dict[str, CdIntegrationState] = {}


def _stub_fetch_contract_http(service: str = CD_SERVICE_NAME) -> CdIntegrationState:
    """_fetch_contract_http 的单测替身:返回 set_cd_contract 注册的答复."""
    return _contract_answers.get(service, CdIntegrationState(False, PROVISION_AUTO, False))


# 真 _fetch_contract_http 的原函数引用:test_cd_contract_http.py 在用例内换回真实现,
# 让"端点不可达 fail-closed / 负缓存"逻辑真实跑一遍(cd_contract 的替身只负责模拟 200 答复).
ORIGINAL_FETCH_CONTRACT_HTTP = dbmod._fetch_contract_http

dbmod._fetch_contract_http = _stub_fetch_contract_http


def reset_contract_stub() -> None:
    """复位替身到'未启用'(conftest 的 autouse 夹具在每个用例前调用)."""
    _contract_answers.clear()


def set_cd_contract(
    db_path: str, enabled=1, provision: str = "auto", with_row: bool = True, create_table: bool = True
) -> None:
    """按需构造契约状态(未启用 / enabled=0 / manual 等),供闸门测试直接摆场景.

    仍在临时库写入契约行(表名随 Glue v2.9.0 收口为 ci_integrated_services):让假库与
    Glue 侧事实一致,便于 schema/视图类断言;替身状态以本函数的参数为准.
    """
    conn = sqlite3.connect(db_path)
    try:
        if create_table:
            conn.execute(CONTRACT_DDL)
        if with_row:
            conn.execute(
                "INSERT OR REPLACE INTO ci_integrated_services (service_name, enabled, provision) VALUES ('cd', ?, ?)",
                (enabled, provision),
            )
            _contract_answers[CD_SERVICE_NAME] = CdIntegrationState(
                bool(enabled), provision if enabled else PROVISION_AUTO, True
            )
        else:
            conn.execute("DELETE FROM ci_integrated_services")  # 清空 = 回到"契约行不存在"
            _contract_answers.pop(CD_SERVICE_NAME, None)
        conn.commit()
    finally:
        conn.close()


def enable_cd(db_path: str, provision: str = "auto") -> None:
    """写入契约行 service_name='cd' + enabled=1(幂等,可重复调用)."""
    set_cd_contract(db_path, enabled=1, provision=provision)
