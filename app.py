"""Provenance Guard API.

M3: POST /submit runs Signal 1 (LLM), writes a structured audit-log entry, and
returns a content_id plus a provisional attribution. Confidence calibration
(M4) and the real transparency label + appeals (M5) are placeholders for now.
"""

import uuid

from flask import Flask, jsonify, request

import audit
from scoring import combine
from signals import signal_llm, signal_stylometric

app = Flask(__name__)
audit.init_db()


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

    # Multi-signal detection pipeline.
    llm = signal_llm(text)                 # Signal 1 — semantic (LLM)
    stylo = signal_stylometric(text)       # Signal 2 — structural (stylometry)
    score = combine(llm, stylo)            # calibrated confidence scoring (M4)

    attribution = score["verdict"]
    confidence = score["confidence"]
    label = "PLACEHOLDER — calibrated label arrives in M5"

    audit.log_decision(
        content_id=content_id,
        creator_id=creator_id,
        attribution=attribution,
        confidence=confidence,
        llm_score=llm["p_ai"],
        stylometric_score=stylo["p_ai"],
        combined_score=score["p_ai"],
        status="classified",
        label=label,
        details={
            "llm_rationale": llm["rationale"],
            "llm_abstained": llm["abstained"],
            "stylometric_metrics": stylo["metrics"],
            "stylometric_abstained": stylo["abstained"],
            "disagreement": score["disagreement"],
            "one_signal_only": score["one_signal_only"],
        },
    )

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "p_ai": score["p_ai"],
        "label": label,
        "signals": {"llm": llm, "stylometric": stylo},
        "status": "classified",
    })


@app.get("/log")
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit.get_log(limit)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
