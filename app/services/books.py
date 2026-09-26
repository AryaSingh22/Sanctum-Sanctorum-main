"""Book catalogue operations."""
from typing import Dict, List, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.db import commit_or_conflict
from app.errors import ConflictError, NotFoundError
from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate

DUPLICATE_ISBN = "A book with this ISBN already exists"

# ORDER BY clause for each accepted ``sort`` value. Titles compare case-insensitively so SQLite
# (uppercase first by default) and Postgres return the same order.
SORT_ORDER = {
    "title": func.lower(Book.title).asc(),
    "-title": func.lower(Book.title).desc(),
    "price": Book.price_cents.asc(),
    "-price": Book.price_cents.desc(),
}


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.

    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    if db.scalar(select(Book.id).where(Book.isbn == data.isbn)) is not None:
        raise ConflictError(DUPLICATE_ISBN)
    book = Book(**data.model_dump())
    db.add(book)
    commit_or_conflict(db, DUPLICATE_ISBN)
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise NotFoundError("Book not found")
    return book


def get_books(db: Session, book_ids: List[int]) -> Dict[int, Book]:
    """Load several books in one query, keyed by id; 404 if any of them is missing."""
    books = {book.id: book for book in db.scalars(select(Book).where(Book.id.in_(book_ids)))}
    missing = [str(book_id) for book_id in book_ids if book_id not in books]
    if missing:
        raise NotFoundError(f"Book not found: {', '.join(missing)}")
    return books


def take_stock(db: Session, book_id: int, quantity: int) -> bool:
    """Take ``quantity`` copies if that many are left; return False, changing nothing, if not.

    The check and the decrement are a single UPDATE, so two requests can never both take the
    last copy: the database applies them one after the other and the second matches no row.
    """
    result = db.execute(
        update(Book)
        .where(Book.id == book_id, Book.stock >= quantity)
        .values(stock=Book.stock - quantity)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount == 1


def return_stock(db: Session, book_id: int, quantity: int) -> None:
    """Put ``quantity`` copies back, adding in the database so simultaneous returns all count."""
    db.execute(
        update(Book)
        .where(Book.id == book_id)
        .values(stock=Book.stock + quantity)
        .execution_options(synchronize_session="fetch")
    )


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing."""
    book = get_book(db, book_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book


def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    """Search the catalogue.

    Rules:
    - ``q`` matches title OR author, case-insensitive substring.
    - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
      default order is id ascending.
    - ``total`` counts all matches before ``limit``/``offset`` are applied.
    """
    query = select(Book)
    if q:
        query = query.where(
            or_(Book.title.icontains(q, autoescape=True), Book.author.icontains(q, autoescape=True))
        )
    if restricted is not None:
        query = query.where(Book.restricted == restricted)
    if min_price is not None:
        query = query.where(Book.price_cents >= min_price)
    if max_price is not None:
        query = query.where(Book.price_cents <= max_price)

    total = db.scalar(select(func.count()).select_from(query.subquery()))

    if sort:
        query = query.order_by(SORT_ORDER[sort])
    # order_by() appends, so id is the tie-breaker after a sort key, or the whole order without one.
    books = db.scalars(query.order_by(Book.id.asc()).limit(limit).offset(offset)).all()

    return BookPage(items=books, total=total, limit=limit, offset=offset)
