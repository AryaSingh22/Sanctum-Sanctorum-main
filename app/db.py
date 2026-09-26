import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.errors import ConflictError

DATABASE_URL = os.getenv("SANCTUM_DATABASE_URL", "sqlite:///./sanctum.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def commit_or_conflict(db: Session, detail: str) -> None:
    """Commit, turning a unique-constraint violation into ConflictError.

    Services check for duplicates before inserting, but two identical requests can both pass that
    check; the database's unique constraint then rejects the second at commit time.
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(detail) from None
