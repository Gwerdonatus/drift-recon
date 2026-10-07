from __future__ import annotations

import os
from decimal import Decimal
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import Base, get_db
from app.main import create_app
from app.models import BankStatement, Transaction, TransactionStatus

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://recon_user:testpassword@localhost:5432/recon_test",
)


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    async with test_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        yield session
        await session.close()
        await conn.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test_key_1234567890abcdef"},
    ) as c:
        yield c

    app.dependency_overrides.clear()


def make_transaction(
    external_id: str = "TXN-001",
    source: str = "test_erp",
    amount: str = "1000.00",
    reference: str = "REF-001",
    transaction_date: str = "2024-01-15",
    **kwargs,
) -> Transaction:
    from datetime import date
    import uuid

    return Transaction(
        id=uuid.uuid4(),
        external_id=external_id,
        source=source,
        transaction_date=date.fromisoformat(transaction_date),
        amount=Decimal(amount),
        currency="USD",
        reference=reference,
        description=f"Payment {external_id}",
        status=TransactionStatus.PENDING,
        **kwargs,
    )


def make_bank_entry(
    external_id: str = "BANK-001",
    bank_name: str = "test_bank",
    amount: str = "1000.00",
    reference: str = "REF-001",
    value_date: str = "2024-01-15",
    **kwargs,
) -> BankStatement:
    from datetime import date
    import uuid

    return BankStatement(
        id=uuid.uuid4(),
        external_id=external_id,
        bank_name=bank_name,
        value_date=date.fromisoformat(value_date),
        amount=Decimal(amount),
        currency="USD",
        reference=reference,
        description=f"Bank payment {external_id}",
        status=TransactionStatus.PENDING,
        **kwargs,
    )
