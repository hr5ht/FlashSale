# Flash-Sale Ticketing

A backend for selling event tickets when many people try to buy the same seats at the same moment. The one rule it is built around: **a seat can never be sold twice.**

Python · FastAPI · PostgreSQL · SQLAlchemy (async) · Alembic · WebSockets · uv · Docker

Live demo: https://flashsaleticketing.vercel.app

> **Disclaimer:** the frontend in `frontend/` is AI-generated and exists only to show the backend working. The project is the backend in `backend/`: the API, the database design and the concurrency handling.

## What it does

Organizers create events with sections, rows and price tiers, then publish them. When the sale opens, buyers pick seats, **hold** them for 2 minutes, and **confirm** the hold to get a booking. Everyone watching an event sees seats change state live over a WebSocket.

How the main problems are solved:

- **No double-selling.** Each held or booked seat is one row in `seat_claims`, with `seat_id` as the primary key, so Postgres itself rejects a second claim on the same seat.
- **All-or-nothing holds.** A hold's seats are claimed together inside a savepoint. If any one is taken, none are held.
- **Expiry without a cleanup job.** Holds expire after 2 minutes, checked against the database clock in the same queries that read or claim seats. Abandoned carts free their seats without any background worker.
- **Safe retries.** Holding and confirming accept an `Idempotency-Key` header, so a retried request returns the first result instead of booking twice.
- **Four locking strategies.** Seats can be claimed with `unique_insert`, `for_update`, `skip_locked` or `upsert`, chosen with `SEAT_ACQUISITION_STRATEGY` (default `upsert`). See [Benchmarks](#benchmarks).
- **Auth and roles.** Short-lived JWT access tokens with refresh tokens. Attendees book, organizers manage their own events, and admins can manage everything.

The API lives under `/api/v1`, with interactive docs at `/docs` when running locally.

### Try the demo

Sign in as Adam or Ben (password `demo-password`), and open the other one in a private window to watch seat changes arrive live. The Requests button shows every API call the page makes.

## Installation

### With Docker (one command)

Needs [Docker](https://www.docker.com/). From the repo root:

```bash
docker compose up --build
```

This starts two containers:

- **db**: PostgreSQL 17 on `localhost:5433`, with the `flash_sale` database and an empty `flash_sale_test` for tests.
- **backend**: runs the migrations, starts the app on http://localhost:8000, then adds the demo events and the Adam and Ben accounts. Restarting is safe, because anything that already exists is skipped.

No `.env` file is needed. Compose uses its own local-only settings; override `JWT_SECRET_KEY` or `SEED_PASSWORD` from your shell if you like.

```bash
docker compose up --build -d     # same, in the background
docker compose stop              # stop, keep everything
docker compose down -v           # remove the containers and wipe the database
```

### For development (app on your machine, Postgres in Docker)

Needs [uv](https://docs.astral.sh/uv/), which installs the right Python version and the exact package versions from `backend/uv.lock`.

```bash
docker compose up -d db          # only Postgres, on localhost:5433
cd backend
uv sync                          # creates .venv with app + dev dependencies
cp .env.example .env             # already points at localhost:5433; set JWT_SECRET_KEY
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The backend container and a local `uvicorn` both use port 8000, so run one at a time.

Other commands, from `backend/`:

```bash
uv run pytest                                        # tests (use flash_sale_test)
SEED_PASSWORD=<any password> uv run scripts/seed_demo.py   # demo data, without Docker
uv run scripts/create_admin.py <email>               # make an admin account
uv add <package>                                     # add a dependency
```

The tests drop and recreate every table in `TEST_DATABASE_URL`, so it must never point at a database you care about.

### Deployment

Runs on Vercel with a Neon Postgres database. The Vercel project's Root Directory is `backend`. Migrations are run by hand from `backend/` (`uv run alembic upgrade head`) with Neon's direct (non-pooled) connection string before deploying.

## Benchmarks

`backend/scripts/flash_sale.py` starts a flash sale with every buyer requesting 2 seats at the same instant, runs it once per locking strategy, and then counts the claims to check that no seat was sold twice.

```bash
cd backend
uv run scripts/flash_sale.py                                              # 300 buyers, 50 seats
uv run scripts/flash_sale.py --buyers 1000 --seats 200 --isolation all    # 1,000 buyers, 200 seats, 3 isolation levels
```

The script writes to `TEST_DATABASE_URL` and empties its tables on every run. Never point it at a real database.

**Setup:** MacBook Air (Apple silicon), PostgreSQL 17 in Docker, 40 database connections. Buyers call the hold service directly, not over HTTP.

**Result: 0 oversells across all 13,200 attempts in the 16 runs below.**

### 300 buyers, 50 seats (at most 25 winners)

| Strategy | Isolation | Won | Conflicts | Retries | Oversells | req/s | p50 ms | p95 ms | p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unique_insert | READ COMMITTED | 22 | 278 | 0 | 0 | 215.2 | 1011.0 | 1374.2 | 1377.4 |
| for_update | READ COMMITTED | 23 | 277 | 0 | 0 | 406.2 | 447.6 | 723.8 | 727.9 |
| **skip_locked** | READ COMMITTED | 21 | 279 | 0 | 0 | **452.9** | **368.6** | **641.3** | **646.7** |
| upsert | READ COMMITTED | 22 | 278 | 0 | 0 | 367.6 | 429.8 | 802.7 | 806.0 |

### 1,000 buyers, 200 seats (at most 100 winners)

| Strategy | Isolation | Won | Conflicts | Retries | Oversells | req/s | p50 ms | p95 ms | p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unique_insert | READ COMMITTED | 87 | 913 | 0 | 0 | 291.8 | 2171.0 | 3221.9 | 3373.4 |
| for_update | READ COMMITTED | 87 | 913 | 0 | 0 | 456.9 | 1207.7 | 2106.7 | 2159.0 |
| **skip_locked** | READ COMMITTED | 82 | 918 | 0 | 0 | **520.6** | **1058.7** | **1832.9** | **1886.2** |
| upsert | READ COMMITTED | 90 | 910 | 0 | 0 | 372.7 | 1339.4 | 2570.4 | 2632.7 |
| unique_insert | REPEATABLE READ | 88 | 912 | 0 | 0 | 341.4 | 1812.8 | 2808.9 | 2884.5 |
| for_update | REPEATABLE READ | 87 | 913 | 0 | 0 | 435.8 | 1289.2 | 2182.9 | 2242.5 |
| skip_locked | REPEATABLE READ | 86 | 914 | 0 | 0 | 506.7 | 1058.0 | 1844.8 | 1902.5 |
| upsert | REPEATABLE READ | 90 | 811 | 99 | 0 | 375.4 | 1357.9 | 2559.0 | 2621.6 |
| unique_insert | SERIALIZABLE | 85 | 524 | 391 | 0 | 298.8 | 1977.7 | 3208.4 | 3255.5 |
| for_update | SERIALIZABLE | 79 | 484 | 437 | 0 | 392.4 | 1427.9 | 2417.6 | 2462.9 |
| skip_locked | SERIALIZABLE | 72 | 822 | 106 | 0 | 488.1 | 1081.6 | 1931.4 | 1963.6 |
| upsert | SERIALIZABLE | 82 | 413 | 505 | 0 | 344.3 | 1594.3 | 2790.0 | 2822.8 |

No run had any errors.

**What the numbers show:**

- **Correctness holds everywhere.** Every strategy, at every isolation level, sold each seat at most once.
- **`skip_locked` was the fastest in every comparison here.** A buyer whose seats are already locked fails at once instead of waiting in line, which gave the highest throughput and lowest P95 (520 req/s and 1.8 s at 1,000 buyers). `for_update` was a close second.
- **Fewer winners than seats is expected.** Neighbouring buyers ask for overlapping seats, so some seats lose all their contests and stay free.
- **SERIALIZABLE trades speed for retries.** Postgres aborts conflicting transactions, which shows up as `Retries`. The API returns those as `503`, and the client can retry.
- **Most of the latency is waiting for a connection.** 1,000 buyers share 40 connections, so most of each request's time is spent queueing. Throughput is the more meaningful figure.

Results vary by a few percent between runs; run the commands above to reproduce them.

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
│   ├── scripts/                 start.sh (container start), seed_demo, create_admin, flash_sale (benchmark)
│   ├── tests/                   auth/, events/, bookings/, plus app-wide tests
│   ├── Dockerfile               uv, locked deps, runs scripts/start.sh
│   ├── alembic.ini
│   ├── pyproject.toml
│   ├── uv.lock
│   └── vercel.json
├── frontend/                    demo UI (AI-generated), served by the backend at /
├── docker/postgres/             creates the test database on first start
├── .github/workflows/ci.yml     lint, type-check, migrate and test with uv
├── docker-compose.yml           db + backend (one-command setup)
├── .dockerignore
└── README.md
```

Requests flow one way: `api/` endpoints call `services/`, which use `models/` and return `schemas/`. `core/` and `db/` are used by every layer and import none of them.
