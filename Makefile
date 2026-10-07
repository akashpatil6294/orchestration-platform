.PHONY: test test-backend test-frontend lint typecheck build migrate dev dev-lan prod-check worker serve

test: test-backend test-frontend

test-backend:
	python -m pytest -q tests/

test-frontend:
	cd frontend && npm test

lint:
	cd frontend && npm run lint

typecheck:
	cd frontend && npx tsc -b

build:
	cd frontend && npm run build

migrate:
	python -m alembic upgrade head

dev:
	python -m app.cli dev

# Production hardening targets (Prevention 3).
dev-lan:
	python -m alembic upgrade head
	python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 & cd frontend && npm run dev -- --host 0.0.0.0 --port 5173

prod-check:
	python -m app.cli doctor --strict

worker:
	python scripts/run_worker.py

serve:
	caddy run --config Caddyfile
