# Provenance Guard

A backend service that any creative-sharing platform can plug into to classify
submitted text as human- or AI-written, score its **confidence** in that verdict,
surface a plain-language **transparency label**, and let creators **appeal** a
misclassification — with **rate limiting** and a structured **audit log** for
production safety.

The full design rationale lives in [planning.md](planning.md). This README is the
canonical record of what was built and how to run it.

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

echo "GROQ_API_KEY=your_key_here" > .env   # not committed (.gitignore)

python app.py        # serves on http://127.0.0.1:5000
```

---

## Architecture (overview)

```
POST /submit ─▶ rate limiter ─▶ Signal 1 (LLM) ─┐
                                Signal 2 (stylo) ─┴▶ confidence scorer
                                ─▶ transparency label ─▶ audit log ─▶ JSON response

POST /appeal ─▶ lookup decision ─▶ status: under_review ─▶ audit log ─▶ response
```

Full diagrams (both flows, labeled arrows) are in
[planning.md → Architecture](planning.md#2-architecture-diagram).

### API endpoints

| Endpoint   | Method | Body                                          | Returns |
|------------|--------|-----------------------------------------------|---------|
| `/submit`  | POST   | `{ "text", "creator_id" }`                    | `content_id`, `attribution`, `confidence`, `label`, both signal scores |
| `/appeal`  | POST   | `{ "content_id", "creator_reasoning" }`       | confirmation + `status: under_review` |
| `/log`     | GET    | `?limit=` (optional)                          | recent audit-log entries |
| `/appeals` | GET    | —                                             | reviewer queue (entries under review) |
| `/health`  | GET    | —                                             | `{ "status": "ok" }` |

Errors: `400` (missing/empty fields), `404` (unknown `content_id`), `429` (rate limit).

---

## Detection signals (multi-signal pipeline)

Two **genuinely independent** signals — one semantic, one structural — so the
combination is more informative than either alone.

### Signal 1 — LLM classifier (Groq `llama-3.3-70b-versatile`)
- **Captures:** holistic semantic + stylistic "feel" — whether the writing reads
  human or machine-generated (hedging, generic phrasing, predictability,
  idiosyncrasy).
- **Why chosen:** AI prose tends to be smooth, evenly developed, and cliché-prone;
  humans take idiosyncratic risks. A capable model picks this up globally.
- **Output:** `{ p_ai: 0–1, rationale }`. Abstains (`p_ai=0.5`) on API/parse error.
- **Blind spot:** can be coaxed to call AI text "human," is non-deterministic, and
  has no ground truth — it's an AI guessing about AI.

### Signal 2 — Stylometric heuristics (pure Python, deterministic)
Three sub-metrics, each mapped to a "looks-AI" score in `[0,1]`, then averaged:
- **Burstiness** (sentence-length variance): humans mix short/long sentences; AI
  is uniform. Low variance → looks AI.
- **Type-token ratio** (lexical diversity, capped 100-word window): AI clusters in
  a moderate "safe" band; humans deviate.
- **Punctuation richness**: expressive humans reach for `—`, `;`, `?`, `…`; AI
  leans on commas/periods. Low variety → looks AI.
- **Output:** `{ p_ai: 0–1, metrics }`. **Abstains on text < 40 words** (variance
  and TTR are statistical noise on short text).
- **Blind spot:** genre-naive — haiku, legal/technical prose, or heavily-edited
  human drafts can be statistically uniform and score AI-ish.

---

## Confidence scoring (uncertainty, not a binary)

The pipeline combines the signals into a **probability of AI (`p_ai`)**, then maps
that to a verdict and a **confidence in the verdict** — *not* a hard flip at 0.5.

```
p_ai = 0.6 · p_llm + 0.4 · p_stylo
```
- LLM weighted higher (stronger semantic signal); stylometry is the independent
  structural check.
- **Disagreement penalty:** if the signals disagree sharply (`|Δ| > 0.4`), `p_ai`
  is pulled halfway back toward 0.5 — disagreement *is* uncertainty.
- **Abstention:** if one signal is unusable, fall back to the other and **cap
  reported confidence at 0.70**.

| `p_ai` range  | verdict        | reported confidence = `\|p_ai − 0.5\| × 2` |
|---------------|----------------|--------------------------------------------|
| `0.00–0.34`   | `likely_human` | high                                       |
| `0.34–0.66`   | `uncertain`    | low by construction                        |
| `0.66–1.00`   | `likely_ai`    | high                                       |

**False-positive asymmetry:** on a writing platform, wrongly accusing a human is
the costlier error, so the `likely_ai` bar (0.66) is deliberately high — we'd
rather land in `uncertain` than falsely accuse.

### How I tested that scores are meaningful
Ran a labeled corpus (clearly-AI, clearly-human, two borderline) and confirmed the
scores spread across all three bands instead of collapsing to a binary:

| Input                        | LLM | Stylo | `p_ai` | verdict        | confidence |
|------------------------------|-----|-------|--------|----------------|------------|
| Clear AI (formal essay)      | 0.80| 0.58  | 0.712  | `likely_ai`    | 0.42       |
| Clear human (casual review)  | 0.20| 0.46  | 0.302  | `likely_human` | 0.40       |
| Borderline: formal human     | 0.70| 0.60  | 0.662  | `likely_ai`*   | 0.32       |
| Borderline: lightly-edited AI| 0.40| —     | 0.400  | `uncertain`    | 0.20       |

Clear AI (0.712) vs clear human (0.302) are **0.41 apart** — meaningfully
different. *The formal-human case lands at *low-confidence* `likely_ai` — a known
blind spot (see Known limitations) handled honestly via low confidence + the
appeal path.

### Two worked examples (different confidence, real scores)

**Higher-confidence classification** — a casual restaurant review:
> *"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium…"*

| llm | stylo | combined `p_ai` | verdict | **confidence** |
|-----|-------|------|---------|----------------|
| 0.20 | 0.35 | 0.26 | `likely_human` | **0.48** |

Both signals agree it's human (informal tone, high sentence-length variance →
stdev 7.5), so confidence is near the top of what blending produces.

**Lower-confidence classification** — a short, lightly-edited AI paragraph:
> *"I have been thinking a lot about remote work lately. There are genuine tradeoffs. Studies show productivity varies widely."*

| llm | stylo | combined `p_ai` | verdict | **confidence** |
|-----|-------|------|---------|----------------|
| 0.40 | abstained | 0.40 | `uncertain` | **0.20** |

The text is under 40 words so stylometry abstains, and the LLM sits near the
fence — the system reports `uncertain` at low confidence rather than guessing.

The **0.48 vs 0.20** gap is the proof the score is meaningful, not a constant.

### What I'd change before deploying this for real
- **Calibrate against a labeled dataset.** The confidence is a principled
  distance-from-fence heuristic, not a calibrated probability; blending also
  compresses the range (max ~0.6). I'd fit a calibration curve (e.g. Platt
  scaling) on labeled human/AI text so "80% confident" is empirically true.
- **Stronger, more weighting-aware ensemble** (perplexity, multiple model
  judges with a voting scheme) rather than two signals.
- **Persistent, per-creator rate limiting** (Redis-backed, keyed on
  authenticated user, not just IP) and **auth** on `/log` / `/appeal`.
- **Prefer provenance over detection** where possible — cryptographic content
  credentials (C2PA) are far more reliable than post-hoc statistical guessing.

---

## Transparency label (three variants)

The submission endpoint returns one of three labels, selected by verdict, with the
confidence percentage interpolated. **Verbatim text:**

> **High-confidence AI** (`likely_ai`)
> 🤖 **Likely AI-generated** — Our analysis suggests this text was probably created with AI assistance (confidence: {pct}%). This is an automated estimate, not a certainty. If you wrote this yourself, you can appeal this label.

> **High-confidence human** (`likely_human`)
> ✍️ **Likely human-written** — Our analysis found no strong signs of AI generation in this text (confidence: {pct}%). This is an automated estimate and not a guarantee of authorship.

> **Uncertain** (`uncertain`)
> ❓ **Inconclusive** — Our signals disagree or are too weak to call this text human- or AI-written with confidence (confidence: {pct}%). We're showing this openly rather than guessing. If a label is later applied, you can appeal it.

`{pct}` = `round(confidence × 100)`. Every label states it's an *automated
estimate*, never an accusation, and surfaces the appeal path where a creator could
be harmed.

---

## Appeals workflow

- **Who:** the creator of a submission (holds the `content_id`; a real platform
  would gate by authenticated author).
- **They provide:** `content_id` + free-text `creator_reasoning`.
- **System does:** looks up the original decision (404 if unknown), flips its
  status `classified → under_review`, appends a linked **appeal** entry to the
  audit log (reasoning + a copy of the original verdict/confidence), and returns a
  confirmation. **No automated re-classification** — resolution is human.
- **Reviewer view:** `GET /appeals` returns the queue of items under review with
  the creator's reasoning and the original scores.

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-ID", "creator_reasoning": "I wrote this myself..."}'
```

