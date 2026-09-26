"""Simultaneous requests competing for the same stock, order or loan.

The shared ``client`` fixture runs on a single in-memory connection, which cannot show a race.
These tests call the services from several threads at once, each with its own session, against
a file-based SQLite database, and check that exactly one request wins.
"""
import threading
from datetime import datetime
from typing import Callable, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.errors import ConflictError
from app.models import Book, Member, MemberTier
from app.schemas import LoanCreate, OrderCreate
from app.services import loans, orders
from tests.conftest import isbn13

NOW = datetime(2026, 1, 1, 12)
THREADS = 8


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


def add_members(session_factory, count: int) -> List[int]:
    with session_factory() as db:
        members = [
            Member(name=f"Member {n}", email=f"member{n}@example.com", tier=MemberTier.SUPREME.value, created_at=NOW)
            for n in range(count)
        ]
        db.add_all(members)
        db.commit()
        return [member.id for member in members]


def add_book(session_factory, stock: int) -> int:
    with session_factory() as db:
        book = Book(title="Contested", author="Someone", isbn=isbn13(1), price_cents=1000, stock=stock)
        db.add(book)
        db.commit()
        return book.id


def stock_of(session_factory, book_id: int) -> int:
    with session_factory() as db:
        return db.get(Book, book_id).stock


def run_at_once(session_factory, calls: List[Callable[[Session], object]]) -> List[str]:
    """Run each call in its own thread and session, all released together.

    Returns one outcome per call: "ok", "conflict", or the name of any other exception.
    """
    barrier = threading.Barrier(len(calls))
    outcomes: List[str] = []

    def worker(call):
        with session_factory() as db:
            barrier.wait()
            try:
                call(db)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")
            except Exception as exc:  # surfaced in the assertion instead of dying with the thread
                outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(outcomes)


def test_last_copy_is_sold_only_once(session_factory):
    member_ids = add_members(session_factory, THREADS)
    book_id = add_book(session_factory, stock=1)
    order = lambda member_id: lambda db: orders.create_order(  # noqa: E731
        db, OrderCreate(member_id=member_id, items=[{"book_id": book_id, "quantity": 1}]), NOW
    )

    outcomes = run_at_once(session_factory, [order(member_id) for member_id in member_ids])

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 0


def test_last_copy_is_lent_only_once(session_factory):
    member_ids = add_members(session_factory, THREADS)
    book_id = add_book(session_factory, stock=1)
    borrow = lambda member_id: lambda db: loans.create_loan(  # noqa: E731
        db, LoanCreate(member_id=member_id, book_id=book_id), NOW
    )

    outcomes = run_at_once(session_factory, [borrow(member_id) for member_id in member_ids])

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 0
