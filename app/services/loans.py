"""Library loan operations: borrowing and returning books."""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError
from app.models import Book, Loan, Member, MemberTier
from app.schemas import LoanCreate, LoanOut, LoanStatus
from app.services.books import get_book, return_stock, take_stock
from app.services.members import ensure_can_access_restricted, get_member

# Maximum concurrent unreturned loans per tier (None = unlimited).
TIER_LOAN_LIMIT: Dict[str, Optional[int]] = {
    MemberTier.APPRENTICE.value: 1,
    MemberTier.ADEPT.value: 3,
    MemberTier.MASTER.value: 5,
    MemberTier.SUPREME.value: None,
}

LOAN_PERIOD = timedelta(days=14)
LATE_FEE_PER_DAY_CENTS = 25
ONE_DAY = timedelta(days=1)


def loan_status(loan: Loan, now: datetime) -> LoanStatus:
    """``returned`` if returned; else ``overdue`` if now > due_at; else ``active``."""
    if loan.returned_at is not None:
        return "returned"
    return "overdue" if loan.is_overdue(now) else "active"


def to_loan_out(loan: Loan, now: datetime) -> LoanOut:
    """Serialize a loan, computing its status at read time."""
    return LoanOut(
        id=loan.id,
        member_id=loan.member_id,
        book_id=loan.book_id,
        borrowed_at=loan.borrowed_at,
        due_at=loan.due_at,
        returned_at=loan.returned_at,
        late_fee_cents=loan.late_fee_cents,
        status=loan_status(loan, now),
    )


def calculate_late_fee(due_at: datetime, returned_at: datetime, price_cents: int) -> int:
    """25 cents per started day late (any partial day counts), capped at the book's price; 0 if not late."""
    if returned_at <= due_at:
        return 0
    full_days, remainder = divmod(returned_at - due_at, ONE_DAY)
    days_late = full_days + (1 if remainder else 0)
    return min(days_late * LATE_FEE_PER_DAY_CENTS, price_cents)


def create_loan(db: Session, data: LoanCreate, now: datetime) -> LoanOut:
    """Borrow a book for 14 days.

    Checks, in order:
    1. 404 member not found; 404 book not found
    2. 403 book restricted and member tier below master
    3. 409 member has any overdue loan
    4. 409 member already has an unreturned loan of this book
    5. 409 member is at their tier's loan limit
    6. 409 book is out of stock
    On success: borrowed_at = now, due_at = now + 14 days, returned_at None,
    late_fee_cents 0, and stock is decremented by one.
    """
    member = get_member(db, data.member_id)
    book = get_book(db, data.book_id)
    if book.restricted:
        ensure_can_access_restricted(member)
    ensure_member_can_borrow(db, member, book, now)
    if not take_stock(db, book.id, 1):
        raise ConflictError("Book is out of stock")

    loan = Loan(member_id=member.id, book_id=book.id, borrowed_at=now, due_at=now + LOAN_PERIOD, late_fee_cents=0)
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return to_loan_out(loan, now)


def ensure_member_can_borrow(db: Session, member: Member, book: Book, now: datetime) -> None:
    """Raise 409 if the member has an overdue loan, already holds this book, or is at their tier limit."""
    open_loans = db.scalars(select(Loan).where(Loan.member_id == member.id, Loan.returned_at.is_(None))).all()
    if any(loan.is_overdue(now) for loan in open_loans):
        raise ConflictError("Member has an overdue loan")
    if any(loan.book_id == book.id for loan in open_loans):
        raise ConflictError("Member already has this book on loan")
    limit = TIER_LOAN_LIMIT[member.tier]
    if limit is not None and len(open_loans) >= limit:
        raise ConflictError(f"Loan limit of {limit} reached for tier '{member.tier}'")


def load_loan(db: Session, loan_id: int) -> Loan:
    """Return the Loan row by id, or raise 404."""
    loan = db.get(Loan, loan_id)
    if loan is None:
        raise NotFoundError("Loan not found")
    return loan


def get_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a loan by id, or raise 404."""
    return to_loan_out(load_loan(db, loan_id), now)


def return_loan(db: Session, loan_id: int, now: datetime) -> LoanOut:
    """Return a borrowed book.

    Rules: 404 if missing; 409 if already returned. Sets returned_at = now, restores one copy
    of stock and charges a late fee (see ``calculate_late_fee``).
    """
    loan = load_loan(db, loan_id)
    if loan.returned_at is not None:
        raise ConflictError("Loan has already been returned")

    loan.returned_at = now
    loan.late_fee_cents = calculate_late_fee(loan.due_at, now, loan.book.price_cents)
    return_stock(db, loan.book_id, 1)
    db.commit()
    db.refresh(loan)
    return to_loan_out(loan, now)


def list_member_loans(
    db: Session, member_id: int, now: datetime, status: Optional[LoanStatus] = None
) -> List[LoanOut]:
    """A member's loans ordered by id, optionally filtered by computed status; 404 if member missing."""
    get_member(db, member_id)
    loans = db.scalars(select(Loan).where(Loan.member_id == member_id).order_by(Loan.id))
    results = [to_loan_out(loan, now) for loan in loans]
    if status is None:
        return results
    return [loan for loan in results if loan.status == status]
