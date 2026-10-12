"""The <think>…</think> streaming splitter."""

from __future__ import annotations

from sila.services.sila.think_stream import ThinkSplitter


def _drain(deltas: list[str]) -> list[tuple[str, str]]:
    s = ThinkSplitter()
    out: list[tuple[str, str]] = []
    for d in deltas:
        out += s.feed(d)
    out += s.flush()
    return out


def _joined(deltas: list[str]) -> tuple[str, str]:
    """(thinking_text, answer_text) after merging same-mode pieces."""
    pieces = _drain(deltas)
    think = "".join(c for m, c in pieces if m == "thinking")
    answer = "".join(c for m, c in pieces if m == "answer")
    return think, answer


def test_no_tags_all_answer() -> None:
    assert _joined(["Hello ", "world"]) == ("", "Hello world")


def test_whole_think_block_in_one_delta() -> None:
    think, answer = _joined(["<think>reason here</think>Final answer"])
    assert think == "reason here"
    assert answer == "Final answer"


def test_tags_split_across_deltas() -> None:
    # The tags arrive in fragments — the classic streaming case.
    think, answer = _joined(["<", "thi", "nk>step 1", " step 2", "</thi", "nk>The ", "answer"])
    assert think == "step 1 step 2"
    assert answer == "The answer"


def test_open_tag_split_at_boundary() -> None:
    think, answer = _joined(["Intro <think", ">hidden</think> visible"])
    assert think == "hidden"
    assert answer == "Intro  visible"


def test_partial_tag_at_stream_end_is_flushed_as_text() -> None:
    # A dangling "<thi" never completed — shown as text, not swallowed.
    _, answer = _joined(["answer <thi"])
    assert answer == "answer <thi"


def test_pieces_preserve_order_and_mode() -> None:
    s = ThinkSplitter()
    first = s.feed("<think>a</think>b")
    assert first == [("thinking", "a"), ("answer", "b")]
    assert s.flush() == []
