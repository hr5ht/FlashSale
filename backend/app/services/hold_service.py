from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, cast, delete, func, literal, select
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import translate
from app.exceptions.booking import (
    EventNotOnSale,
    HoldNotActive,
    HoldNotFound,
    SaleNotStarted,
    SeatLimitExceeded,
    SeatsNotInEvent,
    SeatsUnavailable,
)
from app.models.booking import Hold, HoldStatus, SeatClaim
from app.models.event import Event, EventStatus, Seat
from app.models.user import User
from app.schemas.booking import HoldRead, HoldState
from app.schemas.seat_map import SeatAvailability
from app.services.seat_acquisition import acquire_seats, live_claim_condition
from app.services.seat_broadcaster import send_seat_update


def hold_expiry_expression() -> ColumnElement[datetime]:
    seconds = get_settings().hold_duration_seconds
    return func.now() + cast(literal(f"{seconds} seconds"), INTERVAL)


async def find_hold_by_key(
    session: AsyncSession, user: User, idempotency_key: str
) -> Hold | None:
    return await session.scalar(
        select(Hold).where(
            Hold.user_id == user.id, Hold.idempotency_key == idempotency_key
        )
    )


async def seat_ids_for_hold(session: AsyncSession, hold_id: int) -> list[int]:
    seat_ids = await session.scalars(
        select(SeatClaim.seat_id)
        .where(SeatClaim.hold_id == hold_id)
        .order_by(SeatClaim.seat_id)
    )
    return list(seat_ids)


async def hold_state(session: AsyncSession, hold: Hold) -> HoldState:
    if hold.status is HoldStatus.confirmed:
        return HoldState.confirmed
    if hold.status is HoldStatus.released:
        return HoldState.released
    is_live = await session.scalar(
        select(Hold.expires_at > func.now()).where(Hold.id == hold.id)
    )
    return HoldState.active if is_live else HoldState.expired


async def to_hold_read(session: AsyncSession, hold: Hold) -> HoldRead:
    return HoldRead(
        id=hold.id,
        event_id=hold.event_id,
        seat_ids=await seat_ids_for_hold(session, hold.id),
        state=await hold_state(session, hold),
        expires_at=hold.expires_at,
        created_at=hold.created_at,
    )


async def assert_event_on_sale(session: AsyncSession, event: Event) -> None:
    if event.status is not EventStatus.published:
        raise EventNotOnSale
    sale_started = await session.scalar(
        select(Event.sale_starts_at <= func.now()).where(Event.id == event.id)
    )
    if not sale_started:
        raise SaleNotStarted


async def assert_seats_belong_to_event(
    session: AsyncSession, event: Event, seat_ids: Sequence[int]
) -> None:
    found = await session.scalars(
        select(Seat.id).where(Seat.id.in_(seat_ids), Seat.event_id == event.id)
    )
    if set(found) != set(seat_ids):
        raise SeatsNotInEvent


async def count_live_seats_for_user(
    session: AsyncSession, user: User, event: Event
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(SeatClaim)
        .join(Hold, Hold.id == SeatClaim.hold_id)
        .where(
            Hold.user_id == user.id,
            Hold.event_id == event.id,
            live_claim_condition(),
        )
    )
    return count or 0


async def create_hold(
    session: AsyncSession,
    user: User,
    event: Event,
    seat_ids: Sequence[int],
    idempotency_key: str | None,
) -> HoldRead:
    if idempotency_key is not None:
        existing = await find_hold_by_key(session, user, idempotency_key)
        if existing is not None:
            return await to_hold_read(session, existing)

    requested = sorted(set(seat_ids))
    await assert_event_on_sale(session, event)
    await assert_seats_belong_to_event(session, event, requested)

    limit = get_settings().max_seats_per_user_per_event
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    already_held = await count_live_seats_for_user(session, user, event)
    if already_held + len(requested) > limit:
        raise SeatLimitExceeded

    hold = Hold(
        event_id=event.id,
        user_id=user.id,
        status=HoldStatus.active,
        expires_at=hold_expiry_expression(),
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(hold)
            await session.flush()
            await acquire_seats(session, hold.id, requested)
        await session.commit()
    except IntegrityError:
        if idempotency_key is not None:
            concurrent = await find_hold_by_key(session, user, idempotency_key)
            if concurrent is not None:
                return await to_hold_read(session, concurrent)
        raise SeatsUnavailable
    except DBAPIError as error:
        raise translate(error)

    await session.refresh(hold)
    await send_seat_update(
        event.id,
        requested,
        SeatAvailability.held,
        expires_in=get_settings().hold_duration_seconds,
    )
    return await to_hold_read(session, hold)


async def load_owned_hold(session: AsyncSession, user: User, hold_id: int) -> Hold:
    hold = await session.scalar(
        select(Hold).where(Hold.id == hold_id).with_for_update()
    )
    if hold is None or hold.user_id != user.id:
        raise HoldNotFound
    return hold


async def release_hold(session: AsyncSession, user: User, hold_id: int) -> None:
    hold = await load_owned_hold(session, user, hold_id)
    if hold.status is not HoldStatus.active:
        raise HoldNotActive
    seat_ids = await seat_ids_for_hold(session, hold.id)
    await session.execute(delete(SeatClaim).where(SeatClaim.hold_id == hold.id))
    hold.status = HoldStatus.released
    try:
        await session.commit()
    except DBAPIError as error:
        raise translate(error)
    await send_seat_update(hold.event_id, seat_ids, SeatAvailability.available)
