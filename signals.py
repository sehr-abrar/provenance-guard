"""Detection signals for Provenance Guard.

Signal 1 (this milestone): an LLM classifier via Groq that judges, holistically,
how AI-generated a piece of text reads. Signal 2 (stylometric heuristics) lands
in M4.
"""

import json
import os
import re
import statistics

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL = "llama-3.3-70b-versatile"

_SYSTEM = (
    "You are an expert linguistic analyst for a creative-writing platform. "
    "Your job is to estimate the probability that a piece of text was generated "
    "by an AI language model rather than written by a human. Judge holistically: "
    "consider stylistic coherence, hedging, generic phrasing, predictability, and "
    "idiosyncrasy. You are giving a probabilistic estimate, not a certain verdict."
)

_USER_TEMPLATE = (
    "Assess the following text. Respond with ONLY a JSON object of the form "
    '{{"p_ai": <float 0.0-1.0>, "rationale": "<one short sentence>"}} where p_ai '
    "is the probability the text is AI-generated (1.0 = certainly AI, 0.0 = "
    "certainly human).\n\nTEXT:\n\"\"\"\n{text}\n\"\"\""
)

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set (check your .env file)")
        _client = Groq(api_key=api_key)
    return _client


def _extract_p_ai(raw):
    """Best-effort parse of p_ai from a model response string."""
    try:
        obj = json.loads(raw)
        return float(obj["p_ai"]), obj.get("rationale", "")
    except (ValueError, KeyError, TypeError):
        pass
    # Fallback: pull the first float in [0,1] out of the text.
    m = re.search(r'"?p_ai"?\s*[:=]\s*(0?\.\d+|1\.0|0|1)', raw)
    if m:
        return float(m.group(1)), "parsed from unstructured response"
    m = re.search(r"\b(0?\.\d+|1\.0)\b", raw)
    if m:
        return float(m.group(1)), "parsed from unstructured response"
    raise ValueError(f"could not parse p_ai from: {raw!r}")


def signal_llm(text):
    """Return Signal 1's assessment.

    Output: {"p_ai": float in [0,1], "rationale": str, "abstained": bool}
    On any API/parse failure the signal abstains (p_ai=0.5) so the pipeline can
    fall back rather than crash.
    """
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _USER_TEMPLATE.format(text=text)},
            ],
        )
        raw = resp.choices[0].message.content
        p_ai, rationale = _extract_p_ai(raw)
        p_ai = max(0.0, min(1.0, p_ai))
        return {"p_ai": round(p_ai, 3), "rationale": rationale, "abstained": False}
    except Exception as e:  # noqa: BLE001 - signal must degrade gracefully
        return {
            "p_ai": 0.5,
            "rationale": f"signal abstained: {e}",
            "abstained": True,
        }


# ---------------------------------------------------------------------------
# Signal 2 — Stylometric heuristics (pure Python, deterministic).
# Measures structural REGULARITY. Each sub-metric maps to a "looks-AI" score in
# [0,1] (1 = looks AI); the signal score is their mean. AI prose is uniform;
# human prose is bursty and irregular.
# ---------------------------------------------------------------------------

MIN_WORDS_FOR_STYLO = 40  # below this, variance/TTR are statistically unreliable

_WORD_RE = re.compile(r"[A-Za-z']+")
_RICH_PUNCT = set(";:—–-()\"'!?…")


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _sentences(text):
    parts = re.split(r"[.!?]+", text)
    return [p.strip() for p in parts if p.strip()]


def signal_stylometric(text):
    """Return Signal 2's assessment.

    Output: {"p_ai": float in [0,1], "metrics": {...}, "abstained": bool}
    Abstains (down-weights) on very short text where the statistics are noise.
    """
    words = _WORD_RE.findall(text)
    n_words = len(words)
    abstained = n_words < MIN_WORDS_FOR_STYLO

    # --- Metric A: burstiness (sentence-length variance) -------------------
    # Human writing mixes short and long sentences; AI keeps them uniform.
    # Low variance -> looks AI.
    sents = _sentences(text)
    sent_lens = [len(_WORD_RE.findall(s)) for s in sents]
    if len(sent_lens) >= 2:
        stdev = statistics.pstdev(sent_lens)
        ai_burst = _clamp(1.0 - stdev / 8.0)
    else:
        stdev = 0.0
        ai_burst = 0.5  # can't tell from one sentence

    # --- Metric B: lexical diversity (type-token ratio) --------------------
    # Computed on a capped 100-word window to remove length bias. AI tends to a
    # moderate, "safe" diversity band; humans deviate (terse-distinctive = high,
    # repetitive-casual = low). Closeness to the AI band -> looks AI.
    window = [w.lower() for w in words[:100]]
    ttr = len(set(window)) / len(window) if window else 0.0
    ai_ttr = _clamp(1.0 - abs(ttr - 0.72) / 0.28)

    # --- Metric C: punctuation richness ------------------------------------
    # Expressive human writing reaches for varied punctuation (—, ;, ?, ...);
    # AI/formal prose leans on commas and periods. Low variety -> looks AI.
    distinct_rich = len({c for c in text if c in _RICH_PUNCT})
    ai_punct = _clamp(1.0 - distinct_rich / 4.0)

    p_ai = round(statistics.mean([ai_burst, ai_ttr, ai_punct]), 3)
    return {
        "p_ai": p_ai,
        "abstained": abstained,
        "metrics": {
            "n_words": n_words,
            "sentence_len_stdev": round(stdev, 2),
            "type_token_ratio": round(ttr, 3),
            "distinct_punct": distinct_rich,
            "ai_burst": round(ai_burst, 3),
            "ai_ttr": round(ai_ttr, 3),
            "ai_punct": round(ai_punct, 3),
        },
    }


if __name__ == "__main__":
    # Independent verification before wiring into the endpoint (M3 step).
    samples = {
        "obvious_human": (
            "ok so i burnt the toast again. third time this week. my kitchen smoke "
            "alarm basically lives for these moments, screaming like it's auditioning."
        ),
        "obvious_ai": (
            "In today's fast-paced world, effective time management is essential for "
            "success. By prioritizing tasks and setting clear goals, individuals can "
            "significantly enhance their productivity and overall well-being."
        ),
        "very_short": "Nice day.",
    }
    for name, txt in samples.items():
        print(f"\n[{name}]")
        print(signal_llm(txt))
