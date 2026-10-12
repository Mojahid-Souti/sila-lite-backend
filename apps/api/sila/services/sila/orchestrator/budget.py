"""Per-turn budget for the fan-out runtime (denial-of-wallet control).

Tracks LLM tokens spent and wall-clock elapsed across all workers in one turn.
Fan-out nodes check `exhausted` at boundaries and, when hit, stop spawning/looping
and let the synthesizer deliver partial results (fail-closed — never a crash).

Only the "fanout" runtime consumes this; it's inert otherwise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TurnBudget:
    token_budget: int
    deadline_s: float
    _tokens: int = 0
    _started: float = field(default_factory=time.monotonic)

    def charge(self, tokens: int) -> None:
        """Add to the running token total (negative/None ignored)."""
        if tokens and tokens > 0:
            self._tokens += tokens

    def start_clock(self) -> None:
        """Reset the wall-clock at the start of a turn (the runner calls this)."""
        self._started = time.monotonic()
        self._tokens = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def over_tokens(self) -> bool:
        return self._tokens >= self.token_budget

    @property
    def over_time(self) -> bool:
        return (time.monotonic() - self._started) >= self.deadline_s

    @property
    def exhausted(self) -> bool:
        """True when either cap is hit — callers must stop spending and finish."""
        return self.over_tokens or self.over_time
