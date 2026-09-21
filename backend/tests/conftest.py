import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://mcp_ai:mcp_ai@db:5432/mcp_ai")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.main import app
from app.models import ChatSession


@pytest.fixture(autouse=True)
def clean_database():
    with SessionLocal() as db:
        existing_ids = set(db.scalars(select(ChatSession.id)))
    yield
    with SessionLocal() as db:
        statement = delete(ChatSession)
        if existing_ids:
            statement = statement.where(ChatSession.id.not_in(existing_ids))
        db.execute(statement)
        db.commit()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
