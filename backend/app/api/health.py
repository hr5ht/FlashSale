from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import SessionDep

router = APIRouter()


class HealthStatus(BaseModel):
    status: str
    database: str


@router.get("/health", response_model=HealthStatus)
async def health(session: SessionDep) -> HealthStatus:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return HealthStatus(status="ok", database="ok")
