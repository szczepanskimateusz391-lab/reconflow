# ReconFlow

ReconFlow is a local demo application for reconciling e-commerce orders, payments, sales documents, and returns. It imports four CSV files, calculates the position as of a selected time, and presents cases that need review with calculations and references to source rows.

**All included data, decisions shown in screenshots, and demo results are synthetic.** The project is not prepared for production data.

## The problem and how ReconFlow works

Information about one order often lives in several exports. Finding a related payment is not enough: it may be partial, pending, in another currency, or completed after the analysis date. ReconFlow separates identifying a relationship from checking the transaction amount and status.

- Import validates the CSV schemas and records rejected rows with reasons. It skips identical records and reports a changed record with the same source key as a conflict without overwriting it.
- Reconciliation uses both the order ID and its source system. Without an unambiguous reference, it presents candidates for manual review; the rule score is not a confidence percentage.
- The summary separates detected amount differences, amounts needing investigation, and payouts requiring a separate process. Currencies are shown separately, without automatic conversion.
- The queue groups related findings into operational cases. Users can save a link, status, and comment, review the decision history, and export the cases visible under the current filters to CSV.

For example, order A-101 is worth `150.00 PLN` and has a completed payment of `100.00 PLN`, leaving a `50.00 PLN` underpayment. Marking the case as resolved closes its handling; it does not itself remove the difference or confirm that money has been recovered.

## Screenshots

These screenshots come from a separate PostgreSQL database with synthetic data. They show the running application, not mock-ups. They do not demonstrate that the full Playwright suite was run against Docker Compose. The application interface shown in them is in Polish.

| View | Screenshot |
| --- | --- |
| Results overview and amounts by currency | [Overview](docs/portfolio/screenshots/reconflow-podsumowanie.png) |
| Case queue | [Cases](docs/portfolio/screenshots/reconflow-kolejka.png) |
| A-101: calculation, sources, and decision history | [A-101 details](docs/portfolio/screenshots/reconflow-niedoplata-50.png) |

## Run with Docker

You need Docker Desktop with a running engine and the Compose plugin. From the project root, use a distinct project name to keep this checkout and its database volume separate from any other ReconFlow instance. The local check on 27 Sep 2026 used:

```bash
docker compose -p reconflow-readme-demo up --build -d
docker compose -p reconflow-readme-demo ps
```

That isolated project starts with its own, initially empty PostgreSQL database; import the synthetic CSV files to see reconciliation results. Reuse the same `-p` value for later `ps`, `stop`, and `up` commands.

These are **local addresses, not a hosted online demo**. Opening this README on GitHub does not start the application. Only after Docker Desktop reports that its engine is running and the Compose services are healthy, open these addresses on the **same computer**:

