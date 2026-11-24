from typing import Annotated

from fastapi import Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.exceptions.auth import InsufficientRole, InvalidToken
from app.exceptions.event import NotEventOwner
from app.models.event import Event
from app.models.user import Role, User
from app.services.auth_service import user_from_access_token
from app.services.event_service import get_event, get_visible_event, is_event_manager

SessionDep = Annotated[AsyncSession, Depends(get_session)]

bearer_scheme = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def get_current_user(credentials: BearerCredentials, session: SessionDep) -> User:
    if credentials is None:
        raise InvalidToken
    return await user_from_access_token(session, credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(credentials: BearerCredentials, session: SessionDep) -> User | None:
    if credentials is None:
        return None
    return await get_current_user(credentials, session)


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def require_organizer(user: CurrentUser) -> User:
    if user.role not in (Role.organizer, Role.admin):
        raise InsufficientRole
    return user


Organizer = Annotated[User, Depends(require_organizer)]


async def managed_event(
    event_id: Annotated[int, Path()], user: CurrentUser, session: SessionDep
) -> Event:
    event = await get_event(session, event_id)
    if not is_event_manager(event, user):
        raise NotEventOwner
    return event


async def visible_event(
    event_id: Annotated[int, Path()], viewer: OptionalUser, session: SessionDep
) -> Event:
    return await get_visible_event(session, event_id, viewer)


ManagedEvent = Annotated[Event, Depends(managed_event)]
VisibleEvent = Annotated[Event, Depends(visible_event)]
