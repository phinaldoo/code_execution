from __future__ import annotations

import re
from enum import Enum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RenderingVersion(str, Enum):
    v1 = "v1"
    v2 = "v2"


class InputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(..., min_length=1, max_length=128)
    base64_content: str = Field(
        ...,
        min_length=1,
        max_length=35_000_000,
        validation_alias=AliasChoices("base64_content", "base64"),
    )

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        """Validate file name for safe extraction into the render workspace."""
        if "/" in value or "\\" in value:
            raise ValueError("file_name must not contain path separators")
        if not _SAFE_FILENAME_RE.fullmatch(value):
            raise ValueError(
                "file_name may only contain letters, numbers, dots, underscores and hyphens"
            )
        if value in {".", ".."}:
            raise ValueError("invalid file_name")
        return value


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str = Field(..., min_length=1)
    input_files: list[InputFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_input_files(self) -> "RenderRequest":
        """Validate no duplicate file names in input files."""
        seen: set[str] = set()
        for input_file in self.input_files:
            if input_file.file_name in seen:
                raise ValueError(f"duplicate input file name: {input_file.file_name}")
            seen.add(input_file.file_name)
        return self

