"""SQLAlchemy models."""
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.db import Base


class User(Base):
    __tablename__ = "users"

    id:           Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email:        Mapped[str]       = mapped_column(String, unique=True, nullable=False, index=True)
    password_hash:Mapped[str]       = mapped_column(String, nullable=False)
    vault_key_enc:Mapped[str]       = mapped_column(Text, nullable=True)   # Fernet key encrypted with master key
    created_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)

    vps_connections: Mapped[list["VpsConnection"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    credentials:     Mapped[list["Credential"]]    = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions:        Mapped[list["ChatSession"]]   = relationship(back_populates="user", cascade="all, delete-orphan")


class VpsConnection(Base):
    __tablename__ = "vps_connections"

    id:          Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:     Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    label:       Mapped[str]       = mapped_column(String, default="My VPS")
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
    vps_synced:   Mapped[bool]      = mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:   Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="credentials")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id:         Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id:    Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    history:    Mapped[list]      = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="sessions")
