# Tax planner development

Use uv with the Python version in `.python-version`:

```bash
cd extras/tax
uv sync --locked
uv run --no-sync python -m unittest discover -v
# Serve the API locally (Docker also supplies Alpine.js for the complete UI).
uv run --no-sync uvicorn app:app --host 127.0.0.1 --port 8088
```

This is an application project; its modules run directly from this directory.
The tests use Python's standard library unittest runner and synthetic inputs.
The ledger query tests in `test_app_ledger.py` skip unless
`TAX_TEST_DATABASE_URL` points at a disposable Postgres; they create only TEMP
tables, for example:

```bash
docker run -d --rm --name tax-test-pg -e POSTGRES_PASSWORD=t -p 127.0.0.1:55432:5432 postgres:16
TAX_TEST_DATABASE_URL=postgresql://postgres:t@127.0.0.1:55432/postgres \
  uv run --no-sync python -m unittest discover -v
```
Docker bundles Alpine.js and installs the same locked dependencies into `/opt/venv`.

After changing dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`.
To review available dependency upgrades, run `uv lock --upgrade`, then rerun the tests.
