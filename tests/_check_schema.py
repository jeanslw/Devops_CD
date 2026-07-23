import sqlite3
conn = sqlite3.connect("database/cd_service.db")
cursor = conn.execute("SELECT sql FROM sqlite_master WHERE name='ci_job_git_map'")
row = cursor.fetchone()
if row:
    print(row[0])
else:
    print("Table not found")
conn.close()
