"""Errors raised by halsandbox; the message always says what to fix."""

from __future__ import annotations


class SandboxError(RuntimeError):
    """The secure path cannot start; the message says what to fix."""


class LockdownError(SandboxError):
    """The configured lockdown level forbids the requested action."""
