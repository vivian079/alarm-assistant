"""Find the SOPs for an alarm.

find_sops     Exact match: the SOP's alarm_codes contain the alarm code and its process_steps contain
              the process step. This is the same check as rule S1 (design doc): no match means no SOP
              covers this alarm code at this process step.
suggest_sops  Only used when find_sops finds nothing. Ranks SOPs by how close their text is to the
              alarm code's description (embedding + pgvector). The result is shown to the engineer as
              "may be related, not confirmed" and never goes into a prompt: a similar SOP is not
              necessarily the right one, and Claude would write steps from it.
"""

import hashlib
import json
import os

from fastembed import TextEmbedding

import db

SOP_FILE = os.environ.get("SOP_FILE", "/data/sops.json")

# Runs inside the API container (no API key, the SOP text does not leave the machine).
# 384 numbers per vector; sop_embeddings.embedding is vector(384) to match.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# The Dockerfile downloads the model here when the image is built, so startup does not download it.
MODEL_CACHE = os.environ.get("EMBEDDING_CACHE", "/models")
SUGGESTION_COUNT = 3

with open(SOP_FILE) as f:
    SOPS: list[dict] = json.load(f)
SOPS_BY_ID = {s["sop_id"]: s for s in SOPS}

_model: TextEmbedding | None = None


def embed(texts: list[str]) -> list[list[float]]:
    """Text -> vector, one per text. The model is loaded the first time it is needed."""
    global _model
    if _model is None:
        _model = TextEmbedding(EMBEDDING_MODEL, cache_dir=MODEL_CACHE)
    return [[float(x) for x in v] for v in _model.embed(texts)]


def find_sops(alarm_code: str, equipment_type: str, process_step: str) -> list[dict]:
    """Exact match. [] = rule S1 (no SOP covers this alarm code at this process step)."""
    return [
        s for s in SOPS
        if s["equipment_type"] == equipment_type
        and alarm_code in s["alarm_codes"]
        and process_step in s["process_steps"]
    ]


def format_sop(sop: dict) -> str:
    """One SOP as prompt text: a header line, then one line per step."""
    lines = [
        f"{sop['sop_id']} | {sop['title']} | equipment: {sop['equipment_type']} | "
        f"alarm codes: {', '.join(sop['alarm_codes'])} | process steps: {', '.join(sop['process_steps'])}"
    ]
    lines += [f"  step {s['step']}: {s['text']}" for s in sop["steps"]]
    return "\n".join(lines)


def format_sops(sops: list[dict]) -> str:
    return "\n\n".join(format_sop(s) for s in sops)


def embedding_text(sop: dict) -> str:
    """What gets embedded for one SOP: title + all steps, as one vector.
    Not one vector per step: a step alone ("If all of the above are normal, stop the tool...")
    does not say what problem it is for. The tags (alarm_codes, process_steps) are left out on purpose:
    this search is for when the tags are missing or wrong.
    """
    return sop["title"] + "\n" + "\n".join(s["text"] for s in sop["steps"])


def index_sops() -> int:
    """Runs on API startup. Every SOP in sops.json gets an up-to-date row in sop_embeddings.
    Only new or changed SOPs are embedded (compared by a hash of the text); SOPs no longer in the
    file are deleted. Returns how many were embedded.
    """
    stored = db.get_sop_embedding_hashes(EMBEDDING_MODEL)
    todo = []
    for sop in SOPS:
        text = embedding_text(sop)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        if stored.get(sop["sop_id"]) != content_hash:
            todo.append((sop, content_hash, text))
    if todo:
        vectors = embed([text for _, _, text in todo])
        db.upsert_sop_embeddings([
            (sop["sop_id"], sop["equipment_type"], EMBEDDING_MODEL, content_hash, vector)
            for (sop, content_hash, _), vector in zip(todo, vectors)
        ])
    db.delete_sop_embeddings_except([s["sop_id"] for s in SOPS])
    return len(todo)


def suggest_sops(description: str, equipment_type: str, k: int = SUGGESTION_COUNT) -> list[dict]:
    """The k SOPs of this equipment type whose text is closest to the alarm code's description.
    Filtered by equipment type only: process_steps is a tag too, and the tags are what may be missing.
    Returns [{"sop_id", "title", "similarity"}], most similar first. similarity = 1 - cosine distance;
    it only ranks the SOPs, it does not say whether one is right.
    """
    [query] = embed([description])
    rows = db.nearest_sop_embeddings(query, equipment_type, EMBEDDING_MODEL, k)
    return [
        {"sop_id": r["sop_id"], "title": SOPS_BY_ID[r["sop_id"]]["title"], "similarity": round(1 - r["distance"], 3)}
        for r in rows
    ]
