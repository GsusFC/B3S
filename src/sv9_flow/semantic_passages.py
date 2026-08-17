SEMANTIC_PASSAGE_POLICY_VERSION = "sv9-semantic-passages-v1"
SEMANTIC_PASSAGE_CHARS = 48_000
SEMANTIC_PASSAGE_OVERLAP_CHARS = 319
SEMANTIC_CONTENT_MAX_BYTES = 2_097_152
MAX_SEMANTIC_PASSAGES_PER_RECORD = 44

def semantic_passages(content: str) -> list[str]:
    text = str(content or "")
    if len(text.encode("utf-8")) > SEMANTIC_CONTENT_MAX_BYTES:
        raise ValueError("semantic_content_exceeds_verified_raw_limit")
    stride = SEMANTIC_PASSAGE_CHARS - SEMANTIC_PASSAGE_OVERLAP_CHARS
    passages = [text[start : start + SEMANTIC_PASSAGE_CHARS] for start in (range(0, max(1, len(text) - SEMANTIC_PASSAGE_OVERLAP_CHARS), stride) if text else ())]
    if len(passages) > MAX_SEMANTIC_PASSAGES_PER_RECORD:
        raise RuntimeError("semantic_passage_call_ceiling_exceeded")
    return passages
