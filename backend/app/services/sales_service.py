from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, Hold, HoldStatus, SeatClaim
from app.models.event import Event, Seat
from app.schemas.booking import SalesSummary


async def sales_summary(session: AsyncSession, event: Event) -> SalesSummary:
    seats_total = (
        select(func.count()).select_from(Seat).where(Seat.event_id == event.id)
    ).scalar_subquery()

    def claimed_seats(condition: ColumnElement[bool]) -> ColumnElement[int]:
        return (
            select(func.count())
            .select_from(SeatClaim)
            .join(Hold, Hold.id == SeatClaim.hold_id)
            .where(Hold.event_id == event.id, condition)
        ).scalar_subquery()

    seats_booked = claimed_seats(Hold.status == HoldStatus.confirmed)
    seats_held = claimed_seats(
        and_(Hold.status == HoldStatus.active, Hold.expires_at > func.now())
    )
    bookings_made = (
        select(func.count()).select_from(Booking).where(Booking.event_id == event.id)
    ).scalar_subquery()
    gross_cents = (
        select(func.coalesce(func.sum(Booking.total_cents), 0)).where(
            Booking.event_id == event.id
        )
    ).scalar_subquery()

    row = (
        await session.execute(
            select(
                seats_total.label("seats_total"),
                seats_booked.label("seats_booked"),
                seats_held.label("seats_held"),
                bookings_made.label("bookings"),
                gross_cents.label("gross_cents"),
            )
        )
    ).one()

    return SalesSummary(
        event_id=event.id,
        seats_total=row.seats_total,
        seats_booked=row.seats_booked,
        seats_held=row.seats_held,
        seats_available=row.seats_total - row.seats_booked - row.seats_held,
        bookings=row.bookings,
        gross_cents=row.gross_cents,
    )
