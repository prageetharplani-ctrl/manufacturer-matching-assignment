# Veridia manufacturer matcher

Takes a buyer's requirement and returns a **ranked, explainable shortlist** from the manufacturer CSV.
Python 3.11+, **no third-party dependencies**.

```bash
make run            # = python -m veridia match --example buyer_de
```

That is the only command a newcomer needs. Other entry points:

| Command | What it does |
|---|---|
| `python -m veridia match --example buyer_de` | the German aluminium buyer (also `buyer_a`, `buyer_b`) |
| `python -m veridia match --requirement my.json [--as-of 2026-12-01] [--json]` | your own requirement |
| `python -m veridia why-not VM1006 --example buyer_a` | why a given supplier did or did not match |
| `python -m veridia quality` | data-quality report (what was wrong in the CSV and what we did about it) |
| `python -m veridia demo` | the two-buyer capacity scenario, end to end |
| `python -m veridia ask "3000 aluminium CNC parts, ISO 9001, 6 weeks"` | free text (needs `ANTHROPIC_API_KEY`; shows what it understood and asks you to confirm) |
| `python -m veridia serve` | small JSON HTTP API (see `veridia/api.py` for routes) |
| `make test` | 100 tests, stdlib `unittest` (pytest also works) |

A requirement is a small JSON object (`examples/`):

```json
{"material": "aluminium", "process": "CNC machining", "quantity": 3000,
 "deadline_weeks": 8, "certifications": ["ISO 9001"], "batches": 4, "buyer_country": "Germany"}
```

Typos are caught with suggestions (`steel` -> *did you mean mild steel, stainless steel?*). Zero, negative, absurd,
wrongly-typed, unknown or contradictory values are rejected with a message per field; nothing is guessed.

## Reading a result

```
Requirement: 3,000 x aluminium, CNC machining, 8 weeks, certifications: ISO 9001   (as of 2026-10-28)
31 of 1000 manufacturers match.

1. VM1225  Konkan Micro Industrial Parts (Coimbatore)   score 82.23
     - lead_time: needs <= 8 weeks | has 4 weeks
     - order_range: needs 3,000 units | has accepts 500-5,000
     ! (warnings: certificate expiring soon / expires before delivery / capacity held by others)
     ~ (caveats: data quality notes about this supplier)
How the list narrowed:
   1000  all usable manufacturers
    241  material: aluminium  (-759)   ...
```

* **Reasons**: every criterion with what the buyer asked and what the supplier has. A match shows all seven passing.
* **Score (0-100)** with a per-component breakdown in `--json` (`score_breakdown`): lead-time margin, on-time delivery,
  certificate runway after expected delivery, spare capacity, export experience. Weights live in `config.toml`.
  A missing input is left out and the other weights re-normalise, so missing data is neither rewarded nor punished.
* **Warnings** are things that can bite later (a certificate valid today but expiring before delivery);
  **caveats** are data-quality notes on that supplier.
* **No match?** You never get a bare empty list: you get the funnel, the most restrictive requirement, and the smallest
  relaxation that works, e.g. *extend the deadline to 8 weeks -> 2 matches*.

## Tracing why you got a result

* Every result carries `as_of`, `dataset_sha256` and a `request_fingerprint` (a hash of requirement + date + dataset +
  weights + capacity holds). **Same fingerprint, same output, byte for byte.**
* Every request is appended to `logs/requests.jsonl` with a `request_id`, the requirement, `as_of`, dataset hash,
  fingerprint and the returned supplier keys.
* `why-not <id>` replays one supplier against every criterion.

## What was wrong with the data, and what we did

| Finding | Rows | Handling |
|---|---|---|
| `manufacturer_id` reused by two different companies | 20 | kept both; unique internal keys `VM1092#1` / `#2`; flagged |
| Lead time blank (weeks and days) | 10 | kept; cannot satisfy any deadline; shown in funnel / `why-not` |
| On-time delivery above 100% (up to 142.6) | 10 | impossible, so treated as missing for scoring (not clipped) |
| One expiry date for suppliers with two certifications | 346 | applied to all their certifications (conservative); caveat shown |
| Minimum order above one month of capacity | 13 | flagged only |
| Letter rating (A/B/C) does not track on-time % (bands overlap) | all | score uses the number; letter ignored |
| Material and process are listed independently | all | trusted as declared; standing note on every result |
| Expired certificates (3 on 2026-10-28; 62 expire within 60 days) | 3 / 62 | depends on the date, so computed per request, never baked into the data |

The loader also handles what *this* file does not contain, because the next file might: spelling variants
(`Aluminum`, `al`), whitespace, BOM/CRLF, non-ISO or missing dates, negative or non-numeric numbers, exact duplicate
rows, unknown tokens, unusable rows (excluded **and reported**). Nothing is dropped or repaired silently.

