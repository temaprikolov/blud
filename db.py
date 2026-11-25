from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, false, inspect, text, true
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")

    balance: Mapped[int] = mapped_column(Integer, default=0)
    earned: Mapped[int] = mapped_column(Integer, default=0)
    ref_earned: Mapped[int] = mapped_column(Integer, default=0)

    tasks_done: Mapped[int] = mapped_column(Integer, default=0)
    tasks_skipped: Mapped[int] = mapped_column(Integer, default=0)

    referrer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    ref_counted: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_top_counted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    hide_top: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500))
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="own", server_default="own")
    reward: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserTask(Base):
    __tablename__ = "user_tasks"
    __table_args__ = (UniqueConstraint("user_id", "task_id", name="uq_user_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Channel(Base):

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    check_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_link: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ChannelPass(Base):

    __tablename__ = "channel_passes"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_channel_pass"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChannelMiss(Base):

    __tablename__ = "channel_misses"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_channel_miss"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(300), default="")
    admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Withdrawal(Base):

    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class WithdrawalNotice(Base):

    __tablename__ = "withdrawal_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    withdrawal_id: Mapped[int] = mapped_column(Integer, index=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)


class Check(Base):

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    per_person: Mapped[int] = mapped_column(Integer)
    max_people: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    channels: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    activations: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CheckActivation(Base):
    __tablename__ = "check_activations"
    __table_args__ = (UniqueConstraint("check_id", "user_id", name="uq_check_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FraudEvent(Base):

    __tablename__ = "fraud_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(24))
    points: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OldReferral(Base):

    __tablename__ = "old_referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invited_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Setting(Base):

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


_engine_kwargs ={"connect_args": {"timeout": 30}} if config.DB_URL.startswith("sqlite") else {}
engine = create_async_engine(config.DB_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

if config.DB_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("channels")})
        if "check_enabled" not in cols:
            default = "1" if config.DB_URL.startswith("sqlite") else "TRUE"
            await conn.execute(text(f"ALTER TABLE channels ADD COLUMN check_enabled BOOLEAN NOT NULL DEFAULT {default}"))
        if "is_link" not in cols:
            await conn.execute(text("ALTER TABLE channels ADD COLUMN is_link BOOLEAN NOT NULL DEFAULT " + ("0" if config.DB_URL.startswith("sqlite") else "FALSE")))
        ucols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("users")})
        if "ref_top_counted" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN ref_top_counted BOOLEAN NOT NULL DEFAULT " + ("0" if config.DB_URL.startswith("sqlite") else "FALSE")))
        if "is_premium" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN is_premium BOOLEAN NOT NULL DEFAULT " + ("0" if config.DB_URL.startswith("sqlite") else "FALSE")))
        if "hide_top" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN hide_top BOOLEAN NOT NULL DEFAULT " + ("0" if config.DB_URL.startswith("sqlite") else "FALSE")))
        tcols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("tasks")})
        if "source" not in tcols:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN source VARCHAR(12) NOT NULL DEFAULT 'own'"))
        if "reward" not in tcols:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN reward INTEGER"))
