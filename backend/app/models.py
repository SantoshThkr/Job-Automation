"""ORM models: jobs, applications, questions/answers, timeline events, runs and agent state."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not keep tz info)."""
    return datetime.now(UTC).replace(tzinfo=None)


class JobStatus(StrEnum):
    NEW = "NEW"
    SKIPPED = "SKIPPED"
    REVIEW_PENDING = "REVIEW_PENDING"  # matcher was unsure: user decides apply/skip
    QUEUED = "QUEUED"  # will be applied to on the next pass
    APPLYING = "APPLYING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # review mode: form filled, waiting for the user
    APPLIED = "APPLIED"  # submission confirmed
    DRY_RUN = "DRY_RUN"  # dry run completed, nothing submitted
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    FAILED = "FAILED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    DUPLICATE = "DUPLICATE"


TERMINAL_STATUSES = {
    JobStatus.APPLIED,
    JobStatus.ALREADY_APPLIED,
    JobStatus.DUPLICATE,
    JobStatus.SKIPPED,
}

# Statuses that mean "do not touch again automatically".
NO_AUTO_RETRY_STATUSES = TERMINAL_STATUSES | {
    JobStatus.MANUAL_ACTION_REQUIRED,
    JobStatus.REVIEW_PENDING,
    JobStatus.DRY_RUN,
    JobStatus.FAILED,
}


class Decision(StrEnum):
    APPLY = "APPLY"
    SKIP = "SKIP"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class AgentMode(StrEnum):
    DRY_RUN = "dry_run"
    REVIEW = "review"
    AUTO = "auto"


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class ActualState(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_LOGIN = "WAITING_LOGIN"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_MANUAL = "WAITING_MANUAL"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("portal", "external_id", name="uq_job_portal_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portal: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    company: Mapped[str] = mapped_column(String(512), default="")
    location: Mapped[str] = mapped_column(String(512), default="")
    experience_text: Mapped[str] = mapped_column(String(128), default="")
    salary_text: Mapped[str] = mapped_column(String(128), default="")
    posted_text: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    apply_type: Mapped[str] = mapped_column(String(32), default="unknown")  # portal|external|unknown
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(512), index=True, default="")

    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    match_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resume_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=JobStatus.NEW, index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    search_query: Mapped[str] = mapped_column(String(255), default="")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    application: Mapped[Application | None] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="ApplicationEvent.ts"
    )


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.APPLYING, index=True)
    mode: Mapped[str] = mapped_column(String(16), default=AgentMode.DRY_RUN)
    apply_target: Mapped[str] = mapped_column(String(32), default="portal")  # portal|external
    ats_platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_step: Mapped[str] = mapped_column(String(128), default="")
    submit_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmation_text: Mapped[str] = mapped_column(Text, default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    manual_action_reason: Mapped[str] = mapped_column(Text, default="")
    review_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # approve|reject
    final_url: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="application")
    answers: Mapped[list[QuestionAnswer]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="QuestionAnswer.id"
    )


class QuestionAnswer(Base):
    __tablename__ = "question_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    normalized_question: Mapped[str] = mapped_column(Text, default="", index=True)
    field_type: Mapped[str] = mapped_column(String(32), default="text")
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="")  # profile|rule|memory|ai|user|none
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="answers")


class ApplicationEvent(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    screenshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job | None] = relationship(back_populates="events")


class AnswerMemory(Base):
    """Answers the user supplied for questions the assistant could not answer confidently."""

    __tablename__ = "answer_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_question: Mapped[str] = mapped_column(Text, unique=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    portals: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentState(Base):
    """Singleton row used as the control channel between dashboard/API and the agent process."""

    __tablename__ = "agent_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    desired_state: Mapped[str] = mapped_column(String(16), default=DesiredState.STOPPED)
    actual_state: Mapped[str] = mapped_column(String(32), default=ActualState.IDLE)
    mode: Mapped[str] = mapped_column(String(16), default=AgentMode.DRY_RUN)
    portals: Mapped[list[str]] = mapped_column(JSON, default=list)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_activity: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    pending_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Human-in-the-loop gate: review approval, login, CAPTCHA/OTP, manual form completion
    gate_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gate_reason: Mapped[str] = mapped_column(Text, default="")
    gate_response: Mapped[str | None] = mapped_column(String(16), nullable=True)  # approve|reject
    gate_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    repeat_minutes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @classmethod
    def get(cls, session: Session) -> AgentState:
        state = session.get(cls, 1)
        if state is None:
            from app.config import get_settings

            mode = get_settings().default_mode
            state = cls(id=1, mode=mode if mode in {m.value for m in AgentMode} else AgentMode.DRY_RUN)
            session.add(state)
            session.flush()
        return state
