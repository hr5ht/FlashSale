from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ManagedEvent, Organizer, SessionDep, VisibleEvent
from app.models.event import EventStatus
from app.schemas.event import (
    EventCreate,
    EventDetail,
    EventQuery,
    EventSummary,
    EventUpdate,
)
from app.schemas.pagination import Page
from app.schemas.seat_map import SeatMapRead
from app.services.event_service import (
    create_event,
    list_events,
    publish_event,
    update_event,
)
from app.services.seat_map_service import get_seat_map

router = APIRouter(prefix="/events", tags=["events"])

QueryDep = Annotated[EventQuery, Query()]


@router.post("", response_model=EventDetail, status_code=status.HTTP_201_CREATED)
async def create(
    payload: EventCreate, organizer: Organizer, session: SessionDep
) -> EventDetail:
    event = await create_event(session, organizer, payload)
    return EventDetail.model_validate(event)


@router.get("", response_model=Page[EventSummary])
async def list_published(query: QueryDep, session: SessionDep) -> Page[EventSummary]:
    return await list_events(session, query, status=EventStatus.published)


@router.get("/mine", response_model=Page[EventSummary])
async def list_mine(
    query: QueryDep, organizer: Organizer, session: SessionDep
) -> Page[EventSummary]:
    return await list_events(session, query, organizer=organizer)


@router.get("/{event_id}", response_model=EventDetail)
async def detail(event: VisibleEvent) -> EventDetail:
    return EventDetail.model_validate(event)


@router.patch("/{event_id}", response_model=EventDetail)
async def update(
    payload: EventUpdate, event: ManagedEvent, session: SessionDep
) -> EventDetail:
    updated = await update_event(session, event, payload)
    return EventDetail.model_validate(updated)


@router.post("/{event_id}/publish", response_model=EventDetail)
async def publish(event: ManagedEvent, session: SessionDep) -> EventDetail:
    published = await publish_event(session, event)
    return EventDetail.model_validate(published)


@router.get("/{event_id}/seatmap", response_model=SeatMapRead)
async def seat_map(event: VisibleEvent, session: SessionDep) -> SeatMapRead:
    return await get_seat_map(session, event)
