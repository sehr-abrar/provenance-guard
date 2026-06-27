# Provenance Guard — Planning & Spec

A backend service that any creative-sharing platform can plug into to classify
submitted text as human- or AI-written, score its confidence in that verdict,
surface a plain-language transparency label, and let creators appeal a
misclassification. This document is the spec I write *before* any implementation
code, and the reference I hand (section by section) to AI tools in M3–M5.

---

## 1. Architecture Narrative (the path one piece of text takes)

A creator (or the platform on their behalf) sends raw text to **`POST /submit`**.
The request first passes through the **rate limiter**, which rejects callers that
exceed the configured budget before any expensive work happens. The text then
enters the **detection pipeline**, which runs two independent signals:

1. **Signal 1 — LLM classifier (Groq):** the text is sent to
   `llama-3.3-70b-versatile` with a structured prompt asking it to judge how
   AI-generated the writing reads and to return a probability plus a short
   rationale. This captures *semantic and stylistic coherence* holistically.
2. **Signal 2 — Stylometric heuristics (pure Python):** the text is measured for
   statistical regularity (sentence-length variance, vocabulary diversity,
   punctuation density). This captures *structural uniformity* — a property the
   LLM does not measure directly.

Each signal returns a probability-of-AI in `[0,1]`. The **confidence scorer**
combines them into a single calibrated score, decides a verdict bucket
(`likely_human` / `uncertain` / `likely_ai`), and the **label generator** turns
that bucket + score into one of three plain-language transparency labels. The
decision — text hash, both signal scores, combined score, verdict, label, and a
generated `submission_id` — is written to the **audit log** (SQLite). Finally the
API returns a structured JSON response containing the verdict, confidence, label
text, and `submission_id`.

If a creator disputes a verdict, they send their `submission_id` and reasoning to
**`POST /appeal`**. The system looks up the original decision, sets its status to
`under_review`, writes a linked **appeal entry** to the audit log, and returns a
confirmation. No automated re-classification happens — a human reviewer reads the
appeal queue (`GET /log` / `GET /appeals`) and decides.

---

## 2. Architecture Diagram

### Submission flow
```
                       ┌──────────────┐
 raw text  ─────────▶  │ Rate Limiter │  (429 if over budget)
 POST /submit          └──────┬───────┘
                              │ raw text
                              ▼
                    ┌───────────────────┐
                    │ Detection Pipeline│
                    └─────────┬─────────┘
              raw text ┌──────┴───────┐ raw text
                       ▼              ▼
            ┌────────────────┐  ┌──────────────────┐
            │ Signal 1: LLM  │  │ Signal 2: Stylo- │
            │ (Groq)         │  │ metric heuristics│
            └───────┬────────┘  └────────┬─────────┘
            p_ai_llm│ (0–1)     p_ai_stylo│ (0–1)
                    └─────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  Confidence Scorer    │  combined score (0–1)
                   │  (weighted blend +    │  + verdict bucket
                   │   agreement penalty)  │
                   └──────────┬───────────┘
                       score+ │ verdict
                              ▼
                   ┌──────────────────────┐
                   │   Label Generator    │  label text (1 of 3 variants)
                   └──────────┬───────────┘
                              ▼
                   ┌──────────────────────┐
                   │  Audit Log (SQLite)   │  persist decision + signals
                   └──────────┬───────────┘
                              ▼
                   JSON response: { submission_id, verdict,
                                    confidence, label, signals }
```

### Appeal flow
```
 submission_id + reasoning      ┌─────────────────────┐
 POST /appeal  ───────────────▶ │ Lookup original     │
                                │ decision by id      │
                                └─────────┬───────────┘
                                          │ status: under_review
                                          ▼
                                ┌─────────────────────┐
                                │ Audit Log (SQLite)  │  append appeal entry
                                │ - link to original  │  (reasoning, timestamp)
                                └─────────┬───────────┘
                                          ▼
                          JSON response: { submission_id,
                                           status: "under_review" }

 Human reviewer ──▶ GET /appeals (or GET /log) ──▶ reads queue, decides offline
```