---

## Rate limiting

Applied to `POST /submit` (the LLM-cost-bearing endpoint) via Flask-Limiter,
in-memory storage:

| Limit          | Reasoning |
|----------------|-----------|
| **10 / minute**| A real creator submits a handful of pieces per session, not dozens per minute. 10/min is generous for genuine bursts (revising and resubmitting) while stopping a script from hammering the endpoint. |
| **100 / day**  | Caps sustained abuse — an adversary can't cheaply drain Groq quota over a day — while staying well above any plausible single-creator daily volume. |

Read endpoints (`/log`, `/appeals`) are unthrottled (cheap, documentation-facing).

### Evidence (12 rapid requests, limit 10/min)
```
request 1  -> 200
...
request 9 -> 200
request 10 -> 429
request 11 -> 429
request 12 -> 429
```
> Note: the in-memory limiter requires a single process, so the app runs with
> `use_reloader=False`. Flask's debug reloader otherwise splits the count across
> two processes and undercounts.

---

## Audit log

Every decision and appeal is written to a structured SQLite log
(`provenance.sqlite`, gitignored). Each entry records: timestamp, `content_id`,
`creator_id`, attribution, confidence, **both individual signal scores**
(`llm_score`, `stylometric_score`), the combined `combined_score`, status, the
label shown, and (for appeals) the creator's reasoning.