## Configuration (`config.toml`)

`as_of` (fixed reference date, **2026-10-28**), expiry-warning window, weeks-per-month, input limits, hold TTL, weights.
Override the date per request with `--as-of`. The date is a fixed config value, not "today", so a request asked next
week gives the same answer unless you deliberately move it.

## Secrets

None in the repo. `ANTHROPIC_API_KEY` is read from the environment only (see `.env.example`; the app does not load
`.env` itself, export the variable). With no key everything works except free-text input, which says so and asks for
the structured fields instead.

## DECISIONS

**1. Deterministic engine; AI only at the edges.** Filtering and ranking are plain code: reproducible, testable, and
explainable to a buyer or a supplier who disputes the result. A model is used in exactly two optional places: turning
free text into the structured fields, and rephrasing the explanation. Its output is untrusted: strict schema, unknown
keys rejected, any number the buyer never wrote is dropped and asked for again, fields not literally stated are marked
*inferred* for the buyer to confirm, and a rephrased summary is discarded if it contains a number that is not in the
facts. If the model is down, nothing else changes.

**2. Fixed reference date.** Certificate validity depends on when you ask, so `as_of` is explicit and echoed. A
certificate valid on `as_of` but expiring before the supplier's expected delivery still matches but carries a warning,
because the buyer decides how much that matters. (Making it a hard exclusion is a one-line change.)

**3. Capacity: matching is read-only; reserving is explicit.** Seeing a supplier reserves nothing. A buyer reserves
capacity by placing a **hold**: first come, first served, atomic under one lock, expiring after 30 minutes. Others then
see the reduced capacity plus a warning, never *who* holds it. Revising a requirement releases that session's holds.
*Why:* auto-reserving on every search would let browsing buyers starve each other. *Real data shows the problem:* Buyer B
(stainless sheet metal) has exactly **one** candidate, VM1006, which is also Buyer A's rank-31 option of 34. If A
commits first, B is left with zero matches and is shown the way out (extend to 8 weeks, or 1,078 units). If both try
at once, exactly one wins; this is tested 30 times with a barrier.

**4. Scope: a CLI first, a minimal API second.** A reviewer can run one command; a service is a thin wrapper over the
same function. Standard library only, so it runs anywhere and tests are runnable without installing anything.

**5. Hard filters, then a transparent score.** A supplier either meets the requirement or does not; the score only
orders suppliers that do. The weights reflect the brief (certification and speed matter) and are one file to change.

### Left out on purpose

* **Batch cadence** (`batches` is accepted and echoed, but only the first delivery is matched). No data on ongoing
  throughput commitments; pretending otherwise would be false precision.
* **Price, shipping to Germany, supplier geography**: not in the data.
* **`last_audit_date`**: loaded by no one; audit recency is a plausible quality signal but I could not justify a weight.
* **Verifying that a supplier machines *this* material with *this* process**: the data cannot say; surfaced as a note.
* **Scarcity-aware allocation** (e.g. protect B's only supplier from A, who has 33 others): needs cross-buyer visibility
  and a fairness rule. That is a business decision, not an engineering one; FCFS is the simple, defensible default.
* **UI, auth, rate limiting, persistence.**

### Known limitations

* Sessions and holds live in memory in one process; a restart or a second worker loses them. Multi-worker use needs a
  shared store with the same atomic check-and-reserve.
* A hold reserves raw units against the holder's own deadline window; two buyers with very different deadlines are only
  approximately comparable.
* "Most restrictive requirement" is order-independent (suppliers passing each rule alone), but ties resolve in a fixed order.
* The dataset is mock; scores show relative order, not calibrated probabilities.

## Tests

`make test` runs 100 tests in about 4 seconds, no network and no API key needed.

| File | Covers |
|---|---|
| `test_data.py` | every dirty-data category with a row-level example; known counts on the real file |
| `test_requirement.py` | zero/negative/absurd/wrongly typed/unknown/contradictory inputs, injection-style strings |
| `test_matching.py` | each hard filter and boundary, expiry vs `as_of`, valid-today-expired-by-delivery, scoring order, tie-breaks, funnel, relaxations, `why-not`, byte-identical reruns |
| `test_concurrency.py` | 40 threads vs capacity, hold expiry, A/B race on the same supplier, concurrent pure matching, session revisions and diffs, audit log |
| `test_llm.py` | valid / malformed / unavailable / hallucinated model output, inferred vs explicit, summary guard (all mocked) |
| `test_interfaces.py` | CLI exit codes and output, HTTP API errors and the 409 contention path |

To check the tests can fail, five deliberate bugs were injected (expiry off-by-one, lead-time boundary, holds that never
reject, removed tie-break, accepting invented numbers); each was caught.
