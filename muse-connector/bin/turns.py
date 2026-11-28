"""Per-turn records for Dav1d/Muse Spark turns, which Meta does not report
to Trckr.

The one hard rule this module exists to enforce: a context-window delta is
NOT a turn cost. ``session_status`` reports the size of the whole context
window, so the difference between two turns mixes the prior output, tool
results and system injections into "input", and every turn re-bills the
full context rather than the delta. Compaction shrinks the window, so a
naive delta goes negative. Nothing here is a turn's input/output/cache
split, and nothing here is priced.

Records produced by :func:`growth_record` carry:

- ``context_tokens`` — the full context-window size at the end of the
  turn. This is the input figure until Meta exposes real per-turn usage.
- ``estimated_context_growth_tokens`` — the window's growth since the
  previous record, labelled estimated, or ``None`` when compaction made
  the window shrink (``compaction_detected`` is then true and the growth
  is flagged, never silently recorded as a negative number).
- ``cost_usd`` — always ``None``, with ``unpriced_reason``. Muse Spark
  is unpriced in Trckr (not on the providers' pricing pages). ``$0`` is
  reserved for self-hosted runs and must never be recorded here.

Runtime state (the previous context size, the last record/request ids)
belongs in the gitignored ``muse-connector/state/dav1d-turns.json`` or in
Trckr itself via ``trckr_session`` — never in the committed file. The
committed ``muse-connector/dav1d-turns.json`` is a schema/example file
only; see its header.
"""

UNPRICED_REASON = (
    "muse-spark is unpriced in Trckr (not on the providers' pricing "
    "pages); cost is null with this reason, never 0 — $0 is reserved "
    "for self-hosted runs"
)


def growth_record(*, lane, model, context_tokens, previous_context_tokens=None):
    """Build one estimated-context-growth record.

    ``previous_context_tokens`` is the context size from the previous
    record, or ``None`` for the first record (growth is then unknown,
    not zero — a first record of 0 tokens is a junk row, not a baseline).
    A smaller window than the previous record means compaction: growth
    is ``None`` and ``compaction_detected`` is true.
    """
    if not isinstance(context_tokens, int) or isinstance(context_tokens, bool) or context_tokens < 0:
        raise ValueError("context_tokens must be a non-negative integer")
    if previous_context_tokens is not None and (
        not isinstance(previous_context_tokens, int)
        or isinstance(previous_context_tokens, bool)
        or previous_context_tokens < 0
    ):
        raise ValueError("previous_context_tokens must be a non-negative integer or None")

    if previous_context_tokens is None:
        growth = None
        compaction = False
        note = "first record: growth unknown, not zero"
    elif context_tokens < previous_context_tokens:
        growth = None
        compaction = True
        note = (
            "context window shrank (compaction); growth is null, "
            "not a negative delta"
        )
    else:
        growth = context_tokens - previous_context_tokens
        compaction = False
        note = "estimated context-window growth, not turn tokens"

    return {
        "kind": "estimated_context_growth",
        "lane": lane,
        "model": model,
        "context_tokens": context_tokens,
        "estimated_context_growth_tokens": growth,
        "compaction_detected": compaction,
        "cost_usd": None,
        "unpriced_reason": UNPRICED_REASON,
        "note": note,
    }
