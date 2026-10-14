"""CD 建表来源的'多方同构'契约:清单 ↔ init_mysql.sql ↔ Python MySQL DDL ↔ SQLite 实建表.

为什么需要这份测试:CD 表结构现在有四处来源,任何一处漂移都会让'启用 CD'的两种模式分叉:
  1. CD_REQUIRED_TABLES                     -- 闸门清单(CD 有哪些表)
  2. database/init_mysql.sql                -- **manual** 模式:管理员导入(运维眼中的结构真相)
  3. backend/database.py::_MYSQL_TABLE_DDL  -- **auto** 模式:MySQL 自动建表
     + _MYSQL_COLUMN_MIGRATIONS / _MYSQL_INDEXES(补列,补索引)
  4. backend/database.py::_ensure_cd_tables -- **auto** 模式:SQLite 自动建表
     (运行期自检只保证'表齐',不保证列齐,所以列集合必须在这里锁死)
本测试把四处拉到同一张桌子上逐表,逐列,逐索引比对,并保证 CD 不写共享表.

运行(项目根):
    python -m pytest backend/tests/test_cd_schema_sources.py -q
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings

settings.db_driver = "sqlite"
# 契约读走 cd_contract.py 的进程内替身(模拟 Glue 端点答复):建表闸门不依赖本机是否配了
# CI_API_URL,也不读共享库 -- CD 生产同样只经契约端点读启用状态(见 backend/database.py 顶部注释)

from cd_contract import enable_cd  # noqa: E402

from backend.database import (  # noqa: E402
    CD_REQUIRED_TABLES,
    Database,
    _MYSQL_COLUMN_MIGRATIONS,
    _MYSQL_INDEXES,
    _MYSQL_TABLE_DDL,
)

ROOT = Path(__file__).resolve().parents[2]
MYSQL_SCRIPT = ROOT / "database" / "init_mysql.sql"

# 表体里以这些词开头的行是约束/索引,不是列定义
_NON_COLUMN_KEYWORDS = {"PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN", "FULLTEXT", "CHECK", "SPATIAL"}
_INLINE_INDEX_RE = re.compile(r"(UNIQUE\s+)?(?:KEY|INDEX)\s+(\w+)\s*\(([^)]*)\)", re.I)


def _clean_line(raw: str) -> str:
    """去掉行内 `--` 注释与行尾逗号(SQL 与 Python DDL 文本共用)."""
    return re.sub(r"--.*$", "", raw).rstrip(",").strip()


def _split_table_body(create_sql: str) -> tuple[str, str]:
    """把 CREATE TABLE 文本拆成 (表名, 表体).表体以 4 空格缩进的闭括号行结束."""
    name = create_sql.split("CREATE TABLE IF NOT EXISTS", 1)[1].strip().split("(")[0].strip()
    body = create_sql.split("(", 1)[1]
    return name, body[: body.rindex("\n    )")]


def _parse_body(body: str):
    """解析表体 → (列名清单, {索引名: (列清单, 是否唯一)})."""
    columns: list[str] = []
    inline: dict[str, tuple[str, bool]] = {}
    for raw in body.splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        m = _INLINE_INDEX_RE.match(line)
        if m:
            inline[m.group(2)] = (" ".join(m.group(3).split()), bool(m.group(1)))
            continue
        if line.split()[0].upper() in _NON_COLUMN_KEYWORDS:
            continue
        columns.append(line.split()[0])
    return columns, inline


def _parse_mysql_script(text: str):
    """解析 database/init_mysql.sql → (表→列, 外挂索引, 内联索引).

    外挂索引 = CALL __add_index / __add_unique_index(CD 表部分);内联索引 = CREATE TABLE 表体内的
    INDEX / UNIQUE KEY.列集合已并入 CALL __add_column 的迁移列(即'导入脚本后的最终结构').
    """
    tables: dict[str, list[str]] = {}
    inline_indexes: dict[str, tuple[str, str, bool]] = {}
    external_indexes: dict[str, tuple[str, str, bool]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(cd_\w+)\s*\((.*?)\n\)\s*ENGINE", text, re.S):
        table, body = m.group(1), m.group(2)
        columns, inline = _parse_body(body)
        tables[table] = columns
        for name, (cols, unique) in inline.items():
            inline_indexes[name] = (table, cols, unique)
    for m in re.finditer(r"CALL __add_column\('(\w+)',\s*'(\w+)'", text):
        cols = tables.setdefault(m.group(1), [])
        if m.group(2) not in cols:
            cols.append(m.group(2))
    for m in re.finditer(r"CALL __add_(unique_)?index\('(cd_\w+)',\s*'(\w+)',\s*'([^']*)'", text):
        external_indexes[m.group(3)] = (m.group(2), " ".join(m.group(4).split()), bool(m.group(1)))
    return tables, external_indexes, inline_indexes


def _parse_python_mysql_ddl():
    """解析 _MYSQL_TABLE_DDL(auto 模式 MySQL 建表)→ (表→列, 内联索引)."""
    tables: dict[str, list[str]] = {}
    inline_indexes: dict[str, tuple[str, str, bool]] = {}
    for ddl in _MYSQL_TABLE_DDL.values():
        table, body = _split_table_body(ddl)
        columns, inline = _parse_body(body)
        tables[table] = columns
        for name, (cols, unique) in inline.items():
            inline_indexes[name] = (table, cols, unique)
    return tables, inline_indexes


def _python_mysql_final_columns(table: str, python_tables: dict[str, list[str]]) -> set[str]:
    """auto 模式 MySQL 的最终列集合 = 建表列 ∪ 缺列迁移(与 SQLite 侧的迁移清单对齐)."""
    cols = set(python_tables[table])
    cols |= {c for t, c, _def in _MYSQL_COLUMN_MIGRATIONS if t == table}
    return cols


def _sqlite_created_columns(path: str) -> dict[str, list[str]]:
    """真跑一遍 SQLite 自动建表(契约端点答复 enabled=1 + auto),再反射每张表的列."""
    enable_cd(path)  # 契约替身:Glue 端点答复 enabled=1 + auto(不读共享库)
    Database._tables_ensured = False
    try:
        db = Database(path)
        with db.conn():
            pass
        con = sqlite3.connect(path)
        try:
            return {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in CD_REQUIRED_TABLES}
        finally:
            con.close()
    finally:
        Database._tables_ensured = False


class TableSetContractTestCase(unittest.TestCase):
    """表集合:清单 / 脚本 / Python DDL 三处必须逐张相等(多一张少一张都算漂移)."""

    def test_required_list_is_all_cd_owned(self):
        self.assertFalse([t for t in CD_REQUIRED_TABLES if not t.startswith("cd_")])

    def test_python_mysql_ddl_covers_inventory(self):
        self.assertEqual(set(_MYSQL_TABLE_DDL), set(CD_REQUIRED_TABLES))

    def test_mysql_script_covers_inventory(self):
        tables, _ext, _inline = _parse_mysql_script(MYSQL_SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(set(tables), set(CD_REQUIRED_TABLES))

    def test_python_mysql_ddl_only_creates_cd_owned_tables(self):
        """auto 模式自动建表只许出现 cd_*:共享表(ci_* / admin_users / roles)归 Devops-Glue."""
        for ddl in _MYSQL_TABLE_DDL.values():
            name, _body = _split_table_body(ddl)
            self.assertTrue(name.startswith("cd_"), name)

    def test_migrations_only_reference_inventory(self):
        for table, _col, _def in _MYSQL_COLUMN_MIGRATIONS:
            self.assertIn(table, CD_REQUIRED_TABLES)


class ColumnContractTestCase(unittest.TestCase):
    """列集合:SQLite 实建表 == init_mysql.sql(含 __add_column)== Python MySQL DDL(含迁移清单)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sqlite_cols = _sqlite_created_columns(os.path.join(cls._tmp.name, "schema_sources.db"))
        cls.script_tables, cls.script_ext_indexes, cls.script_inline_indexes = _parse_mysql_script(
            MYSQL_SCRIPT.read_text(encoding="utf-8")
        )
        cls.python_tables, cls.python_inline_indexes = _parse_python_mysql_ddl()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_sqlite_columns_match_mysql_script(self):
        for table in CD_REQUIRED_TABLES:
            self.assertEqual(
                set(self.sqlite_cols[table]),
                set(self.script_tables[table]),
                f"{table}：SQLite 自动建表列集合与 database/init_mysql.sql 漂移",
            )

    def test_python_mysql_ddl_matches_mysql_script(self):
        for table in CD_REQUIRED_TABLES:
            self.assertEqual(
                _python_mysql_final_columns(table, self.python_tables),
                set(self.script_tables[table]),
                f"{table}：_MYSQL_TABLE_DDL/_MYSQL_COLUMN_MIGRATIONS 与 database/init_mysql.sql 漂移",
            )

    def test_sqlite_columns_match_python_mysql_ddl(self):
        for table in CD_REQUIRED_TABLES:
            self.assertEqual(
                set(self.sqlite_cols[table]),
                _python_mysql_final_columns(table, self.python_tables),
                f"{table}：auto 模式两个驱动（SQLite / MySQL）的列集合不一致",
            )


