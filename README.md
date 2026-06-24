<h1 align="center">lovenest</h1>
<p align="center">
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0" /></a>
  <a href="https://github.com/securo-finance/securo"><img src="https://img.shields.io/badge/fork%20of-Securo-5865F2.svg" alt="Fork of Securo" /></a>
</p>

<p align="center">
<strong>lovenest</strong> is a self-hosted personal and household finance hub — a friendly downstream fork of <a href="https://github.com/securo-finance/securo">Securo</a>. It runs entirely on your own infrastructure, giving you and your household full visibility into accounts, spending, and habits without surrendering your financial data to third parties. lovenest tracks upstream Securo closely and layers on a few deployment and login customizations on top.
</p>

## Attribution

**lovenest is built on [Securo](https://github.com/securo-finance/securo), licensed under [AGPL-3.0](LICENSE).**

The core application — every feature listed below — is the work of the Securo project and its contributors. lovenest is a thin **customization and deployment layer** on top of that work: it does not reimplement the app, and it does not claim Securo's features as its own. Upstream copyright and the AGPL-3.0 license are preserved in full (see [LICENSE](LICENSE)).

If you want the canonical project, its docs, demo, and community, go to **[Securo](https://github.com/securo-finance/securo)** ([website](https://usesecuro.com/) · [docs](https://docs.usesecuro.com/) · [demo](https://demo.usesecuro.com/)).

## What lovenest adds

On top of upstream Securo, this fork contributes:

- **OIDC-primary login layout** — surfaces single-sign-on as the primary path on the login screen, suited to households running their own identity provider (Authentik, Pocket ID, etc.).
- **Google OIDC `at_hash` sign-in fix** — accepts Google-issued `id_token`s that carry an `at_hash` claim, fixing a sign-in failure with Google as the OIDC provider. *(Contributed upstream as Securo PR #350.)*
- **SimpleFIN credit-card balance-sign normalization** — normalizes the balance sign for credit-card accounts synced via SimpleFIN so balances read correctly. *(Contributed upstream as Securo PR #351.)*
- **Built-from-source deployment** — runs the production stack built locally from this repository rather than from prebuilt upstream images (see [Quickstart](#quickstart)).

The two sign-in / sync fixes above were submitted back to Securo as pull requests #350 and #351, so they may already be present in upstream releases.

## Quickstart

lovenest builds the production stack from source in this repository:

```bash
docker compose -f docker-compose.prod.yml build && docker compose -f docker-compose.prod.yml up -d
```

Environment configuration lives in `.env` — copy `.env.example` to `.env` and fill in your values (`SECRET_KEY`, optional bank-sync and OIDC settings, etc.) before bringing the stack up.

## Branch model

- **`main`** mirrors upstream Securo.
- **`custom`** is the working and default branch, carrying the lovenest customizations described above.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.

## Features (Securo)

The following is **Securo's feature set**, inherited unchanged by lovenest:

- Multi-account management with running balances
- Transaction management with search, filters, and CSV export
- File import (OFX, QIF, CAMT, CSV) and an auto-categorization rules engine
- Recurring transactions, budgets, and savings goals with progress tracking
- Asset management with valuation tracking and growth rules
- Reports: Net Worth, Income vs Expenses, and cashflow
- Bank sync via providers: **Pluggy** (Brazilian banks), **Enable Banking** (~2500 European PSD2 banks), and **SimpleFIN** (US and international banks) — extensible
- Multi-currency support with automatic FX conversion
- Multi-user support with an admin panel and registration controls
- Two-factor authentication (TOTP) with brute-force protection, plus passkey support
- OIDC login for Authentik, Pocket ID, and other standard providers
- AI Agents (optional): self-hosted LLM chat with tool-use over your data, plus a per-agent RAG knowledge base

For provider setup (Pluggy, Enable Banking, SimpleFIN), OIDC configuration, exchange rates, and AI Agents, follow **[Securo's documentation](https://docs.usesecuro.com/)** — the underlying configuration keys are unchanged in this fork.

### Tech stack

| Layer | Stack |
|-------|-------|
| Backend | FastAPI, SQLAlchemy, Alembic, Celery |
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Database | PostgreSQL |
| Queue | Redis + Celery |

## License

lovenest is licensed under the **[GNU Affero General Public License v3.0](LICENSE)**, the same license as upstream Securo.

- You may use, modify, and distribute this software freely.
- **Network-use clause:** if you run a modified version as a network service, you must offer its complete source code to the users who interact with it over the network.
- Any derivative work — including lovenest itself — must remain licensed under AGPL-3.0.

Upstream Securo copyright and license notices are retained; this fork does not relicense their work. By contributing to lovenest, you agree your contributions are licensed under AGPL-3.0.