---

## 3. Detection Signals

We use **2 distinct, genuinely independent signals** — one semantic, one structural.

### Signal 1 — LLM classifier (Groq, `llama-3.3-70b-versatile`)
- **What it measures:** holistic semantic + stylistic "feel" — whether the text
  reads as human or machine-generated, judged by a model that has internalized
  patterns of both.
- **Why it differs human vs AI:** AI prose tends to be smooth, hedged, evenly
  developed, and cliché-prone; human prose more often takes idiosyncratic risks,
  abrupt turns, and personal specificity. The LLM picks up on these globally.
- **Output:** a JSON object `{ "p_ai": 0.0–1.0, "rationale": "<one sentence>" }`.
  We force structured output via the prompt and parse it; on a parse failure we
  treat the signal as *abstaining* (see scorer).
- **Blind spot:** it can be fooled by prompts like "write this so it sounds
  human," and it is non-deterministic / can hallucinate confidence. It also has
  no ground truth — it is itself an AI guessing about AI, so it can be confidently
  wrong on short or domain-specific text.

### Signal 2 — Stylometric heuristics (pure Python)
- **What it measures:** statistical regularity of the surface text. Three
  sub-metrics, each normalized to a 0–1 "looks-AI" score, then averaged:
  - **Sentence-length variance (burstiness):** AI keeps sentences uniform; humans
    mix very short and very long. Low variance → higher AI score.
  - **Type-token ratio (vocabulary diversity):** measured on a fixed window to
    avoid length bias. Mid-range "safe" diversity skews AI; very high or very low
    skews human.
  - **Punctuation density / variety:** humans use dashes, semicolons, parentheses,
    ellipses irregularly; AI is more even. Low variety → higher AI score.
- **Why it differs human vs AI:** these are mechanical properties the LLM doesn't
  explicitly count, and they're cheap, deterministic, and explainable.
- **Output:** `{ "p_ai": 0.0–1.0, "metrics": { sentence_var, ttr, punct } }` — a
  deterministic float plus the raw sub-metrics for the audit log.
- **Blind spot:** it is genre-naive. A human-written listicle, a haiku, a legal
  clause, or a heavily-edited human draft can be statistically uniform and score
  as AI. Very short text (<40 words) gives unreliable variance — we flag this and
  down-weight the signal.

---

## 4. Uncertainty Representation

**Design-first principle:** the confidence number is for a non-technical reader,
so we decide what it should *mean* before computing it. Our reported
`confidence` is **"how sure the system is in the verdict it gives,"** not a raw
"probability of AI." So both a strong-human and strong-AI result can have high
confidence; the *uncertain* band is reserved for genuine disagreement or
near-boundary scores.

### Combining the signals → `p_ai` (probability of AI)
```
p_ai = 0.6 * p_ai_llm  +  0.4 * p_ai_stylo
```
- The LLM is weighted higher (0.6) because it's the stronger semantic signal; the
  stylometric signal (0.4) is the independent structural check.
- **Disagreement penalty:** if the two signals disagree sharply
  (`|p_ai_llm − p_ai_stylo| > 0.4`), we pull `p_ai` toward 0.5 (the uncertain
  zone) rather than trusting a confident blend — disagreement *is* uncertainty.
- **Abstention:** if Signal 1 fails to parse or Signal 2 has too little text
  (<40 words), we fall back to the available signal and cap reported confidence
  at 0.7 so we never claim high certainty on one leg.

### Verdict buckets and the reported confidence
`p_ai` is the probability of AI. We map it to a verdict and a *confidence in that
verdict*:

| `p_ai` range      | verdict        | reported confidence              |
|-------------------|----------------|----------------------------------|
| `0.00 – 0.34`     | `likely_human` | `1 − p_ai` (distance from 0.5, rescaled) |
| `0.34 – 0.66`     | `uncertain`    | low by construction (near 0.5)   |
| `0.66 – 1.00`     | `likely_ai`    | `p_ai` (rescaled)                |

