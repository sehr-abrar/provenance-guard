"""Provenance Guard API.

M3: POST /submit runs Signal 1 (LLM), writes a structured audit-log entry, and
returns a content_id plus a provisional attribution. Confidence calibration
(M4) and the real transparency label + appeals (M5) are placeholders for now.
"""

import uuid

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit
from labels import generate_label
from scoring import combine
from signals import signal_llm, signal_stylometric

app = Flask(__name__)
audit.init_db()

# Rate limiting (see README for chosen limits + reasoning). In-memory storage is
# fine for a single-process dev/grading setup.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
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
    label = generate_label(attribution, confidence)

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


@app.post("/appeal")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "field 'content_id' is required"}), 400
    if not reasoning:
        return jsonify({"error": "field 'creator_reasoning' is required"}), 400

    original = audit.log_appeal(content_id, reasoning)
    if original is None:
        return jsonify({"error": f"no submission found for content_id {content_id}"}), 404

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Appeal received. This content is now under human review.",
        "original_decision": {
            "attribution": original["attribution"],
            "confidence": original["confidence"],
        },
    })


@app.get("/log")
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit.get_log(limit)})


@app.get("/appeals")
def appeals():
    """Reviewer queue: submissions currently under review."""
    return jsonify({"appeals": audit.get_appeals()})


if __name__ == "__main__":
    # use_reloader=False keeps a single process so the in-memory rate limiter
    # counts correctly (the reloader's extra process splits the count).
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
