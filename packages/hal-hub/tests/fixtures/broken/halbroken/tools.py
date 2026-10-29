"""Raises on import, so the worker never sends `ready`."""

raise RuntimeError("halbroken cannot be imported")
