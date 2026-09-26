"""Rules the database enforces itself, even for writes that bypass the services."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Book, Loan, Member
from tests.conftest import isbn13

NOW = datetime(2026, 1, 1, 12)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add_book(db, **overrides) -> Book:
    book = Book(**{"title": "T", "author": "A", "isbn": isbn13(1), "price_cents": 100, "stock": 1, **overrides})
    db.add(book)
    db.commit()
    return book


@pytest.mark.parametrize("field", ["stock", "price_cents"])
def test_book_numbers_cannot_go_negative(db, field):
    book = add_book(db)
    setattr(book, field, -1)
    with pytest.raises(IntegrityError):
        db.commit()


def test_member_cannot_hold_two_open_loans_of_the_same_book(db):
    book = add_book(db, stock=5)
    member = Member(name="M", email="m@example.com", tier="supreme", created_at=NOW)
    db.add(member)
    db.commit()
    open_loan = dict(member_id=member.id, book_id=book.id, borrowed_at=NOW, due_at=NOW)

    db.add(Loan(**open_loan, returned_at=NOW))  # returned loans do not count
    db.add(Loan(**open_loan))
    db.commit()

    db.add(Loan(**open_loan))
    with pytest.raises(IntegrityError):
        db.commit()
