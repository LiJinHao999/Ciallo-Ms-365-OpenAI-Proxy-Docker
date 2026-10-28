"""Review #2: the two views of one turn must agree on a source's number.

M365 sends a cumulative ``messages`` snapshot AND incremental
``writeAtCursor`` deltas for the same turn, and it does not always render a
citation the same way in both: one live capture had ``【4-6f710b】`` in the
snapshot against ``<cite>turn1search4</cite>`` in the deltas.

Both renderings name ONE source, but they normalise to unrelated keys, so the
tracker numbered them separately -- the snapshot said ``[4]`` where the deltas
said ``[1]``. ``_split_snapshot_lead`` then saw no common prefix, the delta's
replay of the opening looked like new text, and the reader was told the first
sentence twice.

Two things fix it, and both are pinned here: the source's renderings are
registered as ONE key as its reference metadata arrives, and the snapshot-lead
reconciliation tolerates a differing footnote NUMBER (never differing prose).
"""

from __future__ import annotations

from m365_copilot_openai_proxy.substrate_parse import (
    CitationTracker,
    _message_content,
    _split_snapshot_lead,
)


def _stream(chunks: list[str], tracker: CitationTracker | None = None) -> str:
    """Clean ``chunks`` one at a time, then flush, as chat_stream does."""
    tracker = tracker or CitationTracker()
    out = [tracker.clean(chunk) for chunk in chunks]
    out.append(tracker.flush())
    return "".join(out)


# --- review #2: the two views of one turn must share ONE number per source ---

SNAPSHOT_MARK = "【4-6f710b】"
DELTA_MARK = "<cite>turn1search4</cite>"
SHARED_REFERENCE = {
    "ref_key": "4-6f710b",   # what 【4-6f710b】 normalises to
    "ref_id": "turn1search4",  # what <cite>turn1search4</cite> normalises to
    "label": "4",
    "url": "https://fastapi.tiangolo.com",
    "title": "FastAPI",
}


def test_both_renderings_of_one_source_take_the_same_number():
    """Same source, two renderings, ONE footnote.

    M365 restates a source as ``<cite>turn1search4</cite>`` in the streamed
    deltas and as ``【4-6f710b】`` in the cumulative snapshot. Numbering them
    separately made the snapshot say ``[4]`` where the delta said ``[1]``.
    """
    tracker = CitationTracker()
    tracker.note_source(SHARED_REFERENCE)

    delta_text = _stream([f"FastAPI 很快。{DELTA_MARK}"], tracker)
    snapshot_text = tracker.apply(f"FastAPI 很快。{SNAPSHOT_MARK}")

    assert delta_text == "FastAPI 很快。[4]"
    assert snapshot_text == delta_text
    assert tracker.count == 1
    assert len(tracker.keys_in_order) == 1


def test_shared_numbering_holds_without_reference_metadata():
    """No metadata to link the renderings: the numbers may differ, and the
    snapshot-lead reconciliation must still see them as the same run."""
    tracker = CitationTracker()
    lead = tracker.apply(f"FastAPI 很快。{SNAPSHOT_MARK}")
    delta = _stream([f"FastAPI 很快。{DELTA_MARK}"], tracker)

    assert lead != delta, "precondition: the numbers really do differ here"
    split = _split_snapshot_lead(lead, delta)
    assert split == ("", ""), f"replayed opening would be delivered twice: {split!r}"


def test_split_snapshot_lead_still_refuses_a_divergent_run():
    """The footnote tolerance must not become a licence to align anything."""
    assert _split_snapshot_lead("Alpha [1] 中间", "Alpha [2] 别处") is None
    assert _split_snapshot_lead("Alpha [1]", "Omega [1]") is None


def test_split_snapshot_lead_keeps_exact_prefix_behaviour():
    assert _split_snapshot_lead("Alpha Omega", "Alpha ") == ("Omega", "")
    assert _split_snapshot_lead("Alpha", "Alpha Omega") == ("", " Omega")
    assert _split_snapshot_lead("Alpha", "Omega") is None


def test_snapshot_and_delta_sharing_a_map_stay_identical():
    """The whole point of applying the tracker to a snapshot: the two texts a
    reader may see must remain comparable."""
    tracker = CitationTracker()
    tracker.note_source(SHARED_REFERENCE)
    assert _stream([f"Alpha {DELTA_MARK}"], tracker) == "Alpha [4]"
    assert tracker.apply(f"Alpha {SNAPSHOT_MARK}") == "Alpha [4]"


def test_message_content_uses_the_delta_mapping_for_a_snapshot():
    """`_message_content` is what the snapshot path calls; cleaning it with a
    different rule than the deltas is what broke the prefix in the first place."""
    tracker = CitationTracker()
    tracker.note_source(SHARED_REFERENCE)
    _stream([f"Alpha {DELTA_MARK} Omega"], tracker)

    entry = {"author": "bot", "text": f"Alpha {SNAPSHOT_MARK} Omega"}
    assert _message_content(entry, tracker) == "Alpha [4] Omega"
