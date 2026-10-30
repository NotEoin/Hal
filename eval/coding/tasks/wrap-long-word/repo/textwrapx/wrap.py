"""Greedy word wrap."""


def wrap(text, width):
    """Returns the lines of `text` wrapped at `width` characters."""
    lines, current = [], ""
    for word in text.split():
        if len(word) > width:
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
