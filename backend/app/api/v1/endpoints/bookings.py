from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import CurrentUser, ManagedEvent, SessionDep, VisibleEvent
from app.schemas.booking import BookingRead, HoldCreate, HoldRead, SalesSummary
from app.schemas.pagination import Page, PageParams
from app.services.booking_service import (
    cancel_booking,
    confirm_hold,
    get_booking,
    list_bookings,
)
from app.services.hold_service import create_hold, release_hold
from app.services.sales_service import sales_summary

router = APIRouter(tags=["bookings"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
PageDep = Annotated[PageParams, Query()]


@router.post(
    "/events/{event_id}/holds",
    response_model=HoldRead,
    status_code=status.HTTP_201_CREATED,
)
async def hold_seats(
    payload: HoldCreate,
    event: VisibleEvent,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: IdempotencyKey = None,
) -> HoldRead:
    return await create_hold(session, user, event, payload.seat_ids, idempotency_key)


@router.delete("/holds/{hold_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release(hold_id: int, user: CurrentUser, session: SessionDep) -> None:
    await release_hold(session, user, hold_id)


@router.post("/holds/{hold_id}/confirm", response_model=BookingRead)
async def confirm(
    hold_id: int,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: IdempotencyKey = None,
) -> BookingRead:
    return await confirm_hold(session, user, hold_id, idempotency_key)


@router.get("/bookings", response_model=Page[BookingRead])
async def my_bookings(
    page: PageDep, user: CurrentUser, session: SessionDep
) -> Page[BookingRead]:
    return await list_bookings(session, user, page)


@router.get("/bookings/{booking_id}", response_model=BookingRead)
async def booking_detail(
    booking_id: int, user: CurrentUser, session: SessionDep
) -> BookingRead:
    return await get_booking(session, user, booking_id)


@router.delete("/bookings/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel(booking_id: int, user: CurrentUser, session: SessionDep) -> None:
    await cancel_booking(session, user, booking_id)


@router.get("/events/{event_id}/sales", response_model=SalesSummary)
async def event_sales(event: ManagedEvent, session: SessionDep) -> SalesSummary:
    return await sales_summary(session, event)
