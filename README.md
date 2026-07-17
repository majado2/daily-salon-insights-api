# Salon Daily Sales API

FastAPI backend for the multi-branch beauty salon daily sales system. The API keeps employee daily sales separate from the branch cash/bank settlement and derives completion and reconciliation from those records.

## Local Run

```powershell
uv sync --all-groups
Copy-Item .env.example .env
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

To add disposable demo records during local development only, run
`uv run python -m app.cli.seed`. Production never runs the seed command; the
container applies Alembic migrations and starts with an empty business database.

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

## Deployment

Pushes to `main` run all checks and deploy to the CapRover app
`salon-sales-api`. The repository must define the `CAPROVER_PASSWORD` Actions
secret. Runtime database and JWT secrets remain in CapRover environment
variables and are never stored in GitHub.
