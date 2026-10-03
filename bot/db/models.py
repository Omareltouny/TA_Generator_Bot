"""SQLAlchemy 2.x models. Mirrors spec section 5. JSON type is JSONB on Postgres."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    code: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str | None] = mapped_column(String(300))
    term: Mapped[str | None] = mapped_column(String(100))
    language_hint: Mapped[str | None] = mapped_column(String(100))
    outline_json: Mapped[dict | None] = mapped_column(JSONType)
    logo_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    template_id: Mapped[int | None] = mapped_column(Integer)
    plan_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # outline|slides|reference|logo
    filename: Mapped[str] = mapped_column(String(300))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanItem(Base):
    __tablename__ = "plan_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(20))  # lab|tutorial|assignment
    week: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300))
    topics: Mapped[list | None] = mapped_column(JSONType)
    due_date: Mapped[str | None] = mapped_column(String(50))
    weight: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(20), default="outline")  # outline|inferred|user
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned|generating|draft|approved|failed
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ItemVersion(Base):
    __tablename__ = "item_versions"
    __table_args__ = (UniqueConstraint("item_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plan_items.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    student_md: Mapped[str] = mapped_column(Text)
    key_md: Mapped[str] = mapped_column(Text)
    student_docx: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_docx: Mapped[bytes | None] = mapped_column(LargeBinary)
    tex: Mapped[bytes | None] = mapped_column(LargeBinary)
    llm_provider: Mapped[str | None] = mapped_column(String(50))
    llm_model: Mapped[str | None] = mapped_column(String(100))
    feedback_rule_ids: Mapped[list | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FeedbackMessage(Base):
    __tablename__ = "feedback_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("plan_items.id", ondelete="SET NULL"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    raw_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FeedbackRule(Base):
    __tablename__ = "feedback_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("plan_items.id", ondelete="CASCADE"))
    rule_text: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("feedback_messages.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("feedback_rules.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed|cancelled
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Template(Base):
    """User-uploaded .docx used as the pandoc reference document."""
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserState(Base):
    """Per-user conversation state (current course + mode) so nothing lives in process memory."""
    __tablename__ = "user_state"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    course_id: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(String(30))
    data: Mapped[dict | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
