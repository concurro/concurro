"""Strict base classes for generated event models."""

import re
from datetime import datetime, timezone
from typing import ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel, field_validator

T = TypeVar("T")


class StrictModel(BaseModel):
    """Reject coercion in generated object models while ignoring new wire fields."""

    model_config = ConfigDict(strict=True)


class StrictRootModel(RootModel[T]):
    """Apply the same strictness to generated scalar and union roots."""

    model_config = ConfigDict(strict=True)


class StrictTimestamp(StrictRootModel[str]):
    """Normalize RFC 3339 timestamps without losing nanoseconds."""

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$(?![\s\S])"
    )

    @field_validator("root", mode="before")
    @classmethod
    def validate_spelling(cls, value):
        """Reject datetime spellings outside the contract's wire grammar."""
        if not isinstance(value, str) or not cls.PATTERN.fullmatch(value):
            raise ValueError("timestamp must use RFC 3339 with seconds and timezone")
        if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError("timestamp offset is out of range")
        fraction = re.search(r"\.([0-9]+)(?:Z|[+-][0-9]{2}:[0-9]{2})$", value)
        nanos = int(fraction.group(1)[:9].ljust(9, "0")) if fraction else 0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except OverflowError as error:
            raise ValueError("timestamp is outside the supported UTC calendar") from error
        precision = 0 if nanos == 0 else 3 if nanos % 1_000_000 == 0 else 6 if nanos % 1_000 == 0 else 9
        digits = f".{nanos:09d}"[: precision + 1] if precision else ""
        return parsed.strftime("%Y-%m-%dT%H:%M:%S") + digits + "Z"