- Application: [http://127.0.0.1:8080](http://127.0.0.1:18080/)
- API documentation: [http://127.0.0.1:8000/docs](http://127.0.0.1:18080/)

If the browser reports `ERR_CONNECTION_REFUSED`, run `docker version` to check that the Docker engine is available, then `docker compose -p reconflow-readme-demo ps` in the project root to check this isolated project's services and ports. Start them with the same project name if needed. The links cannot work from GitHub alone or from another computer without a separate deployment. Ports `8080` and `8000` must be free; if you override `FRONTEND_PORT` or `BACKEND_PORT`, use those selected ports in the URLs.

On Windows, first open Docker Desktop and wait for **Engine running**. If PowerShell does not recognize `docker`, a per-user Docker Desktop installation can be called without changing `PATH`:

```powershell
$docker = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
& $docker version
& $docker compose -p reconflow-readme-demo up --build -d
& $docker compose -p reconflow-readme-demo ps
```

Run these commands from the project root. Keep Compose running while using the local addresses above. If your Docker Desktop installation is elsewhere, use its `docker.exe` path instead.

Compose starts PostgreSQL, Alembic migrations, FastAPI, and the nginx frontend. Ports bind to `127.0.0.1` by default, and the database uses a persistent volume. `docker compose -p reconflow-readme-demo stop` stops these services without deleting data. Do not use `down -v` if you want to keep the volume.

The demo database password in `docker-compose.yml` is only for local use. You can change the ports with `BACKEND_PORT` and `FRONTEND_PORT`; the amount tolerance, document deadline, and required document types are configured in Compose. Public deployment would require separate security configuration.

### Short demo walkthrough

1. Under **Importy** (Imports), upload `demo-data/orders.csv`, `payments.csv`, `documents.csv`, and `returns.csv` in that order. The counters show added, skipped, and rejected records. Importing the same file again does not create duplicates.
2. Under **Przegląd** (Overview), run reconciliation for the historical demo time: `2025-02-15 12:00 UTC` (`13:00` in Warsaw). The form shows the local time for the **next** analysis; the summary shows the date of the **latest result**.
3. Under **Problemy** (Cases), open A-101. Check `150.00 − 100.00 = 50.00 PLN`, the due date, document, and source rows. If the case is already marked **Rozwiązany** (Resolved), choose a filter that includes closed cases.
4. On a fresh demo database, save a status and comment. After another reconciliation, the decision history remains, and the underpayment stays in the financial result until payment data resolves it. The CSV export contains cases visible under the current filters; one row represents one operational case.

You can test an identifier conflict without overwriting the original record using `demo-data/conflicting-order.csv`. See the [CSV schema contract](docs/csv-schemas.md) for field definitions and the [manually specified test cases](docs/manual-cases.md) for expected results. These supporting documents are in Polish.

## Tests and verified scope

Dependencies are pinned in `backend/requirements*.lock` and `frontend/pnpm-lock.yaml`. Local testing on Windows requires Python 3.12, Node.js with Corepack/pnpm, and PostgreSQL 18 with its tools on `PATH` or specified through `RECONFLOW_POSTGRES_BIN`. Tests modify only dedicated databases; do not run them against a database containing your own decisions.

```powershell
# From the project root
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.\tests\run-postgres-integration.ps1

# Frontend
cd ..\frontend
corepack enable
corepack prepare pnpm@11.19.0 --activate
pnpm install --frozen-lockfile
pnpm run build
pnpm exec playwright install chromium
pnpm run test:e2e:standalone
```

`test:e2e:standalone` starts a separate PostgreSQL cluster, API, and Vite instance. Before importing, the test checks that it is connected to the test instance. The PostgreSQL integration suite also starts a separate cluster and checks concurrent attempts to assign one transaction. Environment details and commands are recorded in the [audit report](docs/final-audit.md), which is in Polish.

| Check | Actual result | Scope |
| --- | --- | --- |
| Backend, 18 Sep 2026 | `30 passed, 4 skipped` | The skipped tests required a dedicated PostgreSQL instance and passed separately. |
| PostgreSQL integration, 18 Sep 2026 | `4 passed` | Import, `NUMERIC` precision, date boundaries, and concurrent assignment conflict. |
| Full local Playwright run, 21 Sep 2026 | `5 passed` | Separate PostgreSQL database, not Compose. This earlier run predates the new import-message test. |
| Isolated Docker Compose, 20 Sep 2026 | PASS | Build, migrations, health checks, four imports, reconciliation, decision, and persistence after restart. |
| Reimport message, 26 Sep 2026 | `1 passed` | One selected Playwright test on separate Compose: `skipped_count=0` without the message and `skipped_count=1` with it. [First](docs/evidence/import-skipped-first.png) and [second](docs/evidence/import-skipped-repeat.png) screenshots. |
| Publication-copy frontend build, 26 Sep 2026 | PASS | `docker compose -p reconflow-public-prep build frontend`; TypeScript and Vite completed without errors. |
| Isolated local launch from this checkout, 27 Sep 2026 | PASS | `docker compose -p reconflow-readme-demo up --build -d`; migrations `0001` and `0002`, all three services healthy, HTTP 200 from the application, proxied `/api/health`, and API docs. The browser displayed ReconFlow and Swagger UI. This fresh database was not populated or used for a full workflow test. |
| **Full Playwright suite against Compose** | **NOT RUN** | Neither the single import test nor the local PostgreSQL run should be presented as this check. |

The generator creates 10,000 synthetic orders with a fixed seed and independent files of expected links and alerts. The [benchmark results](docs/benchmark-results.json) are for a controlled dataset and a SQLite harness, **not** PostgreSQL performance or real-world data. They do not predict recovered funds or time savings.

## Project structure and limitations

- `backend/`: FastAPI, reconciliation rules, SQLAlchemy, Alembic migrations, and Pytest;
- `frontend/`: React, TypeScript, Vite, Tailwind CSS, and Playwright tests;
- `demo-data/`: synthetic demo files;
- `docs/`: CSV schemas, test cases, audit, and screenshots;
- `scripts/`: data generator and benchmark.

This is an MVP for one demo company, without login, roles, bank or marketplace integrations, automatic currency conversion, bookkeeping, or actions in external systems. Batched marketplace payouts, fees, and currency conflicts require a separate process. Production use would require, among other things, access control, data protection, deployment-specific secret configuration, and validation on real data.

This repository does not currently specify a license for the application code. Dependencies retain their own licenses and author information; their names and versions are listed in the dependency and lock files.
