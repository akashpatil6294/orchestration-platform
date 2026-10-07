import sqlite3
c = sqlite3.connect('orchestrator.db')
rows = c.execute("""
    select id, run_id, step_key, usage
    from step_attempts
    where usage like '%redacted%'
""").fetchall()
for r in rows:
    print(r)
