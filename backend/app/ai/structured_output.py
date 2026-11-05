"""Structured model outputs that keep the provider's token usage.

A structured-output call returns the parsed schema object, not the chat
message, so the message's ``usage_metadata`` would otherwise be lost and the
turn's token budget could not be checked. Schemas that inherit from
``UsageReportingOutput`` carry it: the model wrapper stores the message's
usage on the parsed object, and callers read ``result.usage_metadata`` the
same way they read it from a message. The usage is a private attribute, so it
is not part of the JSON schema sent to the model or ``model_dump()``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, PrivateAttr


class UsageReportingOutput(BaseModel):
    _usage_metadata: dict[str, Any] | None = PrivateAttr(default=None)

    @property
    def usage_metadata(self) -> dict[str, Any] | None:
        return self._usage_metadata


def attach_usage(result: Any, message: Any) -> Any:
    """Copy ``message.usage_metadata`` onto a ``UsageReportingOutput`` result."""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(result, UsageReportingOutput) and isinstance(usage, Mapping):
        result._usage_metadata = dict(usage)
    return result
