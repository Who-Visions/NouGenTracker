"""Per-machine daily token rollups, carried by git.

token_tracker.py answers "what did *this* box spend?" by reading the logs each
agent already writes. That answer never leaves the machine it was computed on,
so the fleet-wide question — "what did I spend across every box?" — had no
answer at all.

This module is the transport. Each machine exports its own days to
`dailies/<machine>/<YYYY-MM-DD>.json`, commits them, and pushes. Every other
machine pulls and sums. There is no server and no daemon; git is the bus, the
same way NouGenRelay carries handoffs.

Design rules, inherited from the fleet usage ledger:
- One file per (machine, day). Re-exporting a day REPLACES that file, so a
  re-run can never double-count. Machines only ever write their own directory,
  which is what keeps concurrent pushes from conflicting.
- Integers and model names only. Never session text, paths, or secrets — these
  files land in a public repo.
- Today is exported as `partial`, because the day is not over. Aggregation
  reports partial days separately instead of quietly mixing them into totals.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import socket
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 3

# Field names differ between an invocation record and the billing function:
# invocations carry `cache_read`, model_bill() expects `cache_read_input_tokens`.
# Passing the wrong shape doesn't error — the missing keys read 0 and the cache
# tokens, which are 95% of this fleet's volume, silently bill as free.
_BILL_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "reasoning": "reasoning_tokens",
}

# --- counting semantics ---------------------------------------------------
#
# `schema` describes the SHAPE of a daily file. It says nothing about what the
# numbers inside it MEAN, and that is the version that actually breaks a fleet
# total.
#
# Proven on 2026-08-01: parse_claude() deduped usage records by `uuid`, but
# Claude Code writes one row per content block and repeats the same usage object
# under a fresh uuid each time — so one API request was billed once per block it
# produced. Measured 554M cache-read tokens where the truth was 287M. Fixing it
# did not change the file format at all, so every daily already published stayed
# schema-2-valid, kept summing into the fleet total, and nothing anywhere could
# tell that those numbers were no longer comparable to new ones.
#
# A hand-maintained version constant would not have helped: it only moves when
# someone remembers to move it, and the person who forgets is the person who
# just changed the counting. So the version is DERIVED from the code that does
# the counting.

#: The functions whose behaviour decides what ends up in a token total. A change
#: to any of them makes new numbers incomparable to old ones.
COUNTING_SURFACE = (
    "usage_of",
    "parse_ts",
    "parse_claude",
    "parse_antigravity",
    "parse_codex",
    "parse_gemini_cli",
    "parse_fleet_usage",
)

# Bottom-k MinHash width. 64 gives a Jaccard standard error of ~1/sqrt(64) =
# 12.5%, which cleanly separates "independent machines" (~0) from "same logs
# read twice" (~1) while costing ~0.5 KB per machine-day.
MINHASH_K = 64

# Iglewicz & Hoaglin's cutoff for the modified z-score. Their recommendation is
# 3.5; token usage is heavy-tailed and bursty, so a robust score is the right
# tool and the standard threshold is the right place to start.
OUTLIER_Z = 3.5

REPO_ROOT = Path(__file__).resolve().parent
DAILIES_DIR = REPO_ROOT / "dailies"
TRACKER_SOURCE = REPO_ROOT / "token_tracker.py"

#: What an unstamped daily reports as. Files written before counter stamping
#: existed are, by definition, from an unknown counting version — including the
#: ones written by the parser that double-counted.
UNSTAMPED = "unstamped"

#: Suffix for a counter computed from a working tree that does not match HEAD.
#: A fingerprint's whole job is to name a counting version someone can later
#: look up, and a value computed from code that was never committed cannot be
#: looked up — it is worse than an absent one, because it still reads as
#: authoritative.
#:
#: Marking beats both alternatives. Silently stamping a dirty tree produces an
#: unreproducible id that looks authoritative; refusing to export blocks the
#: ordinary case of testing a parser change before committing it. A marked
#: counter stays comparable to itself — two exports from the same dirty tree
#: still match — while never being mistaken for a version anyone can check out.
#:
#: HISTORICAL NOTE, because the comment here used to assert the opposite: this
#: marker was introduced on the theory that whoart's 71aef8ff08fa came from an
#: uncommitted tree, since no commit reproduced it on phoebus. That was wrong.
#: The two boxes ran different Pythons and the digest was interpreter-sensitive
#: (see _canonical). whoart's tree was clean; so was phoebus's. The mechanism is
#: still right and still earns its place — but the case it catches is rare, not
#: the routine explanation for a counter mismatch. Suspect the interpreter first.
DIRTY_SUFFIX = "+dirty"

# The token fields every record carries. Kept explicit rather than derived so a
# new field in token_tracker.py cannot silently change the on-disk schema.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation",
    "cache_read",
    "reasoning",
)


# --- identity -------------------------------------------------------------
#
# Resolved exactly the way NouGenRelay resolves it, so one grep for a machine
# name finds its commits, its handoffs and its dailies. Divergence here would
# silently split one box's history in two.

def slugify_machine(name: str) -> str:
    """Lowercase, non-alphanumerics to hyphens, no trailing hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "unknown-machine"


def resolve_machine() -> str:
    """NOUGEN_MACHINE wins; otherwise the hostname."""
    explicit = os.environ.get("NOUGEN_MACHINE", "").strip()
    if explicit:
        return slugify_machine(explicit)
    try:
        return slugify_machine(socket.gethostname())
    except Exception:
        return "unknown-machine"


