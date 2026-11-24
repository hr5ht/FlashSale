# Flash-Sale Ticketing

Backend for selling event tickets when a lot of people try to buy the same seats at the same time. The main rule is that a seat can never be sold twice.

FastAPI, PostgreSQL, SQLAlchemy (async), Alembic.

Live: https://flashsaleticketing.vercel.app. The API lives under `/api/v1`; docs are at `/docs` when running locally.

## Try it

Sign in as Adam or Ben from the sign-in page (password `demo-password`), and open the other one in a private window to watch seat changes arrive live. The Requests button shows every call the page makes.

## How it works

- Each held or booked seat is a row in `seat_claims`, and `seat_id` is the primary key, so Postgres rejects a second claim on the same seat.
- A hold is all-or-nothing: all seats are inserted together inside a savepoint, so if one is taken none are held.
- Holds expire after 2 minutes. Expiry is checked against the database clock whenever seats are read or claimed, so there is no background job.
- Holding and confirming accept an `Idempotency-Key` header, so a retried request returns the first result.
- There are four ways of claiming seats (`unique_insert`, `for_update`, `skip_locked`, `upsert`), picked with `SEAT_ACQUISITION_STRATEGY`. `scripts/flash_sale.py` runs many buyers against each one and checks for oversells.

## Running locally

### Everything in Docker (one command)

Needs Docker. From the repo root:

```bash
docker compose up --build
```

Two containers:

- **db**: PostgreSQL 17 on `localhost:5433`, with `flash_sale` and an empty `flash_sale_test` (for running `pytest` from your machine).
- **backend**: `scripts/start.sh` runs the migrations, starts the app on http://localhost:8000, then adds the demo events and the Adam and Ben accounts (password `demo-password`) through the API. It skips anything that already exists, so restarts are safe. Seeding only happens when `SEED_DEMO=true`, which compose sets.

No `.env` is needed for this. Compose uses its own local-only settings, and you can override `JWT_SECRET_KEY` or `SEED_PASSWORD` from your shell.

```bash
docker compose down              # stop; data stays in the db-data volume
docker compose down -v           # stop and wipe the database
```

### Backend on your machine, Postgres in Docker

Needs [uv](https://docs.astral.sh/uv/). uv installs the right Python (see `backend/.python-version`) and the exact versions in `backend/uv.lock`.

```bash
docker compose up -d db          # just Postgres, on localhost:5433
cd backend
uv sync                          # creates .venv with app + dev dependencies
cp .env.example .env             # already points at localhost:5433; set JWT_SECRET_KEY
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Dependencies are managed with `uv add <package>` (or `uv add --dev <package>`), which updates `pyproject.toml` and `uv.lock` together.

When running without Docker, add the demo events and the two demo buyers with (from `backend/`):

```bash
SEED_PASSWORD=<any password> uv run scripts/seed_demo.py
```

Sign-up is closed in production. Admins can create and publish events from the Manage page, book like any buyer, and run the rules lab, which checks every backend rule against the API. To make an admin account (it asks for the password):

```bash
uv run scripts/create_admin.py <email>
```

## Tests

```bash
cd backend
uv run pytest
```

The tests use `TEST_DATABASE_URL`. They drop and recreate every table in it, so it must be its own empty database and never the one in `DATABASE_URL`.

## Deployment

Runs on Vercel with a Neon Postgres database. The Vercel project's Root Directory is `backend`. Migrations are run by hand from `backend/` (`uv run alembic upgrade head`) with Neon's direct (non-pooled) connection string before deploying.

## Project structure

```
.
├── backend/
│   ├── app/
│   │   ├── main.py              create_app(): middleware, error handlers, routers, frontend
│   │   ├── core/                config, security (JWT, passwords), middleware, base errors
│   │   ├── db/                  declarative Base, engine and session
│   │   ├── models/              SQLAlchemy tables: user, event, booking
│   │   ├── schemas/             Pydantic request/response models
│   │   ├── exceptions/          domain errors: auth, event, booking
│   │   ├── services/            business logic
│   │   │   ├── auth_service.py
│   │   │   ├── event_service.py
│   │   │   ├── hold_service.py        hold and release seats
│   │   │   ├── booking_service.py     confirm, list, cancel bookings
│   │   │   ├── sales_service.py       organizer sales summary
│   │   │   ├── seat_map_service.py    seat map with live availability
│   │   │   ├── seat_acquisition.py    the four seat-claiming strategies
│   │   │   └── seat_broadcaster.py    pushes seat changes to open WebSockets
│   │   └── api/
│   │       ├── dependencies.py  session, current user, event access
│   │       ├── health.py        GET /health (unversioned)
│   │       └── v1/
│   │           ├── router.py    mounts every endpoint under /api/v1
│   │           └── endpoints/   auth, events, bookings, seatmap (WebSocket)
│   ├── alembic/                 migrations
│   ├── scripts/                 start.sh (container start), create_admin, seed_demo, flash_sale
│   ├── tests/                   auth/, events/, bookings/, plus app-wide tests
│   ├── Dockerfile               uv, locked deps, runs scripts/start.sh
│   ├── alembic.ini
│   ├── pyproject.toml
│   ├── uv.lock
│   └── vercel.json
├── frontend/                    index.html, app.js, styles.css (served by the backend at /)
├── docker/postgres/             creates the test database on first start
├── .github/workflows/ci.yml
├── docker-compose.yml           db + backend (one-command setup)
├── .dockerignore
└── README.md
```

Requests flow one way: `api/` endpoints call `services/`, which use `models/` and return `schemas/`. `core/` and `db/` are used by every layer and import none of them.
