"""Verification result contract consumed by scheduler use cases."""

from typing import Protocol


class VerificationResult(Protocol):
    ok: bool
    summary: str
    output: str
