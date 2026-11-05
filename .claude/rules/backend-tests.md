---
paths:
  - "backend/tests/**"
  - "backend/pyproject.toml"
---

# Backend tests

- **Pytest is strict.** `[tool.pytest.ini_options]` sets `strict = true` and `asyncio_mode = "auto"`, so async tests need no marker and marker typos fail. CI adds `-W error`: a new warning fails the run, so run `-W error` locally too.
- **Default database is SQLite.** `tests/conftest.py` swaps pgvector's `Vector` for a JSON type and stores UUIDs as text. Its `cosine_distance` is a stub, so similarity ordering is not testable on it.
- **Postgres-only behaviour** uses the `postgres_sessions` fixture in `tests/conftest.py`: one schema per test, dropped afterwards. It skips without `EVIDENCE_TEST_DATABASE_URL` and fails under `CI`. `tests/test_recovery_evidence_integration.py` also creates a temporary database per migration test.
- **Redis.** `tests/test_onchain_rpc.py` needs `REDIS_TEST_URL` pointing at a disposable Redis; it skips locally and fails under `CI` when unset.
- **Feature flags.** `tests/conftest.py` sets `AGENTS_ENABLED` and `ONCHAIN_ENABLED` before `app.main` is imported, because routers mount at import. Set a flag there, not inside a test.
- **API contract sweep.** `tests/api_contract/` hits every route; a new route needs an entry in `tests/api_contract/baseline.json`, and the baseline may never expect a 500. Read `tests/api_contract/README.md` first.
- **Test data is synthetic.** Fixtures use placeholder users and amounts from `tests/conftest.py`, never real accounts, balances or credentials.
