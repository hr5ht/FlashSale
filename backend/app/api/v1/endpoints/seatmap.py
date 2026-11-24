from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.dependencies import SessionDep
from app.exceptions.auth import InvalidToken
from app.exceptions.event import EventNotFound
from app.services import seat_broadcaster
from app.services.auth_service import user_from_access_token
from app.services.event_service import get_visible_event
from app.services.seat_map_service import get_seat_map

router = APIRouter()


@router.websocket("/events/{event_id}/seatmap/ws")
async def seat_map_socket(
    websocket: WebSocket,
    event_id: int,
    session: SessionDep,
    token: str | None = None,
) -> None:
    await websocket.accept()
    try:
        viewer = await user_from_access_token(session, token) if token else None
        event = await get_visible_event(session, event_id, viewer)
        seat_map = await get_seat_map(session, event)
    except InvalidToken:
        await websocket.close(code=4401)
        return
    except EventNotFound:
        await websocket.close(code=4404)
        return
    await session.commit()

    seat_broadcaster.add(event_id, websocket)
    try:
        await websocket.send_json(seat_map.model_dump(mode="json"))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        seat_broadcaster.remove(event_id, websocket)
