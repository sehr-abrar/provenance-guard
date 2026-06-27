"""Provenance Guard API.

M3: POST /submit runs Signal 1 (LLM), writes a structured audit-log entry, and
returns a content_id plus a provisional attribution. Confidence calibration
(M4) and the real transparency label + appeals (M5) are placeholders for now.
"""

import uuid

from flask import Flask, jsonify, request

import audit
from signals import signal_llm

app = Flask(__name__)
audit.init_db()


def _provisional_attribution(p_ai):
    """Three-band verdict from a single signal (placeholder until M4 scoring)."""
    if p_ai >= 0.66:
        return "likely_ai"
    if p_ai <= 0.34:
        return "likely_human"
    return "uncertain"


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/submit")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = data.get("creator_id")

    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400
    if not creator_id:
        return jsonify({"error": "field 'creator_id' is required"}), 400

    content_id = str(uuid.uuid4())

    # Signal 1 — LLM classifier.
    llm = signal_llm(text)
    llm_score = llm["p_ai"]

    # Provisional verdict + PLACEHOLDER confidence/label (real logic in M4/M5).
    attribution = _provisional_attribution(llm_score)
    confidence = llm_score  # placeholder: not yet calibrated
    label = "PLACEHOLDER — calibrated label arrives in M5"

    audit.log_decision(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=round(confidence, 3),
        llm_score=llm_score,
        status="classified",
        label=label,
        details={"llm_rationale": llm["rationale"], "llm_abstained": llm["abstained"]},
    )

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": round(confidence, 3),
        "label": label,
        "signals": {"llm": llm},
        "status": "classified",
    })


@app.get("/log")
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit.get_log(limit)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
