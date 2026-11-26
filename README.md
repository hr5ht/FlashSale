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
- **Safe retries.** Holding and confirming accept an `Idempotency-Key` header, so a retry never creates a second hold or booking. A retry that arrives after the first request finished gets the first result back.
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

Runs on Vercel with a Neon Postgres database. The Vercel project's Root Directory is `backend`, and the build step in `backend/pyproject.toml` copies `frontend/` into it so the deployed app can serve it. Migrations are run by hand from `backend/` (`uv run alembic upgrade head`) with Neon's direct (non-pooled) connection string before deploying.

## Benchmarks

`backend/scripts/flash_sale.py` runs two kinds of measurement against `flash_sale_test`. It empties that database's tables on every run, so never point it at a real database.

- **Locking:** every buyer requests 2 seats at the same instant, once per locking strategy, and the script then counts claims to check that no seat was sold twice.
- **Idempotency** (`--idempotency`): every buyer sends the same hold, then the same confirm, many times at once with one key, and the script counts what was actually created.

Run them inside the backend container, from the repo root:

```bash
T=postgresql+asyncpg://postgres:postgres@db:5432/flash_sale_test
docker compose exec backend python scripts/flash_sale.py --database-url $T                                          # 300 buyers, 50 seats
docker compose exec backend python scripts/flash_sale.py --database-url $T --buyers 1000 --seats 200 --isolation all
docker compose exec backend python scripts/flash_sale.py --database-url $T --idempotency --buyers 100 --retries 10
```

**Setup:** both containers on one MacBook Air (Apple silicon), PostgreSQL 17, 40 database connections. Buyers call the service layer directly, not over HTTP, so these numbers compare locking designs. They are not end-to-end latency.

### No seat sold twice

**0 oversells across 21,200 concurrent hold attempts**, covering every strategy at every isolation level, in all the runs below.

### Locking strategies, 1,000 buyers for 100 seat pairs (READ COMMITTED, median of 3 runs)

| Strategy | Seats sold (pairs of 100) | Holds/s | p50 ms | p95 ms |
|---|---:|---:|---:|---:|
| `skip_locked` | 60% | **547** | **963** | **1,741** |
| `for_update` | **94%** | 457 | 1,169 | 2,109 |
| `upsert` (default) | 92% | 377 | 1,436 | 2,538 |
| `unique_insert` | 86% | 330 | 1,855 | 2,935 |

What this shows:

- **`skip_locked` has the highest throughput but sells only 60% of the seats** (53–61% across the three runs). A buyer whose seats are locked, even for a moment, fails at once instead of waiting, so seats that would have freed up go unsold during the rush.
- **`for_update` sells the most, 94%,** and is second on speed: it queues on the locked rows instead of giving up. It doesn't reach 100% because neighbouring buyers want overlapping pairs, so when one pair wins, the pairs on either side of it lose.
- **At 300 buyers, `skip_locked` came third** (333 holds/s), behind `upsert` (396) and `for_update` (368). Its advantage only appears under heavy contention.
- **Under SERIALIZABLE, Postgres aborts conflicting transactions** (349–493 retries per run). The API returns those as `503` so the client can retry. No run had any errors.
- **Most of the latency is waiting for a connection:** 1,000 buyers share 40 connections, so throughput is the more meaningful figure.

<details>
<summary>All runs</summary>

300 buyers, 50 seats (at most 25 pairs):

| Strategy | Isolation | Won | Conflicts | Retries | Oversells | req/s | p50 ms | p95 ms | p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unique_insert | READ COMMITTED | 21 | 279 | 0 | 0 | 222.3 | 1034.7 | 1321.3 | 1326.6 |
| for_update | READ COMMITTED | 21 | 279 | 0 | 0 | 367.8 | 468.5 | 800.9 | 805.1 |
| skip_locked | READ COMMITTED | 17 | 283 | 0 | 0 | 332.8 | 682.2 | 880.3 | 887.3 |
| upsert | READ COMMITTED | 21 | 279 | 0 | 0 | 396.1 | 379.6 | 745.3 | 747.4 |

1,000 buyers, 200 seats (at most 100 pairs), all isolation levels:

| Strategy | Isolation | Won | Conflicts | Retries | Oversells | req/s | p50 ms | p95 ms | p99 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unique_insert | READ COMMITTED | 85 | 915 | 0 | 0 | 310.9 | 2011.5 | 3117.9 | 3156.0 |
| for_update | READ COMMITTED | 94 | 906 | 0 | 0 | 457.4 | 1168.6 | 2109.1 | 2155.3 |
| skip_locked | READ COMMITTED | 53 | 947 | 0 | 0 | 514.8 | 980.0 | 1839.5 | 1901.2 |
| upsert | READ COMMITTED | 90 | 910 | 0 | 0 | 377.4 | 1436.2 | 2538.1 | 2593.7 |
| unique_insert | REPEATABLE READ | 88 | 912 | 0 | 0 | 329.1 | 1891.0 | 2912.3 | 2971.6 |
| for_update | REPEATABLE READ | 95 | 905 | 0 | 0 | 377.3 | 1580.4 | 2561.0 | 2608.2 |
| skip_locked | REPEATABLE READ | 56 | 944 | 0 | 0 | 511.8 | 994.3 | 1855.7 | 1905.4 |
| upsert | REPEATABLE READ | 90 | 808 | 102 | 0 | 370.5 | 1442.4 | 2605.1 | 2668.7 |
| unique_insert | SERIALIZABLE | 87 | 564 | 349 | 0 | 298.7 | 2157.2 | 3211.7 | 3253.0 |
| for_update | SERIALIZABLE | 83 | 488 | 429 | 0 | 407.9 | 1262.5 | 2329.0 | 2367.3 |
| skip_locked | SERIALIZABLE | 52 | 896 | 52 | 0 | 438.7 | 1143.6 | 2033.3 | 2173.6 |
| upsert | SERIALIZABLE | 85 | 422 | 493 | 0 | 307.4 | 1758.9 | 3126.9 | 3159.0 |

Two more 1,000-buyer runs at READ COMMITTED (won / req/s / p95 ms):

| Strategy | Run 2 | Run 3 |
|---|---|---|
| unique_insert | 88 / 340.5 / 2830.4 | 86 / 329.9 / 2935.1 |
| for_update | 93 / 471.7 / 2020.0 | 94 / 448.7 / 2117.0 |
| skip_locked | 61 / 550.9 / 1733.8 | 60 / 547.2 / 1741.2 |
| upsert | 92 / 401.0 / 2402.9 | 95 / 313.6 / 3043.8 |

</details>

### Idempotent retries, 100 buyers × 10 simultaneous same-key requests

| Step | Requests | Rows created | Got the original back | Rejected |
|---|---:|---:|---:|---:|
| Hold | 1,000 | 100 | 1,000 | 0 |
| Confirm | 1,000 | 100 | 197 | 803 (`409 HoldNotActive`) |

**0 duplicate holds and 0 duplicate bookings.** Every simultaneous hold retry got the original hold back. Simultaneous confirm retries can't double-book either, but only the retries that arrive after the booking exists get it back; the rest are rejected with `409`. Retries that arrive one after another always get the original.

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
│                                (copied into backend/ during the Vercel build)
├── .github/workflows/ci.yml     lint, type-check, migrate and test with uv
├── docker-compose.yml           db (+ test database) and backend: the one-command setup
├── .dockerignore
└── README.md
```

Requests flow one way: `api/` endpoints call `services/`, which use `models/` and return `schemas/`. `core/` and `db/` are used by every layer and import none of them.