So **0.6 means**: the system leans one way but is not far from the boundary — it
falls in or near the `uncertain` band and produces the hedged label, *not* a
binary verdict. This is a three-band design, **not a flip at 0.5**.

### False-positive asymmetry (key design choice)
On a writing platform, calling a human's work AI is the costlier error. So:
- The `likely_ai` threshold (0.66) is **deliberately higher** than the symmetric
  midpoint — we'd rather land in `uncertain` than wrongly accuse a human.
- Every label is phrased as *the system's assessment*, never an accusation, and
  always names the appeal path.

### How we'll test the scores are meaningful (for M4)
Feed a small fixed corpus: ~5 clearly-human texts (messy personal blog posts,
old public-domain prose), ~5 clearly-AI texts (raw model output), and ~3
borderline (lightly-edited AI, terse human). Verify clearly-AI lands >0.66,
clearly-human <0.34, and borderline clusters in 0.34–0.66 — i.e. scores *vary
meaningfully* and don't collapse to a binary.

---

## 5. Transparency Label Design (three variants — verbatim)

Plain language, confidence made meaningful, appeal path always present.

**High-confidence AI** (`verdict = likely_ai`):
> 🤖 **Likely AI-generated** — Our analysis suggests this text was probably
> created with AI assistance (confidence: {pct}%). This is an automated estimate,
> not a certainty. If you wrote this yourself, you can appeal this label.

**High-confidence human** (`verdict = likely_human`):
> ✍️ **Likely human-written** — Our analysis found no strong signs of AI
> generation in this text (confidence: {pct}%). This is an automated estimate and
> not a guarantee of authorship.

**Uncertain** (`verdict = uncertain`):
> ❓ **Inconclusive** — Our signals disagree or are too weak to call this text
> human- or AI-written with confidence (confidence: {pct}%). We're showing this
> openly rather than guessing. If a label is later applied, you can appeal it.

`{pct}` is the reported confidence × 100, rounded.

---

## 6. Appeals Workflow

- **Who can appeal:** the creator of a submission (in this backend, anyone holding
  the `submission_id`; a real platform would gate this by the authenticated
  author).
- **What they provide:** `submission_id` + free-text `reasoning` (why they believe
  the verdict is wrong; optionally evidence such as drafts/links as text).
- **What the system does on receipt:**
  1. Look up the original decision by `submission_id` (404 if unknown).
  2. Set that submission's `status` from `decided` → `under_review`.
  3. Append a linked **appeal** entry to the audit log: appeal id, the
     `submission_id` it references, the reasoning, a timestamp, and a copy of the
     original verdict/confidence for context.
  4. Return `{ submission_id, status: "under_review", appeal_id }`.
- **No automated re-classification.** Resolution is human.
- **What a reviewer sees** (`GET /appeals`): a queue of `under_review` items, each
  showing the original text reference, both signal scores, combined score,
  verdict, label shown, and the creator's reasoning — everything needed to judge
  the appeal in one view.

---

## 7. Anticipated Edge Cases (specific, not generic)

1. **Formally-constrained human poetry (haiku / repetitive verse):** deliberate
   repetition and simple vocabulary drive type-token ratio and sentence-length
   variance toward "uniform," so the stylometric signal scores it as AI even
   though it's human. *Mitigation:* very short text down-weights Signal 2 and the
   disagreement penalty should push toward `uncertain` rather than `likely_ai`.
2. **Human-written technical/legal text:** standardized phrasing, even sentence
   length, and low punctuation variety look statistically AI-like. The LLM may
   also lean AI on dry, formal prose. *Mitigation:* this is exactly why the
   `likely_ai` bar is high (0.66) and why we surface the appeal path on every AI
   label.
3. **Lightly human-edited AI text:** a creator regenerates and tweaks a few
   sentences — enough to break stylometric uniformity but the semantics stay
   AI-flavored. Signals disagree → the system should report `uncertain`, which is
   the honest answer.
4. **Very short submissions (<40 words):** variance and TTR are statistically
   meaningless; the LLM also has little to go on. *Mitigation:* abstain on
   Signal 2, cap confidence at 0.7, lean `uncertain`.