### Sample (`GET /log`) — 4 entries incl. an appeal

Columns show, per entry: **timestamp**, **attribution**, **confidence**, both
individual signal scores (llm / stylo), the combined score, and status.

| id | timestamp (UTC)            | type     | content_id | attribution   | confidence | llm / stylo / combined | status        | reasoning |
|----|----------------------------|----------|------------|---------------|------------|------------------------|---------------|-----------|
| 4  | 2026-06-27T22:39:53.841Z   | appeal   | fe2aa20e   | likely_ai     | 0.423      | 0.8 / 0.579 / 0.712    | under_review  | "I wrote this myself from personal experience…" |
| 3  | 2026-06-27T22:39:53.814Z   | decision | 81507fcd   | uncertain     | 0.200      | 0.4 / 0.557 / 0.400    | classified    | — |
| 2  | 2026-06-27T22:39:53.363Z   | decision | c641177f   | likely_human  | 0.479      | 0.2 / 0.351 / 0.260    | classified    | — |
| 1  | 2026-06-27T22:39:53.109Z   | decision | fe2aa20e   | likely_ai     | 0.423      | 0.8 / 0.579 / 0.712    | under_review  | — |

Note id 1 (the original decision) flipped to `under_review` after the appeal (id 4)
was filed. Raw JSON for one decision entry:

