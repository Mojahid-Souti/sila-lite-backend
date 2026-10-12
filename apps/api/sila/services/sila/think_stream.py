"""Split a streamed reply into THINKING vs ANSWER on <think>…</think> tags.

qwen isn't a native reasoning model, but instructed to wrap its reasoning in
`<think>…</think>` it will — and showing that reasoning (in the collapsible
"thinking" section) is the visible-reasoning UX the user asked for. The tags can
arrive split across token boundaries, so this is a small streaming state machine:
feed each delta, get back ("thinking"|"answer", text) pieces, holding back only a
trailing fragment that could be the start of a tag. Anything with no tags flows
straight through as "answer" — graceful when the model emits none.

Complexity: O(len) per feed; the held-back buffer is < len(tag).
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"


def _partial_suffix(buf: str, marker: str) -> int:
    """Length of the longest suffix of `buf` that is a prefix of `marker` (a
    possible split tag to hold back). 0 when none."""
    for k in range(min(len(buf), len(marker) - 1), 0, -1):
        if marker.startswith(buf[-k:]):
            return k
    return 0


class ThinkSplitter:
    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        """Consume a text delta; return [(mode, chunk)] where mode is
        'thinking' or 'answer'. Chunks are non-empty."""
        self._buf += text
        out: list[tuple[str, str]] = []
        while True:
            marker = _CLOSE if self._in_think else _OPEN
            idx = self._buf.find(marker)
            if idx != -1:
                if idx > 0:
                    out.append(("thinking" if self._in_think else "answer", self._buf[:idx]))
                self._buf = self._buf[idx + len(marker) :]
                self._in_think = not self._in_think
                continue
            hold = _partial_suffix(self._buf, marker)
            emit = self._buf[: len(self._buf) - hold] if hold else self._buf
            if emit:
                out.append(("thinking" if self._in_think else "answer", emit))
            self._buf = self._buf[len(self._buf) - hold :] if hold else ""
            break
        return [(m, c) for m, c in out if c]

    def flush(self) -> list[tuple[str, str]]:
        """End of stream — emit whatever's left (a dangling partial tag is shown
        as text rather than swallowed)."""
        if not self._buf:
            return []
        rest = [("thinking" if self._in_think else "answer", self._buf)]
        self._buf = ""
        return rest
