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
from app.schemas import BookCreate, LoanCreate, MemberCreate, OrderCreate
from app.services import books, loans, members, orders
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


def add_book(session_factory, stock: int, seed: int = 1) -> int:
    with session_factory() as db:
        book = Book(title=f"Contested {seed}", author="Someone", isbn=isbn13(seed), price_cents=1000, stock=stock)
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


def test_orders_naming_the_same_books_in_opposite_order_all_go_through(session_factory):
    member_ids = add_members(session_factory, THREADS)
    first, second = add_book(session_factory, stock=100, seed=1), add_book(session_factory, stock=100, seed=2)
    order = lambda member_id, book_ids: lambda db: orders.create_order(  # noqa: E731
        db, OrderCreate(member_id=member_id, items=[{"book_id": b, "quantity": 1} for b in book_ids]), NOW
    )
    calls = [order(member_id, [first, second] if n % 2 else [second, first]) for n, member_id in enumerate(member_ids)]

    outcomes = run_at_once(session_factory, calls)

    assert outcomes == ["ok"] * THREADS
    assert stock_of(session_factory, first) == stock_of(session_factory, second) == 100 - THREADS


def place_order(session_factory, member_id: int, book_id: int, quantity: int) -> int:
    with session_factory() as db:
        body = OrderCreate(member_id=member_id, items=[{"book_id": book_id, "quantity": quantity}])
        return orders.create_order(db, body, NOW).id


def order_status(session_factory, order_id: int) -> str:
    with session_factory() as db:
        return orders.get_order(db, order_id).status


def test_order_is_cancelled_only_once(session_factory):
    [member_id] = add_members(session_factory, 1)
    book_id = add_book(session_factory, stock=10)
    order_id = place_order(session_factory, member_id, book_id, quantity=3)

    outcomes = run_at_once(session_factory, [lambda db: orders.cancel_order(db, order_id)] * THREADS)

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 10


def test_order_is_either_paid_or_cancelled_never_both(session_factory):
    [member_id] = add_members(session_factory, 1)
    book_id = add_book(session_factory, stock=10)
    order_id = place_order(session_factory, member_id, book_id, quantity=3)
    pay = lambda db: orders.pay_order(db, order_id)  # noqa: E731
    cancel = lambda db: orders.cancel_order(db, order_id)  # noqa: E731

    outcomes = run_at_once(session_factory, [pay, cancel] * (THREADS // 2))

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    expected_stock = {"paid": 7, "cancelled": 10}
    assert stock_of(session_factory, book_id) == expected_stock[order_status(session_factory, order_id)]


def test_loan_is_returned_only_once(session_factory):
    [member_id] = add_members(session_factory, 1)
    book_id = add_book(session_factory, stock=5)
    with session_factory() as db:
        loan_id = loans.create_loan(db, LoanCreate(member_id=member_id, book_id=book_id), NOW).id

    outcomes = run_at_once(session_factory, [lambda db: loans.return_loan(db, loan_id, NOW)] * THREADS)

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 5


def test_same_book_is_lent_to_a_member_only_once(session_factory):
    [member_id] = add_members(session_factory, 1)
    book_id = add_book(session_factory, stock=10)
    borrow = lambda db: loans.create_loan(db, LoanCreate(member_id=member_id, book_id=book_id), NOW)  # noqa: E731

    outcomes = run_at_once(session_factory, [borrow] * THREADS)

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 9


def test_duplicate_isbn_is_a_conflict_not_a_crash(session_factory):
    body = BookCreate(title="Twin", author="Someone", isbn=isbn13(2), price_cents=100, stock=1)

    outcomes = run_at_once(session_factory, [lambda db: books.create_book(db, body)] * THREADS)

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]


def test_duplicate_email_is_a_conflict_not_a_crash(session_factory):
    body = MemberCreate(name="Twin", email="twin@example.com")

    outcomes = run_at_once(session_factory, [lambda db: members.create_member(db, body, NOW)] * THREADS)

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]


def test_last_copy_is_lent_only_once(session_factory):
    member_ids = add_members(session_factory, THREADS)
    book_id = add_book(session_factory, stock=1)
    borrow = lambda member_id: lambda db: loans.create_loan(  # noqa: E731
        db, LoanCreate(member_id=member_id, book_id=book_id), NOW
    )

    outcomes = run_at_once(session_factory, [borrow(member_id) for member_id in member_ids])

    assert outcomes == ["conflict"] * (THREADS - 1) + ["ok"]
    assert stock_of(session_factory, book_id) == 0