```json
{
  "id": 2, "entry_type": "decision", "content_id": "c641177f-...",
  "creator_id": "u-human", "timestamp": "2026-06-27T22:39:53.363Z",
  "attribution": "likely_human", "confidence": 0.479,
  "llm_score": 0.2, "stylometric_score": 0.351, "combined_score": 0.26,
  "status": "classified",
  "label": "✍️ Likely human-written — ... (confidence: 48%) ...",
  "details": {
    "llm_rationale": "The text's informal tone ... suggest a human author.",
    "stylometric_metrics": { "sentence_len_stdev": 7.52, "type_token_ratio": 0.932, "distinct_punct": 1 },
    "disagreement": 0.151, "one_signal_only": false
  }
}
```

---

## Known limitations & edge cases

**The case it would most likely get wrong: formal human prose** (academic
abstracts, legal clauses, corporate writing). This is a direct consequence of
*both* signal properties, not bad luck: the stylometric signal rewards
*uniformity* (low sentence-length variance, sparse punctuation variety) as an
AI tell — but careful formal human writing is *also* uniform. And the LLM signal
independently leans "AI" on dry, hedge-heavy prose because that's what a lot of
its AI training examples look like. So both signals fail in the *same* direction,
the disagreement penalty (which only fires when signals diverge) never triggers,
and a real human can land at low-confidence `likely_ai`. This is exactly the
asymmetric error a writing platform cares about, which is why the `likely_ai` bar
is high, the confidence is reported honestly low, and every AI label carries the
appeal path.

Other known edge cases:
- **Constrained human poetry (haiku/repetition):** simple vocabulary + repetition
  push stylometry toward "uniform." Short-text abstention + the disagreement
  penalty steer toward `uncertain`.
- **Lightly human-edited AI:** signals disagree → penalty pulls to `uncertain`,
  the honest answer.
- **Very short text (<40 words):** stylometry abstains, confidence capped at 0.70.

---

## Spec reflection

- **How the spec guided the build:** writing the three label variants *verbatim in
  planning.md before any code* forced the confidence model to exist first. Because
  I'd already committed to an `uncertain` label, I had to design a *three-band*
  scorer (with a disagreement penalty feeding the middle band) instead of a binary
  threshold — the spec made the "communicate uncertainty" requirement structural,
  not an afterthought.
- **Where the implementation diverged:** planning.md §4 originally defined reported
  confidence as `1 − p_ai`. In implementation I switched to `|p_ai − 0.5| × 2`,
  because `1 − p_ai` gives a *high* number to a confident-human result and a *low*
  number to a confident-AI result — it measures "how human," not "how sure." The
  distance-from-fence formula is symmetric and actually means "confidence in the
  verdict," which is what the labels promise. I updated planning.md to match.

---

## AI usage

I used Claude (via Claude Code) as the implementation tool, driven by sections of
planning.md. Two specific instances:

1. **Stylometric signal + scoring (M4).** I directed it to generate
   `signal_stylometric` and `combine()` from the §3/§4 spec. It produced working
   code, but the initial type-token-ratio mapping barely discriminated on short
   text (every sample scored ~0.87 raw TTR). I caught this by printing per-metric
   sub-scores on the labeled corpus, re-centered the TTR mapping, and documented
   TTR as the weakest sub-metric rather than pretending it was load-bearing —
   burstiness and punctuation do most of the work.
2. **Rate limiting (M5).** I directed it to add Flask-Limiter at `10/min;100/day`.
   The first test showed only 7 requests passing before `429`, not 10. I traced it
   to Flask's debug **reloader** running a second process that split the in-memory
   counter, and overrode the generated `app.run(debug=True)` with
   `use_reloader=False` so the limiter counts correctly (verified: 10×200, 2×429).

---

## Project layout

| File          | Role |
|---------------|------|
| `app.py`      | Flask API: routes, rate limiting |
| `signals.py`  | Signal 1 (LLM) + Signal 2 (stylometric) |
| `scoring.py`  | Confidence scoring / signal combination |
| `labels.py`   | Transparency-label generation |
| `audit.py`    | SQLite audit log (decisions + appeals) |
| `planning.md` | Full spec, architecture diagrams, design rationale |

--- 

## Demo Video

Here you can watch the [demo video](https://youtu.be/858mIONSx7U)!
