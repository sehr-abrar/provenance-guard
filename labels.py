"""Transparency label generation (planning.md §5).

Maps a verdict + calibrated confidence to the exact end-user label text. Three
variants; the wording is fixed and the confidence percentage is interpolated.
Phrased as the system's *assessment*, never an accusation, and the appeal path
is always present on a verdict that could harm a creator.
"""

_LIKELY_AI = (
    "🤖 Likely AI-generated — Our analysis suggests this text was probably "
    "created with AI assistance (confidence: {pct}%). This is an automated "
    "estimate, not a certainty. If you wrote this yourself, you can appeal this "
    "label."
)

_LIKELY_HUMAN = (
    "✍️ Likely human-written — Our analysis found no strong signs of AI "
    "generation in this text (confidence: {pct}%). This is an automated estimate "
    "and not a guarantee of authorship."
)

_UNCERTAIN = (
    "❓ Inconclusive — Our signals disagree or are too weak to call this text "
    "human- or AI-written with confidence (confidence: {pct}%). We're showing "
    "this openly rather than guessing. If a label is later applied, you can "
    "appeal it."
)

_TEMPLATES = {
    "likely_ai": _LIKELY_AI,
    "likely_human": _LIKELY_HUMAN,
    "uncertain": _UNCERTAIN,
}


def generate_label(verdict, confidence):
    """Return the transparency-label text for a verdict + confidence (0–1)."""
    template = _TEMPLATES.get(verdict, _UNCERTAIN)
    return template.format(pct=round(confidence * 100))


if __name__ == "__main__":
    # Confirm all three variants render (M5 verification step).
    for v, c in [("likely_ai", 0.42), ("likely_human", 0.60), ("uncertain", 0.20)]:
        print(f"\n[{v} @ {c}]")
        print(generate_label(v, c))
