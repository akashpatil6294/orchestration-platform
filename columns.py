import sqlite3
c = sqlite3.connect("orchestrator.db")
for table in ("step_attempts", "step_runs"):
    print(f"--- {table} ---")
    try:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        print(cols)
    except Exception as e:
        print("error:", e)