def resolve_agent() -> str:
    """Which lane wrote this. No OS equivalent, so it must be declared."""
    return slugify_machine(os.environ.get("NOUGEN_AGENT", "").strip() or "unknown-agent")


# --- export ---------------------------------------------------------------

def known_machines(dailies_dir: Optional[Path] = None) -> List[str]:
    """Machines that have already published into this clone."""
    root = dailies_dir or DAILIES_DIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def unintroduced_machine_warning(
    machine: Optional[str] = None, dailies_dir: Optional[Path] = None,
    known: Optional[List[str]] = None,
) -> Optional[str]:
    """Flag a machine name the fleet has never seen. None if fine.

    This is the check that stops one box from being counted twice under two
    names. NouGenRelay learned it the hard way: `who-mac-mini`, `phoebus` and
    `kushboygroups-mac-mini-local` are all the same Mac, and a hostname change
    is enough to fork a machine's history in two. Totals summed across a split
    identity are wrong in the direction that looks plausible, which is the
    worst direction to be wrong in.

    It warns rather than refuses — a box's first export is legitimately a new
    name, and there is no human at the keyboard to ask.
    """
    machine = machine or resolve_machine()
    # `known` MUST be sampled before the export writes anything. export_days()
    # creates dailies/<machine>/ for the name it is exporting under, so a
    # caller that reads the directory afterwards always finds the new machine
    # already "known" and this function can never fire. Measured on phoebus
    # 2026-09-04: an export under the hostname silently forked a fourth
    # machine series and the warning did not appear, because by the time it
    # was consulted the fork it was meant to announce had already happened.
    if known is None:
        known = known_machines(dailies_dir)
    if not known or machine in known:
        return None
    return (
        f"'{machine}' has never published dailies here. Known: {', '.join(known)}. "
        "If this box already has a fleet name, export under it: "
        f"NOUGEN_MACHINE=<name>. Otherwise this becomes a new machine."
    )


