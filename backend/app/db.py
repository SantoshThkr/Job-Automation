"""Database engine/session management (SQLite by default, WAL mode for API + agent processes)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from datetime import datetime

from sqlalchemy import MetaData, Table, create_engine, event, func, inspect, literal, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        settings.ensure_dirs()
        url = settings.db_url
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _sqlite_pragmas)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests and after settings change)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    engine = get_engine()
    _migrate_answer_memory_scopes(engine)
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations(engine)
    _backfill_posted_dates(engine)
    _backfill_posted_precision(engine)
    with session_scope() as s:
        models.AgentState.get(s)


_IDENTIFIER = __import__("re").compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def _migrate_answer_memory_scopes(engine: Engine) -> None:
    """Saved answers used to be unique per question, so an answer given for one employer replaced your general one.
    The table is rebuilt once (rows kept, the database file copied first) so each reuse scope keeps its own answer."""
    from app import models

    insp = inspect(engine)
    if engine.dialect.name != "sqlite" or not insp.has_table("answer_memory"):
        return
    legacy = any(u.get("column_names") == ["normalized_question"] for u in insp.get_unique_constraints("answer_memory"))
    if not legacy:
        return
    _backup_sqlite(engine, "before-saved-answer-scopes")
    table = models.AnswerMemory.__table__

    def default(col):  # the model's own default for a column the old table lacks (or left NULL)
        val = col.default.arg if col.default is not None and getattr(col.default, "is_scalar", False) else ""
        return int(val) if isinstance(val, bool) else val

    with engine.begin() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM answer_memory")).scalar_one()
        conn.execute(text("ALTER TABLE answer_memory RENAME TO answer_memory_legacy"))
        table.create(conn)
        legacy = Table("answer_memory_legacy", MetaData(), autoload_with=conn)
        values = []
        for col in table.columns:
            scalar = col.default is not None and getattr(col.default, "is_scalar", False)
            if col.name not in legacy.c:
                values.append(literal(default(col)))
            else:
                values.append(func.coalesce(legacy.c[col.name], literal(default(col))) if scalar else legacy.c[col.name])
        conn.execute(table.insert().from_select([c.name for c in table.columns], select(*values)))
        after = conn.execute(text("SELECT COUNT(*) FROM answer_memory")).scalar_one()
        if after != before:  # never lose a saved answer: the transaction is rolled back
            raise RuntimeError(f"saved answers migration copied {after} of {before} rows - nothing was changed")
        conn.execute(text("DROP TABLE answer_memory_legacy"))


def _backfill_posted_dates(engine: Engine) -> None:
    """Jobs found before posting dates were recorded keep their listing label ("3 days ago", "3+ weeks ago"). It is
    turned into a date once - counted from when the job was first found (the earliest moment the label can have
    been read, so the date errs towards older, never newer). Nothing else changes; the file is copied first."""
    from app.matching.posting import parse_label

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, posted_text, discovered_at FROM jobs WHERE posted_at IS NULL "
                                 "AND COALESCE(posted_text, '') != '' AND discovered_at IS NOT NULL")).all()
    if not rows:
        return
    _backup_sqlite(engine, "before-posting-dates")
    with engine.begin() as conn:
        for job_id, label, found in rows:
            seen = found if isinstance(found, datetime) else datetime.fromisoformat(str(found))
            posted = parse_label(label, seen=seen)
            if posted is None:
                continue
            conn.execute(text("UPDATE jobs SET posted_at = :at, posted_open_ended = :open, posted_date_only = :day, "
                              "posted_source = 'label', posted_spread_s = :spread WHERE id = :id AND posted_at IS NULL"),
                         {"at": posted.at, "open": int(posted.open_ended), "day": int(posted.date_only),
                          "spread": posted.spread_s, "id": job_id})


def _backfill_posted_precision(engine: Engine) -> None:
    """Dates read from a listing's label before its precision was kept get the label's unit ("1 week ago": a week).
    Nothing else changes; the file is copied first."""
    from app.matching.posting import label_spread

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, posted_text FROM jobs WHERE posted_at IS NOT NULL AND posted_spread_s IS NULL"
                                 " AND posted_source = 'label'")).all()
    if not rows:
        return
    _backup_sqlite(engine, "before-posting-precision")
    with engine.begin() as conn:
        for job_id, label in rows:
            conn.execute(text("UPDATE jobs SET posted_spread_s = :s WHERE id = :id AND posted_spread_s IS NULL"),
                         {"s": label_spread(label or ""), "id": job_id})


def _backup_sqlite(engine: Engine, label: str) -> None:
    """A consistent copy of the database file next to it (owner-only), made before a table is rebuilt."""
    import os
    import sqlite3
    from datetime import date
    from pathlib import Path

    path = engine.url.database
    if not path or path == ":memory:" or not Path(path).is_file():
        return
    src_path = Path(path)
    dest = src_path.with_name(f"{src_path.stem}.{label}-{date.today().isoformat()}{src_path.suffix}")
    if dest.exists():
        return
    src, out = sqlite3.connect(str(src_path)), sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    os.chmod(dest, 0o600)


def _apply_lightweight_migrations(engine: Engine) -> None:
    """Add columns introduced after a database was first created (SQLite-friendly)."""
    from app import models  # noqa: F401

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                # identifiers and types come from the model definitions (never from a request); checked anyway
                # because DDL cannot take bound parameters
                if not (_IDENTIFIER.fullmatch(table.name) and _IDENTIFIER.fullmatch(col.name)):
                    raise RuntimeError(f"unexpected identifier in schema: {table.name}.{col.name}")
                coltype = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        val = int(val)
                    default = f" DEFAULT {val!r}" if not isinstance(val, str) else f" DEFAULT '{val}'"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}{default}'))
