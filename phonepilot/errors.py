"""Errors raised by PhonePilot.

Every error carries a message that is safe to print in the CLI. Validation
errors collect every problem instead of stopping at the first one so a broken
template can be fixed in one pass.
"""

from __future__ import annotations


class PhonePilotError(Exception):
    """Base class for expected, user facing failures."""


class ConfigError(PhonePilotError):
    """The local environment or registry is not usable."""


class DeviceError(PhonePilotError):
    """A device is unknown, duplicated, or misconfigured."""


class TemplateError(PhonePilotError):
    """A workflow template failed to load or validate."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = list(errors or [])
        if self.errors and len(self.errors) > 1:
            message = message + "\n" + "\n".join(f"  - {e}" for e in self.errors)
        elif self.errors:
            message = message + "\n" + f"  - {self.errors[0]}"
        super().__init__(message)


class PolicyError(TemplateError):
    """A template asks for something the project refuses to automate."""


class TransportError(PhonePilotError):
    """The board could not be reached or refused a command."""


class StepError(PhonePilotError):
    """A single workflow step failed."""


class DraftError(PhonePilotError):
    """The language model could not produce a usable workflow draft."""