def _without_docstrings(node: ast.AST) -> ast.AST:
    """Drop docstrings so prose edits do not read as a change in behaviour."""
    for child in ast.walk(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Module)):
            continue
        body = getattr(child, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            child.body = body[1:] or [ast.Pass()]
    return node


#: AST fields CPython has added to existing node types. They carry no counting
#: semantics, and including them makes the digest a function of the interpreter
#: rather than of the code — 3.12 added type_params to FunctionDef, which was
#: enough to give every Python version its own fingerprint for identical source.
#: Extend this when a release adds another; the cross-interpreter test in
#: tests/test_counter_portability.py fails loudly when one appears.
_VOLATILE_AST_FIELDS = frozenset({"type_params", "type_comment"})


def _canonical(node: ast.AST) -> str:
    """A serialisation of an AST that means the same thing on every CPython.

    Neither stdlib option survives contact with a mixed-version fleet.
    ast.dump() writes out whatever fields the running interpreter defines, so a
    new field in a point release moves the digest for source that has not
    changed. ast.unparse() renders back to source and dodges that, but its
    formatting is not frozen either — 3.10 emits different text than 3.11 for
    the same tree, which CI caught after 3.11 and 3.13 had agreed locally and
    looked like proof.

    So the traversal is ours: node type name, then each field in _fields order
    minus the volatile ones. That stays insensitive to formatting, comments and
    docstrings — the property that made hashing the AST right to begin with —
    while depending only on grammar this code names explicitly.
    """
    if isinstance(node, ast.AST):
        parts = [type(node).__name__]
        for field in node._fields:
            if field in _VOLATILE_AST_FIELDS:
                continue
            parts.append(f"{field}={_canonical(getattr(node, field, None))}")
        return "(" + ",".join(parts) + ")"
    if isinstance(node, list):
        return "[" + ",".join(_canonical(item) for item in node) + "]"
    return repr(node)


def _ast_digest(source: str) -> Optional[str]:
    """Hash the counting surface out of a tracker source. None if unparseable.

    The pure half of the fingerprint: no git, no filesystem, so it can be run
    over the working copy and over a committed blob and the two compared.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in COUNTING_SURFACE:
            found[node.name] = _canonical(_without_docstrings(node))
    parts = [f"{name}:{found.get(name, '<missing>')}" for name in sorted(COUNTING_SURFACE)]
    return hashlib.blake2b("\x1e".join(parts).encode("utf-8"), digest_size=6).hexdigest()


def _committed_source(path: Path, ref: str = "HEAD") -> Optional[str]:
    """The tracker as committed at `ref`, or None if that cannot be established."""
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return None
    code, committed = _git("show", f"{ref}:{rel.as_posix()}")
    return committed if code == 0 and committed else None


def _tracker_differs_from_head(path: Path) -> bool:
    """Whether the COUNTING code on disk differs from the committed counting code.

    Answers "can someone else reproduce this fingerprint?" — the only question a
    counting version is asked. Two checks, in order, and both are needed.

    First, ask git whether the BYTES match. `hash-object` applies the same
    filters git used writing the blob, so line endings and .gitattributes settle
    themselves, and nothing is decoded — which matters because the decoded-text
    comparison this replaces was wrong on every Windows box and silent about it:
    `read_text(errors="replace")` turned every em dash into U+FFFD while
    `git show` returned it correctly, so the two could never be equal and a
    clean tree stamped `+dirty` forever. `errors="replace"` cannot raise, so the
    only symptom was a suffix nobody questioned.

    Identical bytes settle it. DIFFERENT bytes do not, and that is the second
    check. The fingerprint is deliberately AST-scoped to COUNTING_SURFACE so
    comments and formatting cannot churn it — so an edit to a print statement or
    a new CLI flag leaves the counting code byte-identical to HEAD and the
    fingerprint perfectly reproducible. Reporting that as dirty is a false
    positive that fires during ordinary work, quarantines exports that were
    fine, and trains people to ignore the marker: exactly the reasoning that
    made the fingerprint AST-based in the first place. So on a byte mismatch,
    compare what is actually hashed.

    Unknowable is not dirty. Outside a work tree, without git, or on a file git
    does not track, this returns False — the same answer a clean checkout
    gives, which is the right default for the many places this runs with no
    repository at all.
    """
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return False

    code, head_blob = _git("rev-parse", f"HEAD:{rel.as_posix()}")
    if code != 0:
        return False
    code, disk_blob = _git("hash-object", "--", str(path))
    if code == 0 and head_blob.strip() == disk_blob.strip():
        return False

    committed = _committed_source(path)
    if committed is None:
        return False
    try:
        current = _ast_digest(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    committed_digest = _ast_digest(committed)
    if current is None or committed_digest is None:
        return False
    return current != committed_digest


def counter_fingerprint(source: Optional[Path] = None) -> str:
    """A short digest of the code that decides what counts as a token.

    Hashed from the ABSTRACT SYNTAX TREE of the functions in COUNTING_SURFACE,
    not their text. That distinction is what makes this usable: reformatting,
    renaming a local, adding a comment or rewriting a docstring leaves the
    digest alone, while changing a dedup key, a filter or a field moves it. A
    fingerprint that churned on cosmetic edits would force pointless re-exports
    and be switched off within a week.

    Functions are hashed in name order, so moving one up the file is not a
    change. A name that has disappeared is recorded as `<missing>` — deleting or
    renaming a parser genuinely does change what gets counted.

    Returns UNSTAMPED if the source cannot be read or parsed. Being unable to
    identify the counting version is exactly the state an unstamped file is in,
    and reporting it as a real version would be a lie in the safe-looking
    direction.
    """
    path = Path(source or TRACKER_SOURCE)
    try:
        digest = _ast_digest(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return UNSTAMPED
    if digest is None:
        return UNSTAMPED
    return digest + (DIRTY_SUFFIX if _tracker_differs_from_head(path) else "")


def counter_of(record: Dict[str, Any]) -> str:
    """The counting version a published daily was produced by."""
    value = record.get("counter")
    return str(value) if value else UNSTAMPED


def _day_of(invocation: Dict[str, Any]) -> Optional[str]:
    ts = invocation.get("timestamp")
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def fingerprint(invocation: Dict[str, Any]) -> int:
    """A stable 64-bit id for one model call.

    Two machines that read the same synced log directory would otherwise each
    report the same call, and a plain sum would silently double it. Hashing the
    fields that identify a call — when, which model, how many tokens — lets the
    aggregator recognise the overlap instead of assuming the machines are
    independent.
    """
    parts = [
        str(invocation.get("timestamp")),
        str(invocation.get("model")),
        str(invocation.get("session_id")),
    ] + [str(invocation.get(f) or 0) for f in TOKEN_FIELDS]
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def minhash(fingerprints: Iterable[int], k: int = MINHASH_K) -> List[int]:
    """Bottom-k MinHash sketch of a set of fingerprints.

    Storing every fingerprint would multiply a daily file by its invocation
    count (500+ on a busy day, in a public repo). A bottom-k sketch is ~k
    integers regardless of set size, and the Jaccard similarity of two sketches
    estimates the true overlap of the underlying sets with standard error
    ~1/sqrt(k) — 12.5% at k=64, which is far below the threshold that matters
    here. Broder's original construction; the estimator is unbiased.
    """
    return sorted(set(fingerprints))[:k]


def jaccard(sketch_a: List[int], sketch_b: List[int], k: int = MINHASH_K) -> float:
    """Estimate |A ∩ B| / |A ∪ B| from two bottom-k sketches.

    The union sketch is the bottom-k of the merged sketches; the estimate is
    the fraction of those k that both sides carry. Valid because bottom-k of a
    union is computable from the two bottom-k sketches alone.
    """
    if not sketch_a or not sketch_b:
        return 0.0
    union = sorted(set(sketch_a) | set(sketch_b))[:k]
    if not union:
        return 0.0
    both = set(sketch_a) & set(sketch_b)
    return sum(1 for v in union if v in both) / len(union)


def rollup(invocations: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collapse raw invocations into per-day, per-source token totals.

    Model detail is kept as a per-day breakdown because that is what makes a
    fleet total actionable — "we spent 4M tokens" is trivia, "we spent 4M on
    opus across three boxes" is a decision.

    Exact and estimated counts are tracked separately rather than added
    together. Some providers report usage; others are inferred from text
    length, and folding a guess into a measurement produces a total that cannot
    be reasoned about. Keeping the split lets a reader see how much of any
    number is actually known.
    """
    days: Dict[str, Dict[str, Any]] = {}
    for inv in invocations:
        day = _day_of(inv)
        if not day:
            continue
        bucket = days.setdefault(
            day,
            {"sources": defaultdict(lambda: defaultdict(int)),
             "models": defaultdict(lambda: defaultdict(int)),
             "exact": defaultdict(int),
             "estimated": defaultdict(int),
             "provider_stats": {},
             "fingerprints": [],
             "invocations": 0},
        )
        source = str(inv.get("source") or "unknown")
        model = str(inv.get("model") or "unknown")
        # `exact` is set by the parsers: True where the provider reported usage,
        # False where the tracker inferred it.
        confidence = "exact" if inv.get("exact") else "estimated"
        for field in TOKEN_FIELDS:
            value = int(inv.get(field) or 0)
            bucket["sources"][source][field] += value
            bucket["models"][model][field] += value
            bucket[confidence][field] += value
        if source == "OpenAI Codex":
            stats = bucket["provider_stats"].setdefault(
                source,
                {"session_ids": set(), "plan_types": set(),
                 "uncached_input_tokens": 0, "total_tokens": 0,
                 "input_tokens": 0, "cached_input_tokens": 0,
                 "peak_context_used_percent": 0.0,
                 "rate_limits": {}},
            )
            session_id = inv.get("session_id")
            if session_id:
                stats["session_ids"].add(str(session_id))
            plan_type = inv.get("openai_plan_type")
            if plan_type:
                stats["plan_types"].add(str(plan_type))
            input_tokens = int(inv.get("input_tokens") or 0)
            cached_input = int(inv.get("cache_read") or 0)
            stats["input_tokens"] += input_tokens
            stats["cached_input_tokens"] += cached_input
            stats["uncached_input_tokens"] += int(
                inv.get("openai_uncached_input_tokens")
                if inv.get("openai_uncached_input_tokens") is not None
                else max(0, input_tokens - cached_input))
            # Codex reasoning is a subset of output_tokens, so it must not be
            # added again when deriving total_tokens.
            stats["total_tokens"] += int(
                inv.get("openai_total_tokens")
                if inv.get("openai_total_tokens") is not None
                else input_tokens + int(inv.get("output_tokens") or 0))
            context_window = int(inv.get("openai_context_window") or 0)
            if context_window > 0:
                context_input = int(
                    inv.get("openai_context_input_tokens") or input_tokens)
                stats["peak_context_used_percent"] = max(
                    stats["peak_context_used_percent"],
                    100.0 * context_input / context_window)
            for limit_name in ("primary", "secondary"):
                used = inv.get(f"openai_{limit_name}_used_percent")
                if used is None:
                    continue
                sample = stats["rate_limits"].setdefault(
                    limit_name,
                    {"peak_used_percent": 0.0, "latest_used_percent": 0.0,
                     "window_minutes": None, "resets_at": None,
                     "_latest_at": ""},
                )
                sample["peak_used_percent"] = max(
                    sample["peak_used_percent"], float(used))
                sampled_at = str(inv.get("timestamp") or "")
                if sampled_at >= sample["_latest_at"]:
                    sample["_latest_at"] = sampled_at
                    sample["latest_used_percent"] = float(used)
                    sample["window_minutes"] = inv.get(
                        f"openai_{limit_name}_window_minutes")
                    sample["resets_at"] = inv.get(
                        f"openai_{limit_name}_resets_at")
        elif source == "Claude Code":
            # Like-for-like with the Codex stats above. Anthropic reports three
            # disjoint input buckets: input_tokens (neither cached nor written),
            # cache_creation (new tokens written to the cache) and cache_read.
            # OpenAI reports no write bucket: every token it did not serve from
            # cache sits in its uncached input. So the comparable Claude figure
            # is input + cache_creation, not input alone. Comparing Codex's
            # uncached input with Claude's bare input_tokens made Codex look
            # like ~99.9% of the fleet's fresh input on 2026-11-12 (4.7M vs
            # 1,953) when Claude had written 12.5M tokens to cache that day.
            stats = bucket["provider_stats"].setdefault(
                source,
                {"session_ids": set(), "plan_types": set(),
                 "uncached_input_tokens": 0, "total_tokens": 0,
                 "input_tokens": 0, "cached_input_tokens": 0,
                 "cache_write_tokens": 0,
                 "peak_context_used_percent": 0.0,
                 "rate_limits": {}},
            )
            # parse_claude() sets session_id to the PROJECT, so it would count
            # every conversation in one project as one session. The transcript
            # file is one conversation, matching Codex's per-rollout count.
            session_key = inv.get("source_file") or inv.get("session_id")
            if session_key:
                stats["session_ids"].add(str(session_key))
            fresh = int(inv.get("input_tokens") or 0)
            written = int(inv.get("cache_creation") or 0)
            cached_input = int(inv.get("cache_read") or 0)
            # input_tokens here means "input not served from cache", the same
            # meaning it has in the Codex stats, so one ratio formula serves both.
            stats["input_tokens"] += fresh + written
            stats["cache_write_tokens"] += written
            stats["cached_input_tokens"] += cached_input
            stats["uncached_input_tokens"] += fresh + written
            stats["total_tokens"] += (fresh + written + cached_input
                                      + int(inv.get("output_tokens") or 0))
        bucket["fingerprints"].append(fingerprint(inv))
        bucket["invocations"] += 1

    def public_provider_stats(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Freeze privacy-safe provider aggregates; discard identifiers."""
        public = {}
        for provider, stats in raw.items():
            input_tokens = int(stats["input_tokens"])
            limits = {}
            for name, sample in stats["rate_limits"].items():
                limits[name] = {
                    key: value for key, value in sample.items()
                    if not key.startswith("_")
                }
            public[provider] = {
                "distinct_sessions": len(stats["session_ids"]),
                "total_tokens": int(stats["total_tokens"]),
                "uncached_input_tokens": int(stats["uncached_input_tokens"]),
                # OpenAI's cached subtotal is nested inside input_tokens.  The
                # parser stores fresh input and cache reads as disjoint fields,
                # so the denominator must restore the raw input whole.
                "cache_hit_ratio": round(
                    stats["cached_input_tokens"]
                    / (input_tokens + stats["cached_input_tokens"]), 4)
                    if input_tokens + stats["cached_input_tokens"] else 0.0,
                "peak_context_used_percent": round(
                    stats["peak_context_used_percent"], 2),
                "plan_types": sorted(stats["plan_types"]),
                "rate_limits": limits,
            }
            if "cache_write_tokens" in stats:
                # Only providers that report cache writes carry this field;
                # OpenAI does not, and a zero there would read as measured.
                public[provider]["cache_write_tokens"] = int(
                    stats["cache_write_tokens"])
        return public

    # defaultdicts serialise fine but compare badly in tests; freeze them.
    return {
        day: {
            "sources": {s: dict(f) for s, f in b["sources"].items()},
            "models": {m: dict(f) for m, f in b["models"].items()},
            "exact": {f: b["exact"].get(f, 0) for f in TOKEN_FIELDS},
            "estimated": {f: b["estimated"].get(f, 0) for f in TOKEN_FIELDS},
            "provider_stats": public_provider_stats(b["provider_stats"]),
            "sketch": minhash(b["fingerprints"]),
            "invocations": b["invocations"],
        }
        for day, b in days.items()
    }


def totals_of(breakdown: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Sum a {name: {field: n}} breakdown down to one {field: n}."""
    out = {field: 0 for field in TOKEN_FIELDS}
    for fields in breakdown.values():
        for field in TOKEN_FIELDS:
            out[field] += int(fields.get(field) or 0)
    return out


def export_days(
    invocations: Iterable[Dict[str, Any]],
    machine: Optional[str] = None,
    today: Optional[date] = None,
    dailies_dir: Optional[Path] = None,
) -> List[Path]:
    """Write one file per day this machine has usage for. Returns paths written."""
    machine = machine or resolve_machine()
    today = today or date.today()
    root = (dailies_dir or DAILIES_DIR) / machine
    root.mkdir(parents=True, exist_ok=True)
    counter = counter_fingerprint()

    written: List[Path] = []
    for day, bucket in sorted(rollup(invocations).items()):
        record = {
            "schema": SCHEMA_VERSION,
            # Which counting code produced these numbers. `schema` says the file
            # can be READ; this says the numbers can be COMPARED.
            "counter": counter,
            "machine": machine,
            "date": day,
            # A day is only final once it is over. Anything else would let a
            # mid-afternoon export freeze an incomplete day as authoritative.
            "partial": day >= today.strftime("%Y-%m-%d"),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "generated_by": resolve_agent(),
            "invocations": bucket["invocations"],
            "totals": totals_of(bucket["sources"]),
            "exact": bucket["exact"],
            "estimated": bucket["estimated"],
            # Hex, because JSON numbers above 2^53 are not safely round-trippable.
            "sketch": [f"{v:016x}" for v in bucket["sketch"]],
            "sources": bucket["sources"],
            "models": bucket["models"],
            "provider_stats": bucket["provider_stats"],
        }
        path = root / f"{day}.json"
        # Replace, never append: one file per (machine, day) is what makes a
        # re-export idempotent.
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


# --- aggregate ------------------------------------------------------------

def load_fleet(dailies_dir: Optional[Path] = None,
               since: Optional[str] = None,
               until: Optional[str] = None) -> List[Dict[str, Any]]:
    """Daily records published into this clone, optionally windowed.

    `since`/`until` are inclusive ISO dates (YYYY-MM-DD). They are compared as
    strings because that is exactly what the records store and ISO dates sort
    lexically; parsing them into datetimes would add a failure mode without
    changing a single ordering. Omit both for all published history.

    Windowing lives HERE rather than in the caller because a caller that loads
    everything and filters afterwards is one forgotten filter away from
    silently reporting the wrong period - which is the defect this parameter
    exists to close (--fleet ignored --days entirely until 2026-08-31).
    """
    root = dailies_dir or DAILIES_DIR
    if not root.exists():
        return []

    records: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt or half-pushed file must not take the whole report
            # down; the fleet is only as reliable as its worst clone.
            continue
        if not isinstance(record, dict) or record.get("schema") != SCHEMA_VERSION:
            continue
        # Trust the directory over the field: the path is what git merged, the
        # field is what some other box claimed about itself.
        record["machine"] = path.parent.name
        # A record with no date cannot be placed in a window. Dropping it when
        # one is requested is the honest move: including it would silently pad
        # a bounded report with an unbounded row.
        if since or until:
            day = record.get("date")
            if not isinstance(day, str):
                continue
            if since and day < since:
                continue
            if until and day > until:
                continue
        records.append(record)
    return records


def committed_counter(ref: str = "HEAD") -> str:
    """The fingerprint of the tracker AS COMMITTED — one a reader can retrieve.

    The point of a counting version is that someone holding a number can go and
    read the code that produced it. That makes "reproducible from a commit" a
    property worth knowing about a cohort, not a detail.
    """
    source = _committed_source(TRACKER_SOURCE, ref)
    if source is None:
        return UNSTAMPED
    return _ast_digest(source) or UNSTAMPED


def counter_cohorts(
    winners: List[Tuple[Tuple[str, str], Dict[str, Any]]],
    local: Optional[str] = None,
    reproducible: Optional[set] = None,
) -> Dict[str, Any]:
    """Group published machine-days by the counting version that produced them.

    The question this answers is NOT "do these match me?" — the aggregator is a
    reader, and a box running an older tracker has no authority over a corpus of
    published files. The question is "are these numbers comparable to EACH
    OTHER?", because a single cohort is internally consistent no matter which
    version it is, and a mixed corpus is wrong no matter which version is right.

    The largest cohort is treated as current on the assumption that fixes
    propagate: the machine-days that have NOT been re-exported are the minority,
    and they are what needs republishing. Ties break toward the cohort with the
    most recent `generated_at`, since the newer counting is the one that just
    changed.
    """
    local = local if local is not None else counter_fingerprint()
    cohorts: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    newest: Dict[str, str] = {}
    for (machine, day), record in winners:
        counter = counter_of(record)
        cohorts[counter].append((machine, day))
        stamp = str(record.get("generated_at") or "")
        if stamp > newest.get(counter, ""):
            newest[counter] = stamp

    # UNSTAMPED can never be current, even when it is the ONLY cohort and the
    # corpus therefore looks internally consistent. It is not a version, it is
    # the absence of one — and on this repo it means precisely "written before
    # stamping existed", which is the same window as the parser that billed one
    # request once per content block. A corpus of nothing but unstamped files is
    # the most wrong a fleet total can be, and the one that would slip through a
    # plain majority rule.
    # Neither can a counter nobody can look up. `+dirty` says outright that the
    # code it names was never committed; a plain digest that no commit in this
    # repo reproduces says the same thing without admitting it. Both fail the
    # only question a counting version exists to answer — "go read the code that
    # produced this number" — so neither may be declared current, however many
    # files carry it.
    #
    # This is not hypothetical. On 2026-08-01 seventeen files stamped
    # 22555db5d239 outvoted fifteen stamped 71aef8ff08fa, and 71aef8ff08fa was
    # the one five commits reproduce. Ranking by headcount handed "current" to
    # the cohort that could not be verified and marked the verifiable one stale.
    if reproducible is None:
        reproducible = {committed_counter()}
    reproducible = {c for c in reproducible if c and c != UNSTAMPED}

    def _eligible(counter: str) -> bool:
        return counter != UNSTAMPED and not counter.endswith(DIRTY_SUFFIX)

    def _rank(item):
        counter, entries = item
        # Reproducibility outranks headcount. A majority of unverifiable files
        # is still unverifiable.
        return (counter in reproducible, len(entries), newest.get(counter, ""))

    ranked = sorted(
        ((counter, entries) for counter, entries in cohorts.items()
         if _eligible(counter)),
        key=_rank,
        reverse=True,
    )
    current = ranked[0][0] if ranked else local
    stale = sorted(
        (machine, day, counter)
        for counter, entries in cohorts.items() if counter != current
        for machine, day in entries
    )
    return {
        "local": local,
        "current": current,
        # "mixed" means the corpus cannot be summed as it stands. More than one
        # cohort does it; so does a single unstamped one, because those numbers
        # are not comparable to anything this code would produce today.
        "mixed": len(cohorts) > 1 or (len(cohorts) == 1 and UNSTAMPED in cohorts),
        "cohorts": {counter: sorted(entries) for counter, entries in cohorts.items()},
        "stale": stale,
        # Which cohorts a reader can actually go and verify. Named so the report
        # can say WHY a cohort is stale — "nobody re-exported yet" and "this
        # names code that was never committed" need different answers.
        "reproducible": sorted(c for c in cohorts if c in reproducible),
        "unverifiable": sorted(c for c in cohorts
                               if c != UNSTAMPED and c not in reproducible),
        # A box whose own counting differs from the corpus is not wrong yet — it
        # becomes a second cohort the moment it publishes.
        "local_is_current": (not ranked) or local == current,
    }


def stale_range(stale: List[Tuple[str, str, str]], machine: str) -> Optional[Tuple[str, str]]:
    """First and last date this machine needs to re-export. None if clean.

    Returned so the tool can print the exact command instead of "some of your
    dailies are stale, figure out which".
    """
    days = sorted(day for m, day, _ in stale if m == machine)
    return (days[0], days[-1]) if days else None


def aggregate(
    records: Iterable[Dict[str, Any]], price_fn: Optional[Any] = None
) -> Dict[str, Any]:
    """Fold published dailies into fleet totals, per machine and per day.

    Pass price_fn (token_tracker.model_bill) to also get spend. Without it
    the token totals are unchanged — dollars are additive on top, never a
    precondition, so a caller with no pricing table still gets volume.
    """
    by_machine: Dict[str, Dict[str, int]] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    by_source: Dict[str, Dict[str, int]] = {}
    spend_machine: Dict[str, float] = {}
    spend_model: Dict[str, float] = {}
    exact_totals: Dict[str, int] = {f: 0 for f in TOKEN_FIELDS}
    estimated_totals: Dict[str, int] = {f: 0 for f in TOKEN_FIELDS}
    partial: List[Tuple[str, str]] = []

    # One machine, one day, one truth. Resolve every duplicate BEFORE summing —
    # deciding as we go would leave a superseded record already added to the
    # totals with no way to take it back out.
    winners: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in records:
        key = (record.get("machine", "unknown-machine"), record.get("date", ""))
        stamp = str(record.get("generated_at") or "")
        incumbent = winners.get(key)
        if incumbent is None or stamp > str(incumbent.get("generated_at") or ""):
            winners[key] = record

    by_counter: Dict[str, Dict[str, int]] = {}
    for (machine, day), record in winners.items():
        totals = {f: int(record.get("totals", {}).get(f) or 0) for f in TOKEN_FIELDS}
        slot = by_counter.setdefault(counter_of(record), {f: 0 for f in TOKEN_FIELDS})
        for field in TOKEN_FIELDS:
            slot[field] += totals[field]
        for target, name in ((by_machine, machine), (by_day, day)):
            slot = target.setdefault(name, {f: 0 for f in TOKEN_FIELDS})
            for field in TOKEN_FIELDS:
                slot[field] += totals[field]
        for source, fields in (record.get("sources") or {}).items():
            slot = by_source.setdefault(source, {f: 0 for f in TOKEN_FIELDS})
            for field in TOKEN_FIELDS:
                slot[field] += int(fields.get(field) or 0)
        # Recomputed from this clone's price table, not read from the record.
        for model, cost in spend_of(record.get("models") or {}, price_fn).items():
            spend_model[model] = spend_model.get(model, 0.0) + cost
            spend_machine[machine] = spend_machine.get(machine, 0.0) + cost
        for name, target in (("exact", exact_totals), ("estimated", estimated_totals)):
            fields = record.get(name) or {}
            for field in TOKEN_FIELDS:
                target[field] += int(fields.get(field) or 0)
        if record.get("partial"):
            partial.append((machine, day))

    winner_list = list(winners.items())
    return {
        "machines": by_machine,
        "spend": {
            "by_machine": spend_machine,
            "by_model": spend_model,
            "total": sum(spend_machine.values()),
            "priced": price_fn is not None,
        },
        "days": by_day,
        "sources": by_source,
        "totals": totals_of(by_machine),
        "exact": exact_totals,
        "estimated": estimated_totals,
        "confidence": _confidence(exact_totals, estimated_totals),
        "overlaps": detect_overlaps(winner_list),
        "counters": counter_cohorts(winner_list),
        "by_counter": by_counter,
        "anomalies": detect_anomalies(by_day),
        "partial": sorted(partial),
        "machine_count": len(by_machine),
        "day_count": len(by_day),
    }


def spend_of(
    models: Dict[str, Dict[str, int]], price_fn: Optional[Any] = None
) -> Dict[str, float]:
    """Cost per model for one machine-day's token breakdown.

    Cost is deliberately NOT stored in the daily records. Tokens are the
    measurement; dollars are a view over them, and the two age differently:
    prices get corrected (claude-opus-5 was billing at a fifth of its real rate
    until the table was fixed), introductory rates expire, and a machine
    running an older checkout has an older table. Freezing dollars at export
    time would bake each machine's pricing bugs into the fleet total
    permanently, and re-pricing history would mean rewriting every published
    file. Recomputing from tokens at read time means one price correction fixes
    every machine retroactively, including days exported months ago.

    price_fn is injected rather than imported because token_tracker runs its
    parsers at import; importing it here would make reading a JSON file cost a
    six-second scan of every agent log on the box.
    """
    if price_fn is None:
        return {}
    out: Dict[str, float] = {}
    for model, fields in models.items():
        # Translate to the billing function's field names. Passing the
        # invocation-record names silently prices cache tokens at zero, and
        # cache is 95% of this fleet's volume.
        bucket = {billed: int(fields.get(ours) or 0) for ours, billed in _BILL_KEYS.items()}
        try:
            cost, _source = price_fn(model, bucket)
        except Exception:
            continue
        out[model] = out.get(model, 0.0) + float(cost)
    return out


def _confidence(exact: Dict[str, int], estimated: Dict[str, int]) -> float:
    """Share of the fleet total that was measured rather than inferred."""
    measured = sum(exact.values())
    inferred = sum(estimated.values())
    return measured / (measured + inferred) if (measured + inferred) else 1.0


def detect_overlaps(
    winners: List[Tuple[Tuple[str, str], Dict[str, Any]]], threshold: float = 0.5
) -> List[Dict[str, Any]]:
    """Machine-days whose sketches say two boxes read the same calls.

    Independent machines share essentially no fingerprints, so any material
    Jaccard similarity means the same underlying log was counted twice — the
    one failure mode that inflates a fleet total while every individual file
    looks correct.
    """
    by_day: Dict[str, List[Tuple[str, List[int]]]] = defaultdict(list)
    for (machine, day), record in winners:
        raw = record.get("sketch") or []
        try:
            sketch = [int(v, 16) if isinstance(v, str) else int(v) for v in raw]
        except (TypeError, ValueError):
            continue
        if sketch:
            by_day[day].append((machine, sketch))

    found: List[Dict[str, Any]] = []
    for day, entries in by_day.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                score = jaccard(entries[i][1], entries[j][1])
                if score >= threshold:
                    found.append({
                        "date": day,
                        "machines": sorted((entries[i][0], entries[j][0])),
                        "jaccard": round(score, 3),
                    })
    return sorted(found, key=lambda d: (-d["jaccard"], d["date"]))


def detect_anomalies(by_day: Dict[str, Dict[str, int]]) -> List[Dict[str, Any]]:
    """Days whose spend is a genuine outlier, by modified z-score in log space.

    Two choices matter here, and both were measured against this repo's own
    history rather than assumed.

    First, robust statistics. Mean and standard deviation are the wrong tools:
    usage is bursty, and a single spike drags both, so the spike ends up hiding
    itself. The modified z-score (Iglewicz & Hoaglin) uses the median and the
    median absolute deviation, and neither moves when one day explodes.

        z = 0.6745 * (x - median) / MAD

    The 0.6745 factor makes MAD a consistent estimator of sigma for normal
    data, so the familiar 3.5 cutoff keeps its usual meaning.

    Second, log space. Daily token counts here span 3.3e5 to 1.8e8 — three
    orders of magnitude — because usage is multiplicative: an active day is a
    *multiple* of an idle one, not a fixed amount above it. On raw values that
    spread inflates MAD until nothing can clear the threshold (max |z| = 1.81
    across this fleet's 17 days, flagging nothing). On log10 values the same
    series peaks at |z| = 5.23. Logs also make the test symmetric in the way
    the domain actually is: a day at a hundredth of typical is exactly as
    notable as one at a hundred times, which is false on a linear scale.
    """
    series = [(day, sum(t.values())) for day, t in sorted(by_day.items())]
    # Below ~7 points the median and MAD are too unstable to call anything an
    # outlier; saying nothing beats saying something wrong.
    if len(series) < 7:
        return []

    # +1 keeps a zero-token day finite; at these magnitudes it changes nothing.
    values = [math.log10(v + 1) for _, v in series]
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    if mad == 0:
        # A flat majority: fall back to the mean absolute deviation, which the
        # same authors recommend when MAD degenerates.
        mad = statistics.mean(deviations) / 0.7979 if any(deviations) else 0.0
    if mad == 0:
        return []

    out = []
    for (day, raw), value in zip(series, values):
        z = 0.6745 * (value - median) / mad
        if abs(z) >= OUTLIER_Z:
            out.append({
                "date": day,
                "tokens": raw,          # report the real count, not its log
                "z": round(z, 1),
                # How many times typical this day was — the number a reader
                # actually wants, recovered from log space.
                "ratio": round(10 ** (value - median), 2),
                "direction": "spike" if z > 0 else "trough",
            })
    return sorted(out, key=lambda d: -abs(d["z"]))


# --- git transport --------------------------------------------------------

def _git(*args: str, cwd: Optional[Path] = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ("git",) + args,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            # `text=True` alone decodes with the console default — cp1252 on
            # Windows — and this now runs `git show HEAD:token_tracker.py`,
            # a UTF-8 file full of em dashes and ⚠. The decode raises inside
            # subprocess's reader THREAD, so `run` returns normally with
            # stdout=None and the caller dies on `None + str`. That took
            # `--fleet` and `--export` down entirely on every Windows box,
            # which is where the counting-version check needed to run most.
            # Same fault and same fix as NouGenRelay's _git.
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 1, str(exc)
    # And `subprocess.run` only guarantees str for these when it did the
    # capturing itself. A caller that redirects a stream — and every test that
    # fakes this call — gets None instead, so the coalesce is needed on top of
    # the encoding fix, not instead of it. Both machines found this hour, from
    # opposite ends: 21 faked-call test failures, and a live crash on Windows.
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def install_hooks(repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Point this clone at the tracked hooks directory.

    `core.hooksPath` lives in .git/config, which is per-clone and cannot be
    committed — so every fresh clone starts unhooked no matter what is baked
    into the repo. This is the one command that closes that gap, and nothing
    else is allowed to depend on someone having run it.
    """
    root = repo_root or REPO_ROOT
    if not (root / ".githooks" / "prepare-commit-msg").exists():
        return False, "no .githooks/prepare-commit-msg in this repo"
    code, out = _git("config", "core.hooksPath", ".githooks", cwd=root)
    if code != 0:
        return False, out or "git config failed"
    return True, "core.hooksPath -> .githooks"


def hooks_installed(repo_root: Optional[Path] = None) -> bool:
    root = repo_root or REPO_ROOT
    code, out = _git("config", "core.hooksPath", cwd=root)
    if code != 0 or not out:
        return False
    path = Path(out)
    if not path.is_absolute():
        path = root / path
    return (path / "prepare-commit-msg").exists()


def stamped_commit(subject: str, paths: List[Path], repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Commit the given paths with identity already in the message.

    The trailers are written here rather than left to prepare-commit-msg for
    the same reason NouGenRelay writes its own: this function already resolved
    the machine and agent to name the files, so making the stamp depend on a
    per-clone config step would only lose information the caller already had.
    `git interpret-trailers` and this use the same keys, so a hooked clone
    produces no duplicates.
    """
    root = repo_root or REPO_ROOT
    if not paths:
        return False, "nothing to commit"
    code, out = _git("add", "--", *[str(p) for p in paths], cwd=root)
    if code != 0:
        return False, out
    code, out = _git("diff", "--cached", "--quiet", cwd=root)
    if code == 0:
        return False, "no changes to commit"
    message = f"{subject}\n\nMachine: {resolve_machine()}\nAgent: {resolve_agent()}\n"
    return _git("commit", "-m", message, cwd=root)[0] == 0, "committed"
