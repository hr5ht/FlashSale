from fastapi import APIRouter

from app.api.v1.endpoints import auth, bookings, events, seatmap

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(events.router)
api_router.include_router(bookings.router)
api_router.include_router(seatmap.router)
