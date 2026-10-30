"""Text report."""

from geom.shapes import area_of


def describe(width, height):
    """Returns a one-line description of a rectangle."""
    return f"{width}x{height} has area {area_of(width, height)}"
