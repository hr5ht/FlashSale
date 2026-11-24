from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import translate
from app.exceptions.booking import BookingNotFound, HoldNotActive
from app.models.booking import Booking, Hold, HoldStatus, SeatClaim
from app.models.event import PriceTier, Seat
from app.models.user import User
from app.schemas.booking import BookingRead
from app.schemas.pagination import Page, PageParams
from app.schemas.seat_map import SeatAvailability
from app.services.hold_service import load_owned_hold, seat_ids_for_hold
from app.services.seat_broadcaster import send_seat_update


async def find_booking_by_key(
    session: AsyncSession, user: User, idempotency_key: str
) -> Booking | None:
    return await session.scalar(
        select(Booking).where(
            Booking.user_id == user.id, Booking.idempotency_key == idempotency_key
        )
    )


def booking_read(booking: Booking, seat_ids: list[int]) -> BookingRead:
    return BookingRead(
        id=booking.id,
        event_id=booking.event_id,
        hold_id=booking.hold_id,
        seat_ids=seat_ids,
        total_cents=booking.total_cents,
        created_at=booking.created_at,
    )


async def to_booking_read(session: AsyncSession, booking: Booking) -> BookingRead:
    return booking_read(booking, await seat_ids_for_hold(session, booking.hold_id))


async def confirm_hold(
    session: AsyncSession, user: User, hold_id: int, idempotency_key: str | None
) -> BookingRead:
    if idempotency_key is not None:
        existing = await find_booking_by_key(session, user, idempotency_key)
        if existing is not None:
            return await to_booking_read(session, existing)

    hold = await load_owned_hold(session, user, hold_id)
    if hold.status is not HoldStatus.active:
        raise HoldNotActive

    still_live = await session.scalar(
        select(Hold.expires_at > func.now()).where(Hold.id == hold.id)
    )
    if not still_live:
        raise HoldNotActive

    total_cents = await session.scalar(
        select(func.sum(PriceTier.price_cents))
        .select_from(SeatClaim)
        .join(Seat, Seat.id == SeatClaim.seat_id)
        .join(PriceTier, PriceTier.id == Seat.price_tier_id)
        .where(SeatClaim.hold_id == hold.id)
    )
    if not total_cents:
        raise HoldNotActive

    booking = Booking(
        event_id=hold.event_id,
        user_id=user.id,
        hold_id=hold.id,
        total_cents=total_cents,
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(booking)
            hold.status = HoldStatus.confirmed
            await session.flush()
        await session.commit()
    except IntegrityError:
        if idempotency_key is not None:
            concurrent = await find_booking_by_key(session, user, idempotency_key)
            if concurrent is not None:
                return await to_booking_read(session, concurrent)
        raise HoldNotActive
    except DBAPIError as error:
        raise translate(error)

    await session.refresh(booking)
    confirmed = await to_booking_read(session, booking)
    await send_seat_update(
        booking.event_id, confirmed.seat_ids, SeatAvailability.booked
    )
    return confirmed


async def list_bookings(
    session: AsyncSession, user: User, page: PageParams
) -> Page[BookingRead]:
    total = await session.scalar(
        select(func.count()).select_from(Booking).where(Booking.user_id == user.id)
    )
    bookings = list(
        await session.scalars(
            select(Booking)
            .where(Booking.user_id == user.id)
            .order_by(Booking.created_at.desc(), Booking.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    )

    hold_ids = [booking.hold_id for booking in bookings]
    claims = await session.execute(
        select(SeatClaim.hold_id, SeatClaim.seat_id)
        .where(SeatClaim.hold_id.in_(hold_ids))
        .order_by(SeatClaim.seat_id)
    )
    seats_by_hold: dict[int, list[int]] = {hold_id: [] for hold_id in hold_ids}
    for hold_id, seat_id in claims.all():
        seats_by_hold[hold_id].append(seat_id)

    items = [booking_read(booking, seats_by_hold[booking.hold_id]) for booking in bookings]
    return Page(items=items, total=total or 0, limit=page.limit, offset=page.offset)


async def get_booking(session: AsyncSession, user: User, booking_id: int) -> BookingRead:
    booking = await session.get(Booking, booking_id)
    if booking is None or booking.user_id != user.id:
        raise BookingNotFound
    return await to_booking_read(session, booking)


async def cancel_booking(session: AsyncSession, user: User, booking_id: int) -> None:
    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update()
    )
    if booking is None or booking.user_id != user.id:
        raise BookingNotFound
    seat_ids = await seat_ids_for_hold(session, booking.hold_id)
    await session.execute(delete(SeatClaim).where(SeatClaim.hold_id == booking.hold_id))
    await session.execute(
        update(Hold).where(Hold.id == booking.hold_id).values(status=HoldStatus.released)
    )
    await session.delete(booking)
    try:
        await session.commit()
    except DBAPIError as error:
        raise translate(error)
    await send_seat_update(booking.event_id, seat_ids, SeatAvailability.available)
