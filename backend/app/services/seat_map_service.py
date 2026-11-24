from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Hold, HoldStatus, SeatClaim
from app.models.event import Event, Seat, Section
from app.schemas.event import PriceTierRead
from app.schemas.seat_map import (
    SeatAvailability,
    SeatMapRead,
    SeatRead,
    SeatRowRead,
    SeatSectionRead,
)
from app.services.seat_acquisition import live_claim_condition


async def seat_availability(
    session: AsyncSession, event_id: int
) -> dict[int, SeatAvailability]:
    rows = await session.execute(
        select(SeatClaim.seat_id, Hold.status)
        .join(Hold, Hold.id == SeatClaim.hold_id)
        .join(Seat, Seat.id == SeatClaim.seat_id)
        .where(Seat.event_id == event_id, live_claim_condition())
    )
    return {
        seat_id: (
            SeatAvailability.booked
            if status is HoldStatus.confirmed
            else SeatAvailability.held
        )
        for seat_id, status in rows.all()
    }


async def get_seat_map(session: AsyncSession, event: Event) -> SeatMapRead:
    availability = await seat_availability(session, event.id)
    rows = await session.execute(
        select(Section.id, Section.name, Seat)
        .join(Section, Section.id == Seat.section_id)
        .where(Seat.event_id == event.id)
        .order_by(Section.id, Seat.row_label, Seat.seat_number)
    )

    sections: dict[int, SeatSectionRead] = {}
    row_index: dict[tuple[int, str], SeatRowRead] = {}
    for section_id, section_name, seat in rows.all():
        section = sections.get(section_id)
        if section is None:
            section = SeatSectionRead(id=section_id, name=section_name, rows=[])
            sections[section_id] = section
        seat_row = row_index.get((section_id, seat.row_label))
        if seat_row is None:
            seat_row = SeatRowRead(label=seat.row_label, seats=[])
            row_index[(section_id, seat.row_label)] = seat_row
            section.rows.append(seat_row)
        seat_row.seats.append(
            SeatRead(
                id=seat.id,
                number=seat.seat_number,
                price_tier_id=seat.price_tier_id,
                status=availability.get(seat.id, SeatAvailability.available),
            )
        )

    return SeatMapRead(
        event_id=event.id,
        price_tiers=[PriceTierRead.model_validate(tier) for tier in event.price_tiers],
        sections=list(sections.values()),
    )
