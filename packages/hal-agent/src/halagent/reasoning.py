"""Streaming splitter for inline reasoning in reply text (design 03, 3.3.4).

Some servers put reasoning inside `content` between family markers (think tags, the Gemma 4
channel, harmony). `ReasoningSplitter` is a state machine fed chunk by chunk. It holds back
at most `MAX_HOLD` characters at a chunk edge, so a marker cut in two by the network is still
found, and never delays text longer than the longest marker. An unclosed block at the end of
the stream stays reasoning, so the visible text is empty and the loop treats it as an empty
reply.
"""

from __future__ import annotations

from dataclasses import dataclass

# Marker pairs per family; Gemma 4 and harmony markers come from the design's 3.3.4 table.
MARKERS: dict[str, tuple[str, str]] = {
    "think": ("<think>", "</think>"),
    "gemma4": ("<|channel>thought", "<channel|>"),
    "harmony": ("<|channel|>analysis<|message|>", "<|end|>"),
}
MAX_HOLD = 32  # the longest marker, with room to spare


@dataclass(frozen=True, slots=True)
class Piece:
    """A split fragment: `reasoning` is True for text inside a reasoning block."""

    text: str
    reasoning: bool


class ReasoningSplitter:
    """Split a stream of reply text into visible and reasoning pieces."""

    def __init__(self, families: tuple[str, ...] = tuple(MARKERS)) -> None:
        unknown = [f for f in families if f not in MARKERS]
        if unknown:
            raise ValueError(f"unknown reasoning family: {unknown[0]!r}")
        self._starts = {MARKERS[f][0]: MARKERS[f][1] for f in families}
        self._end = ""  # the end marker of the open block, "" when outside one
        self._buf = ""

    @property
    def in_reasoning(self) -> bool:
        """True while inside an unclosed reasoning block."""
        return bool(self._end)

    def feed(self, chunk: str) -> list[Piece]:
        """Consume `chunk` and return the pieces that are safe to emit now."""
        self._buf += chunk
        return self._drain(final=False)

    def finish(self) -> list[Piece]:
        """Flush held-back text at the end of the stream; an open block stays reasoning."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[Piece]:
        out: list[Piece] = []
        while self._buf:
            targets = [self._end] if self._end else list(self._starts)
            hit = min(
                ((self._buf.find(t), t) for t in targets if t in self._buf),
                default=None,
            )
            if hit is not None:
                pos, marker = hit
                self._emit(out, self._buf[:pos])
                self._buf = self._buf[pos + len(marker) :]
                self._end = "" if self._end else self._starts[marker]
                continue
            keep = 0 if final else self._partial_marker_len(targets)
            self._emit(out, self._buf[: len(self._buf) - keep])
            self._buf = self._buf[len(self._buf) - keep :]
            break
        return out

    def _emit(self, out: list[Piece], text: str) -> None:
        if not text:
            return
        reasoning = bool(self._end)
        if out and out[-1].reasoning == reasoning:
            out[-1] = Piece(out[-1].text + text, reasoning)
        else:
            out.append(Piece(text, reasoning))

    def _partial_marker_len(self, targets: list[str]) -> int:
        """Length of the buffer tail that could still grow into one of `targets`."""
        longest = 0
        for marker in targets:
            for n in range(min(len(marker) - 1, len(self._buf), MAX_HOLD), 0, -1):
                if self._buf.endswith(marker[:n]):
                    longest = max(longest, n)
                    break
        return longest


def split_text(text: str, families: tuple[str, ...] = tuple(MARKERS)) -> tuple[str, str]:
    """Split a complete reply into `(visible, reasoning)`."""
    splitter = ReasoningSplitter(families)
    pieces = splitter.feed(text) + splitter.finish()
    visible = "".join(p.text for p in pieces if not p.reasoning)
    reasoning = "".join(p.text for p in pieces if p.reasoning)
    return visible, reasoning
