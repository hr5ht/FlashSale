import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.exceptions import AppError, RetryableTransaction
from app.core.security import hash_password
from app.exceptions.booking import SeatLimitExceeded, SeatsUnavailable
from app.models.booking import Booking, Hold, SeatClaim
from app.models.event import Event, EventStatus, PriceTier, Seat, Section
from app.models.user import Role, User
from app.services.auth_service import register_user
from app.services.booking_service import confirm_hold
from app.services.hold_service import create_hold
from app.services.seat_acquisition import STRATEGIES

TABLES = "users, events, price_tiers, sections, seats, holds, seat_claims, bookings"


@dataclass
class Outcome:
    result: str
    seconds: float


@dataclass
class Report:
    strategy: str
    isolation: str
    attempts: int
    claims: int
    seats_total: int
    oversells: int
    wall_seconds: float
    latencies: list[float]
    tally: Counter

    @property
    def successes(self) -> int:
        return self.tally["success"]

    @property
    def conflicts(self) -> int:
        return self.tally["conflict"]

    @property
    def retryable(self) -> int:
        return self.tally["retryable"]

    @property
    def errors(self) -> int:
        return sum(count for name, count in self.tally.items() if name.startswith("error"))

    @property
    def throughput(self) -> float:
        return self.attempts / self.wall_seconds if self.wall_seconds else 0.0

    def percentile(self, fraction: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return ordered[index] * 1000


async def reset(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


async def seed_sale(
    factory: async_sessionmaker, seats: int, buyers: int
) -> tuple[Event, list[User], list[int]]:
    async with factory() as session:
        organizer = await register_user(
            session, "organizer@bench.local", "correct horse", Role.organizer
        )
        await session.flush()

        event = Event(
            organizer_id=organizer.id,
            title="Flash Sale",
            description="benchmark",
            category="concert",
            city="Bengaluru",
            venue="Benchmark Arena",
            starts_at=text("now() + interval '30 days'"),
            sale_starts_at=text("now() - interval '1 hour'"),
            status=EventStatus.published,
        )
        session.add(event)
        await session.flush()

        tier = PriceTier(event_id=event.id, name="Gold", price_cents=100000)
        section = Section(event_id=event.id, name="Floor")
        session.add_all([tier, section])
        await session.flush()

        await session.execute(
            text(
                "INSERT INTO seats (event_id, section_id, price_tier_id, row_label, seat_number) "
                "SELECT :event, :section, :tier, 'A', g FROM generate_series(1, :seats) g"
            ),
            {
                "event": event.id,
                "section": section.id,
                "tier": tier.id,
                "seats": seats,
            },
        )

        await session.execute(
            text(
                "INSERT INTO users (email, password_hash, role) "
                "SELECT 'buyer' || g || '@bench.local', :hash, 'attendee' "
                "FROM generate_series(1, :buyers) g"
            ),
            {"hash": hash_password("correct horse"), "buyers": buyers},
        )
        people = list(
            await session.scalars(
                select(User)
                .where(User.email.like("buyer%@bench.local"))
                .order_by(User.id)
            )
        )
        seat_ids = list(
            await session.scalars(
                select(Seat.id).where(Seat.event_id == event.id).order_by(Seat.id)
            )
        )
        await session.refresh(event)
        await session.commit()
        return event, people, seat_ids


async def attempt_hold(
    factory: async_sessionmaker,
    buyer: User,
    event: Event,
    seat_ids: list[int],
) -> Outcome:
    started = perf_counter()
    async with factory() as session:
        try:
            await create_hold(session, buyer, event, seat_ids, None)
            return Outcome("success", perf_counter() - started)
        except SeatsUnavailable:
            return Outcome("conflict", perf_counter() - started)
        except SeatLimitExceeded:
            return Outcome("conflict", perf_counter() - started)
        except RetryableTransaction:
            return Outcome("retryable", perf_counter() - started)
        except Exception as error:  # noqa: BLE001
            return Outcome(f"error:{type(error).__name__}", perf_counter() - started)


async def run_strategy(
    engine: AsyncEngine,
    strategy: str,
    isolation: str,
    buyers: int,
    seats: int,
    seats_per_buyer: int,
) -> Report:
    os.environ["SEAT_ACQUISITION_STRATEGY"] = strategy
    get_settings.cache_clear()
    await reset(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    event, people, seat_ids = await seed_sale(factory, seats, buyers)

    baskets = [
        [seat_ids[(index + offset) % seats] for offset in range(seats_per_buyer)]
        for index in range(buyers)
    ]

    started = perf_counter()
    outcomes = await asyncio.gather(
        *(
            attempt_hold(factory, buyer, event, basket)
            for buyer, basket in zip(people, baskets)
        )
    )
    wall = perf_counter() - started

    async with factory() as session:
        claims = await session.scalar(select(func.count()).select_from(SeatClaim)) or 0
        distinct = (
            await session.scalar(select(func.count(func.distinct(SeatClaim.seat_id))))
            or 0
        )

    return Report(
        strategy=strategy,
        isolation=isolation,
        attempts=buyers,
        claims=claims,
        seats_total=seats,
        oversells=(claims - distinct) + max(0, claims - seats),
        wall_seconds=wall,
        latencies=[outcome.seconds for outcome in outcomes],
        tally=Counter(outcome.result for outcome in outcomes),
    )


def print_reports(reports: list[Report], seats_per_buyer: int) -> None:
    header = (
        f"{'strategy':<15}{'isolation':<18}{'ok':>5}{'conflict':>10}"
        f"{'retry':>7}{'err':>5}{'claims':>8}{'oversell':>10}"
        f"{'req/s':>9}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}"
    )
    print(header)
    print("-" * len(header))
    for report in reports:
        print(
            f"{report.strategy:<15}{report.isolation:<18}{report.successes:>5}"
            f"{report.conflicts:>10}{report.retryable:>7}{report.errors:>5}"
            f"{report.claims:>8}{report.oversells:>10}"
            f"{report.throughput:>9.1f}{report.percentile(0.50):>9.1f}"
            f"{report.percentile(0.95):>9.1f}{report.percentile(0.99):>9.1f}"
        )
    print()
    capacity = reports[0].seats_total // seats_per_buyer if reports else 0
    print(f"seats {reports[0].seats_total}, {seats_per_buyer} per buyer -> at most {capacity} winners")
    total_oversells = sum(report.oversells for report in reports)
    print(f"total oversells across all runs: {total_oversells}")
    for report in reports:
        failures = {
            name: count
            for name, count in report.tally.items()
            if name.startswith("error")
        }
        if failures:
            print(f"{report.strategy} @ {report.isolation}: {failures}")


async def send_once(factory: async_sessionmaker, request) -> str:
    async with factory() as session:
        try:
            return f"id:{(await request(session)).id}"
        except AppError as error:
            return f"rejected:{type(error).__name__}"


def summarize(label: str, answers: list[str], retries: int, created: int) -> bool:
    groups = [answers[start : start + retries] for start in range(0, len(answers), retries)]
    ids_per_buyer = [{answer for answer in group if answer.startswith("id:")} for group in groups]
    same_id = sum(
        answer.startswith("id:") and len(ids) == 1
        for group, ids in zip(groups, ids_per_buyer)
        for answer in group
    )
    rejected = Counter(answer for answer in answers if answer.startswith("rejected"))
    print(
        f"{label:<8} requests {len(answers):>5}   created {created:>4}   "
        f"answered with the original {same_id:>5}   rejected {sum(rejected.values()):>4} {dict(rejected) or ''}"
    )
    return created == len(groups) and all(len(ids) <= 1 for ids in ids_per_buyer)


async def run_idempotency(engine: AsyncEngine, buyers: int, retries: int) -> bool:
    """Every buyer sends the same hold, then the same confirm, `retries` times at once with one key."""
    await reset(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    event, people, seat_ids = await seed_sale(factory, buyers * 2, buyers)

    hold_answers = await asyncio.gather(
        *(
            send_once(
                factory,
                lambda session, b=buyer, i=index: create_hold(
                    session, b, event, seat_ids[2 * i : 2 * i + 2], f"hold-{i}"
                ),
            )
            for index, buyer in enumerate(people)
            for _ in range(retries)
        )
    )
    async with factory() as session:
        rows = (await session.execute(select(Hold.user_id, Hold.id))).all()
        holds_created = len(rows)  # counted from rows, so a duplicate can't hide in the dict
        hold_ids = {user_id: hold_id for user_id, hold_id in rows}

    confirm_answers = await asyncio.gather(
        *(
            send_once(
                factory,
                lambda session, b=buyer, i=index: confirm_hold(
                    session, b, hold_ids[b.id], f"confirm-{i}"
                ),
            )
            for index, buyer in enumerate(people)
            for _ in range(retries)
        )
    )
    async with factory() as session:
        bookings_created = await session.scalar(select(func.count()).select_from(Booking)) or 0

    print(f"{buyers} buyers x {retries} simultaneous retries, one idempotency key per buyer per step\n")
    holds_ok = summarize("hold", hold_answers, retries, holds_created)
    bookings_ok = summarize("confirm", confirm_answers, retries, bookings_created)
    print(f"\nduplicates: {holds_created - buyers} holds, {bookings_created - buyers} bookings")
    return holds_ok and bookings_ok


HOT_QUERIES = {
    "event listing (city + category, ordered)": """
        SELECT e.id, e.title, e.starts_at,
               (SELECT min(pt.price_cents) FROM price_tiers pt WHERE pt.event_id = e.id) AS min_price
        FROM events e
        WHERE e.status = 'published' AND lower(e.city) = 'bengaluru' AND e.category = 'concert'
        ORDER BY e.starts_at, e.id LIMIT 20 OFFSET 0
    """,
    "event listing (category only, no city filter)": """
        SELECT e.id, e.title, e.starts_at
        FROM events e
        WHERE e.status = 'published' AND e.category = 'concert'
        ORDER BY e.starts_at, e.id LIMIT 20 OFFSET 0
    """,
    "event listing (city only)": """
        SELECT e.id, e.title, e.starts_at
        FROM events e
        WHERE e.status = 'published' AND lower(e.city) = 'bengaluru'
        ORDER BY e.starts_at, e.id LIMIT 20 OFFSET 0
    """,
    "seat map (seats of one event)": """
        SELECT s.id, s.row_label, s.seat_number, sec.name
        FROM seats s JOIN sections sec ON sec.id = s.section_id
        WHERE s.event_id = %(event)s
        ORDER BY sec.name, s.row_label, s.seat_number
    """,
    "seat availability (live claims of one event)": """
        SELECT sc.seat_id, h.status
        FROM seat_claims sc
        JOIN holds h ON h.id = sc.hold_id
        JOIN seats s ON s.id = sc.seat_id
        WHERE s.event_id = %(event)s
          AND (h.status = 'confirmed' OR (h.status = 'active' AND h.expires_at > now()))
    """,
    "per-user seat count for one event": """
        SELECT count(*) FROM seat_claims sc
        JOIN holds h ON h.id = sc.hold_id
        WHERE h.event_id = %(event)s AND h.user_id = %(user)s
          AND (h.status = 'confirmed' OR (h.status = 'active' AND h.expires_at > now()))
    """,
}


async def seed_for_explain(engine: AsyncEngine, events: int, seats: int) -> tuple[int, str]:
    await reset(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    event, people, seat_ids = await seed_sale(factory, seats, 20)

    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO events (organizer_id, title, description, category, city, venue, "
                "starts_at, sale_starts_at, status) "
                "SELECT :organizer, 'Event ' || g, 'filler', "
                "(ARRAY['concert','sports','theatre','conference','festival'])[1 + (g % 5)]::event_category, "
                "(ARRAY['Bengaluru','Mumbai','Delhi','Chennai','Pune'])[1 + (g % 5)], "
                "'Venue', now() + (g || ' days')::interval, now() - interval '1 day', 'published' "
                "FROM generate_series(1, :events) g"
            ),
            {"organizer": event.organizer_id, "events": events},
        )
        await session.execute(
            text(
                "INSERT INTO price_tiers (event_id, name, price_cents) "
                "SELECT id, 'Gold', 50000 + (id % 1000) FROM events WHERE id <> :event"
            ),
            {"event": event.id},
        )
        await session.execute(
            text(
                "INSERT INTO sections (event_id, name) "
                "SELECT id, 'Floor' FROM events WHERE id <> :event"
            ),
            {"event": event.id},
        )
        await session.execute(
            text(
                "INSERT INTO seats (event_id, section_id, price_tier_id, row_label, seat_number) "
                "SELECT sec.event_id, sec.id, pt.id, 'A', g "
                "FROM sections sec JOIN price_tiers pt ON pt.event_id = sec.event_id "
                "CROSS JOIN generate_series(1, 20) g WHERE sec.event_id <> :event"
            ),
            {"event": event.id},
        )
        await session.commit()

    async with factory() as session:
        for index in range(0, min(len(seat_ids), 40), 2):
            await create_hold(session, people[index // 2], event, [seat_ids[index]], None)

    async with engine.begin() as connection:
        await connection.execute(text("ANALYZE"))
    return event.id, str(people[0].id)


async def run_explain(engine: AsyncEngine, events: int, seats: int) -> None:
    event_id, user_id = await seed_for_explain(engine, events, seats)
    print(f"seeded {events + 1} events, {seats} seats on event {event_id}\n")
    async with engine.connect() as connection:
        for label, query in HOT_QUERIES.items():
            statement = query % {"event": event_id, "user": f"'{user_id}'::uuid"}
            plan = await connection.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS) {statement}")
            )
            print(f"=== {label} ===")
            for line in plan.scalars():
                print(f"  {line}")
            print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flash sale concurrency benchmark")
    parser.add_argument("--buyers", type=int, default=300)
    parser.add_argument("--seats", type=int, default=50)
    parser.add_argument("--seats-per-buyer", type=int, default=2)
    parser.add_argument(
        "--strategy",
        default="all",
        choices=["all", *STRATEGIES],
    )
    parser.add_argument(
        "--isolation",
        default="READ COMMITTED",
        choices=["all", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"],
    )
    parser.add_argument("--pool", type=int, default=40)
    parser.add_argument("--database-url", default=get_settings().test_database_url)
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--explain-events", type=int, default=5000)
    parser.add_argument("--explain-seats", type=int, default=5000)
    parser.add_argument("--idempotency", action="store_true", help="measure same-key retries instead")
    parser.add_argument("--retries", type=int, default=10)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    levels = (
        ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]
        if args.isolation == "all"
        else [args.isolation]
    )

    if args.explain:
        engine = create_async_engine(args.database_url)
        await run_explain(engine, args.explain_events, args.explain_seats)
        await engine.dispose()
        return

    if args.idempotency:
        engine = create_async_engine(args.database_url, pool_size=args.pool, max_overflow=0)
        no_duplicates = await run_idempotency(engine, args.buyers, args.retries)
        await reset(engine)
        await engine.dispose()
        raise SystemExit(0 if no_duplicates else 1)

    reports: list[Report] = []
    for level in levels:
        engine = create_async_engine(
            args.database_url,
            isolation_level=level,
            pool_size=args.pool,
            max_overflow=0,
        )
        for strategy in strategies:
            reports.append(
                await run_strategy(
                    engine,
                    strategy,
                    level,
                    args.buyers,
                    args.seats,
                    args.seats_per_buyer,
                )
            )
        await reset(engine)
        await engine.dispose()

    print_reports(reports, args.seats_per_buyer)


if __name__ == "__main__":
    asyncio.run(main())
