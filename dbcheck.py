import sqlite3, os
path = "orchestrator.db"
print("db:", os.path.abspath(path), "exists:", os.path.exists(path), "size:", os.path.getsize(path) if os.path.exists(path) else 0)
c = sqlite3.connect(path)
tables = sorted(r[0] for r in c.execute("select name from sqlite_master where type='table'"))
print("tables:", tables)
