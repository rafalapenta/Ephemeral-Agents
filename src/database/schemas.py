from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolType(StrEnum):
    MCP_SERVER = "mcp_server"
    FASTMCP = "fastmcp"
    PYTHON_SCRIPT = "python_script"
    WEBHOOK = "webhook"


class CatalogModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class AgentCreate(CatalogModel):
    agent_id: str = Field(min_length=1)
    macro_domain: str = Field(min_length=1)
    squad: str | None = None
    name: str = Field(min_length=1)
    system_prompt_path: str = Field(min_length=1)
    trigger_hooks: list[str] = Field(min_length=1)
    is_active: bool = True

    @field_validator("trigger_hooks")
    @classmethod
    def normalize_trigger_hooks(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if not normalized:
            raise ValueError("at least one non-empty trigger hook is required")
        return list(dict.fromkeys(normalized))


class ToolCreate(CatalogModel):
    tool_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    tool_type: ToolType
    connection_config: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class AgentToolCreate(CatalogModel):
    agent_id: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    permissions_override: dict[str, Any] | None = None


class SkillCreate(CatalogModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(default="")
    content_md: str = Field(min_length=1)
    token_budget: int = Field(default=800, ge=1)
    source: str = Field(default="local", min_length=1)


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="ignore")

    id: str
    name: str
    description: str
    content_md: str
    token_budget: int = 800
    source: str = "local"
    created_at: datetime | None = None


class DirectorSkillLink(CatalogModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="ignore")

    director_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    is_core: bool = False
    load_priority: int = Field(default=1, ge=0)


DirectorSkillCreate = DirectorSkillLink

