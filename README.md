<!-- nougen-readme-compiler: v1 -->

<!-- nougen-readme-compiler: extensions: footer -->

# 📊 NouGenTracker

A single-file, cross-provider AI token usage tracker. It reads the local logs each coding agent already writes and reconciles them into one honest, cache-aware report — input / output / cache-read / reasoning tokens by provider, by day, over any range.

## 📚 Contents
- [Why](#why)
- [Providers covered](#providers-covered)
- [Usage](#usage)
- [Forward tracking for local/free lanes](#forward-tracking-for-localfree-lanes)
- [Design notes](#design-notes)
- [Fleet totals](#fleet-totals)
- [Working on this repo from more than one machine](#working-on-this-repo-from-more-than-one-machine)
- [Fleet role](#fleet-role)

## ❓ Why

Every AI coding tool counts tokens differently and hides them in a different place. Claude Code, OpenAI Codex, the Gemini CLI, and Google Antigravity each keep their own store with their own schema and their own definition of "a token." NouGenTracker treats those disparate histories as **one body of work** and produces a unified ledger, so you can see what the whole fleet actually spent — and what it *would* have cost at first-party API list prices (a usage gauge, not a bill).

No telemetry, no network calls for the core report — it parses what's already on disk.

## 📡 Providers covered

| Lane | Source | Tokens |
|---|---|---|
| **Anthropic (Claude Code)** | `~/.claude/projects/**/*.jsonl` | exact |
| **OpenAI (Codex)** | `~/.codex/state_5.sqlite` + rollout logs | exact (+ native `tokens_used` cross-check) |
| **Google (Gemini CLI)** | `~/.gemini/tmp/*/chats/session-*.json[l]` | exact via the `tokens` block |
| **Google (Antigravity)** | `~/.gemini/antigravity*/brain/**/transcript.jsonl` | estimated (chars÷4) |
| **Fleet ledger** (local Ollama/Gemma, OpenRouter, HF) | `vault/fleet_usage.jsonl` | exact, see `fleet/` |

## 🚀 Usage

```bash
python token_tracker.py                       # last 2 days (default)
python token_tracker.py --days 7              # last 7 days
python token_tracker.py --weeks 2             # last 14 days
python token_tracker.py --month 2026-06       # a calendar month
python token_tracker.py --start 2026-06-01 --end 2026-06-15
python token_tracker.py --compare 7           # last 7d vs prior 7d, per provider
python token_tracker.py --by-provider         # add the by-provider summary

# Exact lower bound (must be tz-aware ISO):
TOKEN_TRACKER_CUTOFF="2026-06-29T06:00:00-04:00" python token_tracker.py --by-provider
```

### What you get
- Per-provider and per-day token tables.
- An **API-equivalent shadow bill** with cache-reads priced as cache-reads (not as fresh input), so the number is honest rather than inflated.
- Cache-health, model-class, and "cold context leak" route hints.

### Sol 6.1 pricing scope

Verified against [OpenAI's GPT-6.1 Sol model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol) on September 29, 2026: Standard rates per million tokens are **$2 input, $0.10 cached input, $2.50 cache writes, and $10 output**. The exact `gpt-6.1-sol` model has a documented fallback when live pricing is unavailable. Reasoning effort suffixes, including `xhigh`, `max`, and the Codex app's `ultra` label, resolve to the base model with inferred-price provenance.

These are API-equivalent Standard estimates. OpenAI specifies full-request premiums above 272,000 input tokens (2x input/cache and 1.5x output), Fast at 2x Standard, Batch/Flex at half Standard, and regional processing at a 10% premium where available. Daily totals cannot identify those request boundaries or service tiers, so this tracker does not apply those multipliers to aggregated tokens. This table does not establish ChatGPT/Codex subscription credit limits.

### Sourced model catalog

The offline catalog covers **183 model records and 84 aliases**, verified on September 29, 2026: 110 OpenAI, 33 Claude, and 40 Google records. Sources are [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), and [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing), together with linked official model and lifecycle pages. Each record carries its source and verification date. Snapshot aliases that inherit a parent's price remain estimates.

```bash
python pricing_catalog.py --model gpt-6.1-sol
python pricing_catalog.py --model gemini-3.8-flash --date 2027-01-01
python pricing_catalog.py --model claude-opus-5-5
```

The tracker uses Standard text rates; the catalog keeps audio, image, video, embedding, cache writes, and storage charges in separate fields. Option-dependent video/music charges retain their published price rows. Long-context thresholds are metadata: daily aggregates cannot reconstruct request boundaries. Nine retired Claude 1/2/Instant IDs have no usable official rate and are explicitly unpriced; open-weight models retain host-dependent status. Unknown rates are never advertised as documented prices.

Sonnet 5 remains $2/$10 per million input/output tokens because its announced increase was canceled. GPT-5.6 Sol is $4/$20. Published Gemini promotional rates and cache storage transition on January 1, 2027; dated lookups select the correct band. No runtime deployment or subscription allowance change is implied by this catalog.

## 📡 Forward tracking for local/free lanes

Local Ollama/Gemma calls and OpenRouter/HF requests don't persist token counts anywhere, so they're invisible to a log parser. The `fleet/` components fix that **going forward**:

- **`fleet/fleet_usage_proxy.py`** — a transparent logging proxy that sits on Ollama's port, forwards to a relocated upstream, and records each response's `prompt_eval_count` / `eval_count` to an append-only ledger. Inference correctness first: bytes are forwarded faithfully; logging is a best-effort side effect.
- **`fleet/fleet_usage_log.py`** — the append-only JSONL ledger writer (`vault/fleet_usage.jsonl`). `token_tracker.py` reads it as the Fleet provider lane.

Point your OpenAI-compatible / Ollama clients at the proxy and the local lanes start showing up in the report. Config via `FLEET_PROXY_PORT`, `FLEET_OLLAMA_UPSTREAM`, `FLEET_USAGE_LEDGER`.

## 📝 Design notes

- **Trust the tool's own counter.** Where a provider records exact tokens (Claude usage blocks, Codex `tokens_used`, the Gemini CLI `tokens` block), the tracker uses them and only estimates when there is genuinely nothing on disk.
- **A request is the unit, not a log row.** Claude Code writes one row per content block — the assistant message, then one per `tool_use` — and every row repeats the *same* usage object under a fresh `uuid`. Deduping by uuid dedupes nothing. Measured on one box 2026-08-01: 652 of 1,136 requests spanned multiple rows, usage identical in every one, inflating cache-reads from 287M to 554M. Dedup is by `requestId` (the unit the provider bills), with `uuid` as the fallback. The bug scaled with how many tools a session called, so the busier the work, the more wrong the report.
- **Findings are computed or absent.** The report's closing section reads the window's own invocations — spend concentration, cache hit rate per lane, caches written but never reused, estimated-vs-exact confidence, and days above 3x their lane's own median. Each line carries the number it came from. When nothing crosses a threshold it says so, because advice that fires unconditionally teaches people to skip the section.
- **Cache discipline is the whole economy.** Cache-reads dominate token *counts* but bill at ~10% of input; pricing them correctly is the difference between an honest gauge and a scary-but-meaningless number.
- Pricing tables are tunable knobs (`DOC` = first-party documented, `EST` = estimate).

## 👥 Fleet totals

`token_tracker.py` reads the logs on the box it runs on. That answer never leaves the machine, so the fleet-wide question had no answer. Dailies are the transport: each machine exports its own days, git carries them, every machine sums them.

```bash
python3 token_tracker.py --install-hooks          # once per clone (see below)
NOUGEN_MACHINE=phoebus python3 token_tracker.py --publish
git push -u origin dailies/phoebus                # then open a PR

python3 token_tracker.py --fleet                  # totals across every machine
```

Each machine writes only `dailies/<machine>/<YYYY-MM-DD>.json`, so two boxes pushing at once cannot conflict. Re-exporting a day replaces that day's file, so a re-run can never double-count.

To publish on a schedule, run `--publish` from cron or launchd on each box. It is deliberately not a GitHub Action: a runner has no access to the agent logs on your machines, so the export has to happen where the logs are.

### Passive live status

The live health path is deliberately separate from collection and publishing:

```bash
python3 tracker_live.py
python3 tracker_live.py --json
```

It lists canonical `dailies/<machine>/<date>.json` names and reads at most the newest aggregate record for each expected machine. It never reads raw agent logs, starts `token_tracker.py`, creates a cache, writes a file, reaches the network, publishes a daily, or restarts a service. The output is publication freshness only. A stale or missing machine is unknown; it is never evidence of zero usage.

Set `NOUGENTRACKER_EXPECTED_MACHINES` to a comma-separated fleet list or pass `--machine` repeatedly. Collection and `--publish` remain explicit batch work and are not triggered by the live status path.

### Dollars are a view, not a record

Daily files store **tokens only**. Spend is recomputed from those tokens at read time, using the price table in the clone doing the reading.

This matters because prices and measurements age differently. `claude-opus-5` billed at a fifth of its real rate until the table was fixed; introductory rates expire; a machine on an older checkout has an older table. Freezing dollars into each daily file would bake every machine's pricing bugs into the fleet total permanently, and correcting a price would mean rewriting every published file. Recomputing means one table fix re-prices every machine retroactively, including days exported months ago.

### Machine identity

Resolved the way NouGenRelay resolves it — `NOUGEN_MACHINE`, else the hostname, lowercased and slugified — so one grep finds a box's commits, its handoffs, and its dailies. **Set `NOUGEN_MACHINE` if the box already has a fleet name.** One machine under two names is counted twice, and the total is wrong in the direction that looks plausible; `--export` warns on a name the fleet has never published under.

`--install-hooks` is per-clone because `core.hooksPath` lives in `.git/config`, which cannot be committed. Nothing depends on someone having run it: the tool stamps its own commits with `Machine:`/`Agent:` trailers, and the hook only covers commits written by hand.

### What the numbers say about themselves

- **confidence** — the share of the total that was *measured* rather than inferred from text length. Some providers report usage; others are inferred from text length. These are tracked separately, because folding a guess into a measurement produces a number you cannot reason about. On this fleet only ~26–32% of reported tokens are measured (the source README carries both figures from different passes).
- **overlap** — if two machines read the same synced log directory, both report the same calls and a plain sum doubles them. Every machine-day carries a 64-wide bottom-k MinHash sketch of its call fingerprints; the aggregator estimates Jaccard similarity and says so when it is high. ~0.5 KB per machine-day regardless of call volume.
- **outlier days** — modified z-score (median + MAD, Iglewicz & Hoaglin) rather than mean and standard deviation, which one spike would drag until it hid itself. Computed on log10 of the daily total: usage is multiplicative and spans three orders of magnitude here, and on raw values MAD inflates until nothing clears the threshold (max |z| = 1.81 across this fleet's 17 days — silence). In log space the same series peaks at 5.23.
- **counting version** — `schema` says a daily file can be *read*. It says nothing about whether its numbers can be *compared*. On 2026-08-01 the Claude parser stopped billing one API request once per content block; the file format did not change, so every daily already published stayed schema-valid and kept summing into the fleet total while meaning something different. Each daily now carries a `counter` — a digest of the AST of the functions that decide what gets counted, so it moves when a dedup key changes and *doesn't* move when a comment does, and nobody has to remember to bump it. When the published files disagree, `--fleet` prints per-cohort totals and refuses to print one FLEET line, and `--export` tells the machine which of its own days to re-export. Unstamped files are never "current": that is the absence of a version, not a version, and a corpus of nothing but unstamped files is the most wrong a fleet total can be.

## 🔄 Working on this repo from more than one machine

This repo is worked from several boxes. Two mechanisms keep them from doing each other's work twice, both carried by git — no server, no daemon.

**Before you start**, announce it:

```bash
relay check                                    # has another machine moved?
relay claim take -s token_tracker.py -g "fix pricing table"
# ... work ...
relay claim release -s token_tracker.py
relay create -g "what you did" -m "where you left off"
```

Records land in `.handoffs/`. Install the CLI from [NouGenRelay](https://github.com/who-visions/nougenrelay); if `relay` isn't on PATH after `pip install -e .`, `python -m nougen_relay` always works.

**Commits carry their origin.** `.githooks/prepare-commit-msg` stamps every commit with `Machine:` and `Agent:` trailers, so `git log` answers "which box, which lane" rather than just "which GitHub account":

```bash
python3 token_tracker.py --install-hooks
```

This is per-clone — `core.hooksPath` lives in `.git/config` and cannot be committed — so a fresh clone starts unstamped until someone runs it. Nothing depends on it: `--publish` stamps its own commits, and the hook only covers commits written by hand.

Skipping `relay check` is not free. The route-recommendations rewrite in this repo was written twice on two machines on the same afternoon, and one of the two was thrown away.

## Fleet role

| | |
|---|---|
| Role | usage telemetry |
| Kind | core |
| Status | canonical |
| Canonical for | telemetry |
| Visibility | public |

This role table is generated from the NouGenRelay fleet manifest; the fleet manifest stays the source of truth for it.

## 🔗 Links

- [Source Repository](<https://github.com/Who-Visions/NouGenTracker>)

Part of the NouGenAi / Who Visions fleet. Built for the Stadium.
