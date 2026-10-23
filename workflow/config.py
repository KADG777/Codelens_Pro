"""Validated execution choices shared by the visual editor and run service."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config_version: Literal[1] = 1
    parallel_checks: bool = False
    enable_repair: bool = True
    max_fix_rounds: int = Field(default=2, ge=0, le=2)
    pause_after_review: bool = False
    extension_nodes: list[str] = Field(default_factory=list, max_length=4)

    def effective_fix_rounds(self) -> int:
        return self.max_fix_rounds if self.enable_repair else 0
