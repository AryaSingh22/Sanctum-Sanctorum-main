import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.errors import ConflictError

DATABASE_URL = os.getenv("SANCTUM_DATABASE_URL", "sqlite:///./sanctum.db")


def normalize_database_url(url: str) -> str:
    """Point bare Postgres URLs at the psycopg (v3) driver.

    Hosts such as Neon, Supabase and Render hand out ``postgres://`` or ``postgresql://`` URLs.
    SQLAlchemy rejects the first and maps the second to psycopg2, which is not installed.
    """
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme) :]
    return url


def create_database_engine(url: str) -> Engine:
    url = normalize_database_url(url)
    if url.startswith("sqlite"):
        # FastAPI runs sync endpoints in a thread pool, so a connection may move between threads.
        return create_engine(url, connect_args={"check_same_thread": False})
    # Hosted databases close idle connections (Neon suspends when idle): check each one on checkout.
    return create_engine(url, pool_pre_ping=True)


engine = create_database_engine(DATABASE_URL)
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
