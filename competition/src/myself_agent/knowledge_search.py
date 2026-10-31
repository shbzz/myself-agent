"""Deterministic bounded lexical retrieval, with complete immutable citations."""

import hashlib
import json
import re
import unicodedata

ALGORITHM = "lexical-v1"
MAX_HITS = 6
MAX_CONTEXT_CHARS = 12000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def chunks(text):
    return [
        {
            "id": f"c{start // 1200 + 1:04d}",
            "start": start,
            "end": min(start + 1200, len(text)),
            "text": text[start : start + 1200],
            "sha256": text_digest(text[start : start + 1200]),
        }
        for start in range(0, len(text), 1200)
    ]


def terms(text):
    words = re.findall(r"[\u3400-\u9fff]+|[^\W_]+", unicodedata.normalize("NFKC", text).casefold())
    result = set()
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1:
            result.update(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.add(word)
    return result


def retrieve(snapshot, agent_id, query):
    """Only the selected Agent's frozen grants are searched; empty grants mean deny."""
    query_truncated = len(query) > 2000
    query = query[:2000]
    needles = set(sorted(terms(query))[:128])
    grant = snapshot.get("grants", {}).get(agent_id, {}) if snapshot else {}
    candidates = []
    for source in snapshot.get("sources", []) if snapshot else []:
        if source["id"] not in grant.get("knowledge_ids", []):
            continue
        for chunk in source["chunks"]:
            score = len(needles & terms(chunk["text"])) + 2 * len(needles & terms(source["title"]))
            if score:
                candidates.append(
                    {
                        "kind": "source",
                        "citation": f"K:{source['id']}@{source['version']}#{chunk['id']}",
                        "source_id": source["id"],
                        "version": source["version"],
                        "chunk_id": chunk["id"],
                        "title": source["title"],
                        "reference": source["reference"],
                        "start": chunk["start"],
                        "end": chunk["end"],
                        "sha256": chunk["sha256"],
                        "source_sha256": source["sha256"],
                        "text": chunk["text"],
                        "score": score,
                    }
                )
    for memory in snapshot.get("memories", []) if snapshot else []:
        if memory["id"] not in grant.get("memory_ids", []) or memory["status"] != "approved":
            continue
        score = len(needles & terms(memory["statement"]))
        if score:
            candidates.append(
                {
                    "kind": "experience",
                    "citation": f"M:{memory['id']}@{memory['version']}",
                    "memory_id": memory["id"],
                    "version": memory["version"],
                    "text": memory["statement"],
                    "sha256": text_digest(memory["statement"]),
                    "evidence": memory["evidence"],
                    "score": score,
                    "qualification": (
                        "human-reviewed experience linked to a verified run, not a guarantee"
                    ),
                }
            )
    candidates.sort(key=lambda value: (-value["score"], value["citation"]))
    result = {
        "algorithm": ALGORITHM,
        "snapshot_digest": snapshot.get("digest") if snapshot else None,
        "query": query,
        "query_truncated": query_truncated,
        "hits": [],
        "truncated": False,
        "max_hits": MAX_HITS,
        "max_context_chars": MAX_CONTEXT_CHARS,
    }
    for hit in candidates:
        if len(result["hits"]) >= MAX_HITS:
            break
        if (
            len(json.dumps(result | {"hits": [*result["hits"], hit]}, ensure_ascii=False))
            > MAX_CONTEXT_CHARS - 32
        ):
            continue
        result["hits"].append(hit)
    result["truncated"] = len(result["hits"]) < len(candidates)
    return result