class IndexContractTestCase(unittest.TestCase):
    """索引:auto 模式建的 == manual 脚本建的(两个模式的库结构 / 性能不许分叉)."""

    @classmethod
    def setUpClass(cls):
        cls.script_tables, cls.script_ext_indexes, cls.script_inline_indexes = _parse_mysql_script(
            MYSQL_SCRIPT.read_text(encoding="utf-8")
        )
        cls.python_tables, cls.python_inline_indexes = _parse_python_mysql_ddl()

    def test_external_indexes_match_mysql_script(self):
        """外挂索引(脚本里的 CALL __add_index / __add_unique_index,仅 CD 表)== _MYSQL_INDEXES."""
        python_indexes = {name: (table, " ".join(cols.split()), unique) for name, table, cols, unique in _MYSQL_INDEXES}
        self.assertEqual(python_indexes, self.script_ext_indexes)

    def test_inline_indexes_match_mysql_script(self):
        """表体内联索引(INDEX / UNIQUE KEY)两边必须同名同列."""
        self.assertEqual(self.python_inline_indexes, self.script_inline_indexes)

    def test_indexes_only_on_cd_tables(self):
        for _name, table, _cols, _unique in _MYSQL_INDEXES:
            self.assertIn(table, CD_REQUIRED_TABLES)
        for name, (table, _cols, _unique) in self.python_inline_indexes.items():
            self.assertTrue(table.startswith("cd_"), name)

    def test_mysql_script_never_bootstraps_shared_tables(self):
        """manual 脚本不许对共享表(ci_* / admin_users / roles ...)建索引 —— 那些归 Devops-Glue."""
        text = MYSQL_SCRIPT.read_text(encoding="utf-8")
        for m in re.finditer(r"CALL __add_(?:unique_)?index\('([^']+)'", text):
            self.assertTrue(m.group(1).startswith("cd_"), f"init_mysql.sql 对共享表建索引：{m.group(0)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
