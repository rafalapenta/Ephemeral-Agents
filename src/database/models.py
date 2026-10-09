from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("idx_agents_macro_domain", "macro_domain"),
        Index("idx_agents_squad", "squad"),
        Index("idx_agents_is_active", "is_active"),
    )

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    macro_domain: Mapped[str] = mapped_column(String, nullable=False)
    squad: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    system_prompt_path: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_hooks: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    tool_links: Mapped[list[AgentTool]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    skill_links: Mapped[list[DirectorSkill]] = relationship(
        back_populates="director",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (
        CheckConstraint(
            "tool_type IN ('mcp_server', 'fastmcp', 'python_script', 'webhook')",
            name="ck_tools_tool_type",
        ),
        Index("idx_tools_type", "tool_type"),
    )

    tool_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_type: Mapped[str] = mapped_column(String, nullable=False)
    connection_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    agent_links: Mapped[list[AgentTool]] = relationship(
        back_populates="tool",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AgentTool(Base):
    __tablename__ = "agent_tools"
    __table_args__ = (
        Index("idx_agent_tools_agent_id", "agent_id"),
        Index("idx_agent_tools_tool_id", "tool_id"),
    )

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.tool_id", ondelete="CASCADE"), primary_key=True
    )
    permissions_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    agent: Mapped[Agent] = relationship(back_populates="tool_links")
    tool: Mapped[Tool] = relationship(back_populates="agent_links")


class SkillCatalog(Base):
    __tablename__ = "skills_catalog"
    __table_args__ = (
        Index("idx_skills_catalog_source", "source"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=800, server_default="800"
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="local", server_default="local"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )

    director_links: Mapped[list[DirectorSkill]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DirectorSkill(Base):
    __tablename__ = "director_skills"
    __table_args__ = (
        Index("idx_director_skills_director_id", "director_id"),
        Index("idx_director_skills_skill_id", "skill_id"),
    )

    director_id: Mapped[str] = mapped_column(
        ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        ForeignKey("skills_catalog.id", ondelete="CASCADE"), primary_key=True
    )
    is_core: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    load_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    director: Mapped[Agent] = relationship(back_populates="skill_links")
    skill: Mapped[SkillCatalog] = relationship(back_populates="director_links")


class SkillUsage(Base):
    __tablename__ = "skill_usage"
    __table_args__ = (
        Index("idx_skill_usage_skill_id", "skill_id"),
        Index("idx_skill_usage_director_id", "director_id"),
        Index("idx_skill_usage_task_id", "task_id"),
        Index("idx_skill_usage_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    skill_id: Mapped[str | None] = mapped_column(
        ForeignKey("skills_catalog.id", ondelete="SET NULL"), nullable=True
    )
    director_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    is_ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adherence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    outcome: Mapped[str] = mapped_column(String, nullable=False)  # success | failure | escalated | missing_skill | limit_exceeded
    gates_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    note: Mapped[str] = mapped_column(String(280), nullable=False, default="")


