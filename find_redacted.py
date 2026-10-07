import sqlite3, json

c = sqlite3.connect("orchestrator.db")

def walk(obj, path=""):
    """Yield (path, value) for every leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj

for table, col in [("step_attempts", "output_data"), ("step_runs", "output_data")]:
    print(f"\n=== {table}.{col} ===")
    for rid, raw in c.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL"):
        if raw is None or "redacted" not in str(raw):
            continue
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        for path, val in walk(obj):
            if isinstance(val, str) and "redacted" in val.lower():
                print(f"  row id={rid}  path={path}  value={val!r}")
