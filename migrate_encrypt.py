"""一次性迁移脚本：将 cd_servers 表中所有明文 password / ssh_key 加密。

用法：
    1. 确保 .env 中已正确配置数据库连接和 SECRET_KEY
    2. python migrate_encrypt.py
    3. 验证无误后删除此脚本
"""

import sys
import os

# 确保能 import app 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import Database
from app.crypto import encrypt

db = Database()

print(f"[INFO] 连接数据库: driver={db._driver}")
conn = db.conn()

# 查询所有服务器
rows = conn.execute("SELECT id, name, password, ssh_key FROM cd_servers").fetchall()

print(f"[INFO] 共 {len(rows)} 条记录\n")

encrypted_count = 0
for r in rows:
    sid, name, pwd, key = r["id"], r["name"], r["password"] or "", r["ssh_key"] or ""

    new_pwd = encrypt(pwd)
    new_key = encrypt(key)

    if new_pwd == pwd and new_key == key:
        print(f"  [SKIP] id={sid} '{name}' — 已加密或无敏感数据")
        continue

    conn.execute(
        "UPDATE cd_servers SET password=?, ssh_key=? WHERE id=?",
        (new_pwd, new_key, sid),
    )
    print(f"  [ OK ] id={sid} '{name}' — 已加密")
    encrypted_count += 1

conn.commit()
conn.close()

print(f"\n[DONE] 已加密 {encrypted_count} 条记录。")
print(f"[INFO] 请重启 cd_service 服务使变更生效。")
print(f"[INFO] 验证无误后请删除本脚本 migrate_encrypt.py")
