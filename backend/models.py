"""SQLAlchemy models."""
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from backend.db import Base
from backend.embeddings import EMBED_DIM


class User(Base):
    __tablename__ = "users"

    id:           Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email:        Mapped[str]       = mapped_column(String, unique=True, nullable=False, index=True)
    password_hash:Mapped[str]       = mapped_column(String, nullable=False)
    vault_key_enc:Mapped[str]       = mapped_column(Text, nullable=True)   # Fernet key encrypted with master key
    email_verified:Mapped[bool]     = mapped_column(Boolean, default=False)
    # Subscription
    subscription_status:    Mapped[str] = mapped_column(String, default="none")  # none|trialing|active|cancelled|expired|past_due
    paypal_subscription_id: Mapped[Optional[str]]      = mapped_column(String, nullable=True)
    trial_ends_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    current_period_end:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_reminder_sent:    Mapped[bool]               = mapped_column(Boolean, default=False)
    # LLM supervisor choice (Claude is the tuned default; others experimental)
    llm_provider: Mapped[str]           = mapped_column(String, default="anthropic")
    llm_model:    Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)

    vps_connections: Mapped[list["VpsConnection"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    credentials:     Mapped[list["Credential"]]    = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions:        Mapped[list["ChatSession"]]   = relationship(back_populates="user", cascade="all, delete-orphan")
    tasks:           Mapped[list["Task"]]          = relationship(back_populates="user", cascade="all, delete-orphan")
    memory_facts:    Mapped[list["MemoryFact"]]    = relationship(back_populates="user", cascade="all, delete-orphan")


class VpsConnection(Base):
    __tablename__ = "vps_connections"

    id:          Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:     Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    label:       Mapped[str]       = mapped_column(String, default="My VPS")
    agent_type:  Mapped[str]       = mapped_column(String, default="openclaw")  # openclaw | hermes
    host:        Mapped[str]       = mapped_column(String, nullable=False)
    port:        Mapped[int]       = mapped_column(Integer, default=22)
    username:    Mapped[str]       = mapped_column(String, nullable=False)
    password_enc:Mapped[str]       = mapped_column(Text, nullable=True)    # Fernet-encrypted
    is_default:  Mapped[bool]      = mapped_column(Boolean, default=True)
    created_at:  Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="vps_connections")


class Credential(Base):
    __tablename__ = "credentials"

    id:           Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:      Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name:         Mapped[str]       = mapped_column(String, nullable=False)       # e.g. "hostgator_cpanel"
    username:     Mapped[str]       = mapped_column(String, default="")
    password_enc: Mapped[str]       = mapped_column(Text, nullable=False)         # Fernet-encrypted
    notes:        Mapped[str]       = mapped_column(String, default="")
    filename:     Mapped[Optional[str]] = mapped_column(String, nullable=True)  # set → file credential; password_enc holds the file body
    vps_synced:   Mapped[bool]      = mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="credentials")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id:         Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:    Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    vps_id:     Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("vps_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    history:    Mapped[list]      = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="sessions")


class Task(Base):
    """A unit of work the agent performed — shown in the Activity panel."""
    __tablename__ = "tasks"

    id:           Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:      Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    vps_id:     Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("vps_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    title:        Mapped[str]       = mapped_column(String, nullable=False)
    status:       Mapped[str]       = mapped_column(String, default="in_progress")   # in_progress | done | failed
    outcome:      Mapped[str]       = mapped_column(Text, default="")
    embedding:    Mapped[Optional[list]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="tasks")


class MemoryFact(Base):
    """A durable fact Claude remembers across sessions (survives compression)."""
    __tablename__ = "memory_facts"

    id:         Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:    Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    vps_id:     Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("vps_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    key:        Mapped[str]       = mapped_column(String, nullable=False)
    value:      Mapped[str]       = mapped_column(Text, nullable=False)
    category:   Mapped[str]       = mapped_column(String, default="general")
    embedding:  Mapped[Optional[list]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="memory_facts")


class AuditLog(Base):
    """Append-only forensic record of every command run on a user's server.
    Non-sensitive metadata is plaintext; the command/output detail is redacted
    AND encrypted with the user's vault key (detail_enc)."""
    __tablename__ = "audit_log"

    id:          Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:     Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    vps_id:     Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("vps_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at:  Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, index=True)
    action_type: Mapped[str]       = mapped_column(String, default="")   # vps_cmd | vps_write | tui_input
    vps_host:    Mapped[str]       = mapped_column(String, default="")
    headline:    Mapped[str]       = mapped_column(String, default="")   # redacted plain-English
    status:      Mapped[str]       = mapped_column(String, default="ok") # ok | failed | cancelled
    detail_enc:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # encrypted redacted JSON
