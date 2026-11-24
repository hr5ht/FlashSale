from enum import StrEnum

from pydantic import BaseModel

from app.schemas.event import PriceTierRead


class SeatAvailability(StrEnum):
    available = "available"
    held = "held"
    booked = "booked"


class SeatRead(BaseModel):
    id: int
    number: int
    price_tier_id: int
    status: SeatAvailability


class SeatRowRead(BaseModel):
    label: str
    seats: list[SeatRead]


class SeatSectionRead(BaseModel):
    id: int
    name: str
    rows: list[SeatRowRead]


class SeatMapRead(BaseModel):
    event_id: int
    price_tiers: list[PriceTierRead]
    sections: list[SeatSectionRead]
