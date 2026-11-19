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

If you want the canonical project, its docs, demo, and community, go to **[Securo](https://github.com/securo-finance/securo)** ([website](https://usesecuro.com/) · [roadmap](https://www.usesecuro.com/roadmap) · [docs](https://docs.usesecuro.com/) · [demo](https://demo.usesecuro.com/)).

## What lovenest adds

On top of upstream Securo, this fork contributes:

- **OIDC-primary login layout** — surfaces single-sign-on as the primary path on the login screen, suited to households running their own identity provider (Authentik, Pocket ID, etc.).
- **Google OIDC `at_hash` sign-in fix** — accepts Google-issued `id_token`s that carry an `at_hash` claim, fixing a sign-in failure with Google as the OIDC provider. *(Contributed upstream as Securo PR #350.)*
- **SimpleFIN credit-card balance-sign normalization** — normalizes the balance sign for credit-card accounts synced via SimpleFIN so balances read correctly. *(Contributed upstream as Securo PR #351.)*
- **Built-from-source deployment** — runs the production stack built locally from this repository rather than from prebuilt upstream images (see [Quickstart](#quickstart)).

The two sign-in / sync fixes above were submitted back to Securo as pull requests #350 and #351, so they may already be present in upstream releases.

## Quickstart

lovenest needs **Git, Bash, and Docker with the Compose plugin** (supporting
`up --wait`). Node, Python, and bank-provider accounts are not needed on the host.
Clone the product branch, then run the fork's setup command:

```bash
git clone --branch lovenest https://github.com/ADolkun/lovenest.git
cd lovenest
tools/deploy/setup.sh
```

Setup creates a private `.env` with random secrets, builds the production images
from source, and waits for the app to be ready. It preserves an existing `.env`.
Open **http://localhost:3000** and complete the first-admin setup. Bank sync, OIDC,
and AI agents can be configured later; they are optional.

For custom ports or a domain, run `tools/deploy/setup.sh --env-only`, edit `.env`,
then run `tools/deploy/deploy.sh`. See [self-hosting](docs/self-hosting.md) for
updates, optional rebuilds after pulls, logs, backups, and troubleshooting.
The upstream `install.sh` installs Securo; use `tools/deploy/setup.sh` for Lovenest.

Upgrading an install that ran PostgreSQL 16? Run `scripts/upgrade-postgres.sh` once after pulling and before `up`; see [Upgrading PostgreSQL](docs/postgres-upgrade.md).

Balances, budgets, due dates, and recurring transactions use the application timezone configured in **Admin Settings → Date and time**. Each workspace can override it in **Workspace settings**. Without a saved setting, the application uses `TZ`, then the host timezone, then UTC.

## Branch model

- **`main`** mirrors upstream Securo.
- **`lovenest`** is the working and default branch, carrying the lovenest customizations described above.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow.

## Features (Securo)

The following is **Securo's feature set**, inherited unchanged by lovenest:

- Multi-account management with running balances
- Transaction management with search, filters, and CSV export
- File import (OFX, QIF, CAMT, CSV) and an auto-categorization rules engine
- Recurring transactions, budgets, and savings goals with progress tracking
- Asset management with valuation tracking and growth rules
- Reports: Net Worth, Income vs Expenses, and cashflow
- Bank sync via providers: **Pluggy** (Brazilian banks), **Enable Banking** (~2500 European PSD2 banks), **SimpleFIN** (US and international banks), and **Coinbase** (crypto balances and trade history, read-only API key) — extensible
- Multi-currency support with automatic FX conversion
- Multi-user support with an admin panel and registration controls
- Two-factor authentication (TOTP) with brute-force protection, plus passkey support
- OIDC login for Authentik, Pocket ID, and other standard providers, including OIDC-only deployments
- AI Agents (optional): self-hosted LLM chat with tool-use over your data, plus a per-agent RAG knowledge base
- Reporting-date override on synced transactions: report a transaction in another period without changing the bank's booking date (see [Reporting dates](#reporting-dates-and-bank-balances))
- Plugins (optional, off by default): install community plugins from the plugin catalog into a workspace, with a preview before anything changes (see [Optional capabilities](#optional-capabilities))
- Attachments and invoice logos on local disk, or in a private S3-compatible bucket (see [Attachment storage](#attachment-storage))

For provider setup (Pluggy, Enable Banking, SimpleFIN), OIDC configuration, exchange rates, and AI Agents, follow **[Securo's documentation](https://docs.usesecuro.com/)** — the underlying configuration keys are unchanged in this fork.

See [automatic bank synchronization](docs/automatic-bank-sync.md) to configure the minimum polling interval (including PSD2 four-access-per-day limits).

### Optional capabilities

Everything beyond the core is off by default, switched on with one setting, and costs nothing until then. `.env.example` documents each key.

- **Plugins**: set `PLUGINS_ENABLED=true` to let workspace members install data plugins (categories and rules). Plugins with code, such as sync from a service you use, also need `plugins` in `COMPOSE_PROFILES`, plus `PLUGINS_RUNNER_URL`, `PLUGINS_RUNNER_TOKEN` and `PLUGINS_EGRESS_SECRET`; they run in their own containers without the core secrets or the database. See [bank provider plugins](docs/plugins/bank-provider-plugins.md).
- **AI Agents**: `AGENTS_ENABLED=true` and `COMPOSE_PROFILES=agents` ([self-hosting](docs/self-hosting.md)).
- **Bank providers and exchange rates**: environment variables, or in the app under **Admin Settings → Bank connections** and **Exchange rates** without a restart ([provider settings](docs/provider-settings.md)).

### Attachment storage

Attachments and invoice logos use local disk by default. Set `STORAGE_PROVIDER=s3` to keep them in a private Amazon S3 or S3-compatible bucket (Garage, MinIO). Create the bucket first; Lovenest does not create buckets or change their policies, and downloads still go through the authorized API, so the bucket stays private.

```dotenv
STORAGE_PROVIDER=s3
STORAGE_S3_BUCKET=attachments
STORAGE_S3_REGION=garage
STORAGE_S3_ENDPOINT_URL=https://s3.example.com
STORAGE_S3_ADDRESSING_STYLE=path
STORAGE_S3_ACCESS_KEY=your-access-key
STORAGE_S3_SECRET_KEY=your-secret-key
```

For Amazon S3, omit the endpoint, use the bucket's region and leave the addressing style at `auto`. The key needs `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on the objects and `s3:ListBucket` on the bucket; omit both keys to use an IAM role, and set `STORAGE_S3_SESSION_TOKEN` for temporary credentials. `http://` endpoints are refused unless `STORAGE_S3_ALLOW_INSECURE_HTTP=true` (local development only). The S3 client ships in the regular image and loads only in S3 mode.

The setting applies to every attachment. To move existing files, stop the backend and workers, copy the contents of the attachments directory into the bucket keeping relative paths, verify, then switch the setting. Back up both PostgreSQL and the bucket. The design is upstream's [ADR 0019](docs/adr/0019-s3-storage.md).

### Reporting dates and bank balances

A synchronized transaction can carry a reporting-date override without changing the bank's booking date. Clear it to restore automatic attribution; later bank syncs keep the override. Reporting periods and transaction-list dates follow the override; account running balances and posted cash-flow balance movements still follow the bank date (the effective movement date in accrual mode). A posted expense attributed to a future period shows in that period's cash-flow outflow without being deducted from the projected balance a second time. The calendar offers separate reporting-date and bank-date views.

### Tech stack

| Layer | Stack |
|-------|-------|
| Backend | FastAPI, SQLAlchemy, Alembic, Celery |
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Database | PostgreSQL |
| Queue | Redis + Celery |

## AI-Assisted Development

Parts of this codebase were built with help of AI. All code is human-reviewed and no data leaves your environment.

Contributing with AI is welcome. We review the author, not the tool: whatever wrote the diff, you own its quality, its fit with where lovenest is going, and everything that happens after it merges. See [Using AI](CONTRIBUTING.md#using-ai).

## Support Securo

lovenest is built on Securo, which is free and open source. If lovenest helps you, a coffee keeps Securo's development going:

<p align="center">
  <a href="https://www.buymeacoffee.com/usesecuro"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" width="160" alt="Buy Me a Coffee" /></a>
</p>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

lovenest is licensed under the **[GNU Affero General Public License v3.0](LICENSE)**, the same license as upstream Securo.

- You may use, modify, and distribute this software freely.
- **Network-use clause:** if you run a modified version as a network service, you must offer its complete source code to the users who interact with it over the network.
- Any derivative work — including lovenest itself — must remain licensed under AGPL-3.0.

Upstream Securo copyright and license notices are retained; this fork does not relicense their work. By contributing to lovenest, you agree your contributions are licensed under AGPL-3.0.
