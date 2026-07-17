# Salon Daily Sales API

FastAPI backend for the multi-branch beauty salon daily sales system. The API keeps employee daily sales separate from the branch cash/bank settlement and derives completion and reconciliation from those records.

## Local Run

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run alembic upgrade head
uv run python -m app.cli.seed
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

Local development uses SQLite. Production must set a MySQL `8.0.16+` connection URL and secure cookie settings.

- API docs: `http://127.0.0.1:8010/api/v1/docs`
- Health: `http://127.0.0.1:8010/api/v1/health`

## Checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q
```

The container listens on port `8000`; local port `8010` is only a workstation convention.
