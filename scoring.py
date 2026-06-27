"""Confidence scoring for Provenance Guard (planning.md §4).

Combines the two independent signals into a single probability-of-AI, then maps
that to a three-band verdict and a calibrated "confidence in the verdict".
"""

# Signal weights: the LLM is the stronger semantic signal; stylometry is the
# independent structural check.
W_LLM = 0.6
W_STYLO = 0.4

# Three-band thresholds. The likely_ai bar (0.66) is deliberately higher than a
# symmetric midpoint: on a writing platform, wrongly accusing a human is the
# costlier error, so we'd rather land in "uncertain".
T_HUMAN = 0.34
T_AI = 0.66

# When the two signals disagree sharply, disagreement *is* uncertainty: pull the
# blended score halfway back toward 0.5.
DISAGREE_GAP = 0.40
DISAGREE_PULL = 0.5

# When only one signal is usable we never claim high certainty.
ABSTAIN_CONF_CAP = 0.70


def _verdict(p_ai):
    if p_ai >= T_AI:
        return "likely_ai"
    if p_ai < T_HUMAN:
        return "likely_human"
    return "uncertain"


def combine(llm, stylo):
    """Combine two signal dicts ({'p_ai', 'abstained', ...}) into a verdict.

    Returns: {p_ai, verdict, confidence, disagreement, one_signal_only}.
    """
    p_llm, p_stylo = llm["p_ai"], stylo["p_ai"]
    llm_out = llm.get("abstained", False)
    stylo_out = stylo.get("abstained", False)

    one_signal_only = llm_out or stylo_out
    if llm_out and not stylo_out:
        p_ai = p_stylo
    elif stylo_out and not llm_out:
        p_ai = p_llm
    else:
        # Both usable (or both abstained -> still a blend, but capped below).
        p_ai = W_LLM * p_llm + W_STYLO * p_stylo

    disagreement = abs(p_llm - p_stylo)
    if not one_signal_only and disagreement > DISAGREE_GAP:
        # Shrink distance from 0.5 — honest hedge toward "uncertain".
        p_ai = 0.5 + (p_ai - 0.5) * DISAGREE_PULL

    p_ai = max(0.0, min(1.0, p_ai))
    verdict = _verdict(p_ai)

    # Confidence = how sure we are in the verdict = distance from the 0.5 fence,
    # rescaled to [0,1]. High for clear human OR clear AI; near 0 in the
    # uncertain band by construction.
    confidence = abs(p_ai - 0.5) * 2.0
    if one_signal_only:
        confidence = min(confidence, ABSTAIN_CONF_CAP)

    return {
        "p_ai": round(p_ai, 3),
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "disagreement": round(disagreement, 3),
        "one_signal_only": one_signal_only,
    }