---

## 8. API Surface (the contract)

| Endpoint        | Method | Accepts                                   | Returns |
|-----------------|--------|-------------------------------------------|---------|
| `/submit`       | POST   | `{ "text": "<string>" }`                  | `{ submission_id, verdict, confidence, label, signals: { llm, stylometric }, status }` |
| `/appeal`       | POST   | `{ "submission_id": "...", "reasoning": "..." }` | `{ submission_id, status: "under_review", appeal_id }` |
| `/log`          | GET    | (optional `?limit=`)                        | array of audit-log entries (decisions + appeals) |
| `/appeals`      | GET    | —                                          | array of submissions with `status = under_review` (reviewer queue) |
| `/health`       | GET    | —                                          | `{ status: "ok" }` |

Errors: `400` on missing/empty `text` or `reasoning`; `404` on unknown
`submission_id`; `429` when the rate limit is exceeded.

**Rate limiting (values finalized in M5, reasoning here):** a real creator submits
a handful of pieces per session, not hundreds. Plan: **`10/minute` and
`100/day` per IP** on `/submit` — generous for genuine use, tight enough that an
adversary can't cheaply flood the (LLM-cost-bearing) endpoint. Read endpoints
(`/log`, `/appeals`) get a looser limit. Final numbers + rationale go in the
README.

---

## 9. AI Tool Plan (M3–M5)

For each milestone: which spec sections I hand the AI tool, what I ask it to
generate, and how I verify before moving on.

### M3 — Submission endpoint + first signal
- **Spec provided:** §3 (Detection signals — Signal 1), §2 (diagram), §8 (API
  surface).
- **Ask for:** a Flask app skeleton with `POST /submit` and `GET /health`, plus
  the `signal_llm(text) -> {p_ai, rationale}` function using Groq
  `llama-3.3-70b-versatile` with structured-output parsing and a safe fallback.
- **Verify:** call `signal_llm` directly on 3–4 inputs (one obvious AI, one
  obvious human, one short) and confirm sane `p_ai` values *before* wiring it into
  the endpoint; then hit `/submit` with curl.

### M4 — Second signal + confidence scoring
- **Spec provided:** §3 (Signal 2), §4 (Uncertainty representation), §2 (diagram).
- **Ask for:** `signal_stylometric(text) -> {p_ai, metrics}` (pure Python) and the
  `combine(p_llm, p_stylo) -> {p_ai, verdict, confidence}` scorer implementing the
  weighting, disagreement penalty, abstention, and three-band thresholds.
- **Check:** run the M4 test corpus (§4) — confirm clearly-AI >0.66, clearly-human
  <0.34, borderline in 0.34–0.66; confirm scores vary meaningfully and the
  disagreement penalty fires when signals split.

### M5 — Production layer (labels, appeals, audit log, rate limiting)
- **Spec provided:** §5 (label variants), §6 (appeals workflow), §2 (diagram), §8
  (rate-limit plan).
- **Ask for:** `generate_label(verdict, confidence) -> text` for all three
  variants, the `POST /appeal` endpoint, the SQLite audit log
  (decisions + linked appeals) with `GET /log` and `GET /appeals`, and
  Flask-Limiter config on `/submit`.
- **Verify:** craft inputs that reach all three labels; POST an appeal and confirm
  status flips to `under_review` and an appeal row links to the original; confirm
  `/log` shows ≥3 entries; hammer `/submit` past the limit and confirm `429`.

---

## 10. Checkpoint status

- ✅ Architecture narrative + diagram (both flows, labeled arrows).
- ✅ Two distinct signals chosen, each with measured property + blind spot.
- ✅ Uncertainty: three-band design, not a 0.5 flip; false-positive asymmetry baked in.
- ✅ Three label variants written verbatim.
- ✅ Appeals workflow defined (who, what, status change, logging, reviewer view).
- ✅ ≥2 specific edge cases.
- ✅ API surface defined.
- ✅ AI Tool Plan covers M3, M4, M5 with sections + asks + verification.
