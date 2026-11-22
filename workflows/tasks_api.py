"""Task queue request models, shared by the /task routes and the !task command."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from brain_db import TaskStatus


class TaskCreate(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    priority: int = 3  # 1=urgent, 3=normal, 5=low
    # user (via !task / API) or system (heartbeat-generated). Internal
    # producers (persona, schedule, scan) write via brain_db directly, so the
    # public API can't impersonate them.
    source: Literal["user", "system"] = "user"

    @field_validator("description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be blank")
        return v


class TaskResult(BaseModel):
    result: str
    status: TaskStatus = TaskStatus.COMPLETED

    @field_validator("status")
    @classmethod
    def _closing_status(cls, v: TaskStatus) -> TaskStatus:
        if v not in TaskStatus.RESULTS:
            raise ValueError(f"status must be one of {sorted(TaskStatus.RESULTS)}")
        return v
