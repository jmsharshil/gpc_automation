import io
import json
import logging
import re
import numpy as np
from typing import Dict, List

import PyPDF2
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 600         # words per chunk (smaller = more precise paragraph retrieval)
CHUNK_OVERLAP = 100      # word overlap between adjacent chunks

# Matches paragraph numbers like 6.54 or 8.30 at the start of a line
PARA_REF_PATTERN = re.compile(r'(?m)^[ \t]*(\d{1,3}\.\d{1,3})[ \t]')


def extract_text_from_pdf(pdf_bytes: bytes) -> Dict[int, str]:
    """
    Returns {page_number (1-indexed): text}.
    Pages with < 10 chars are stored as empty strings.
    """
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    pages_text: Dict[int, str] = {}

    for page_num, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            logger.warning("Failed to extract page %s: %s", page_num, exc)
            text = ""
        pages_text[page_num] = text

    logger.info("PDF extraction done: %s pages", len(pages_text))
    return pages_text


# ─────────────────────────────────────────────
# CHUNKING
# ─────────────────────────────────────────────

def split_into_chunks(pages_text: Dict[int, str]) -> List[Dict]:
    """
    Paragraph-aware, then word-based overlapping chunking.
    Detects paragraph markers like '8.30' or '6.54' at line starts
    and uses them as natural split boundaries.
    Returns list of: {page_number, chunk_index, text, char_count}
    """
    chunks = []
    chunk_index = 0

    for page_num in sorted(pages_text.keys()):
        text = pages_text[page_num]
        if not text.strip():
            continue

        # Split into paragraph-aware segments
        lines = text.splitlines()
        segments: List[tuple] = []  # (para_label, text_block)
        current_lines: List[str] = []
        current_label = None

        for line in lines:
            m = PARA_REF_PATTERN.match(line)
            if m:
                if current_lines:
                    seg_text = "\n".join(current_lines).strip()
                    if seg_text:
                        segments.append((current_label, seg_text))
                current_label = m.group(1)
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            seg_text = "\n".join(current_lines).strip()
            if seg_text:
                segments.append((current_label, seg_text))

        # Fall back to whole-page if no paragraph markers found
        if not segments:
            segments = [(None, text.strip())]

        # Word-based chunking within each paragraph segment
        for para_label, seg_text in segments:
            words = seg_text.split()
            if not words:
                continue

            for i in range(0, max(len(words), 1), CHUNK_SIZE - CHUNK_OVERLAP):
                chunk_words = words[i: i + CHUNK_SIZE]
                chunk_text = " ".join(chunk_words)
                if not chunk_text.strip():
                    continue
                # Prefix continuation chunks so the paragraph number stays searchable
                if para_label and i > 0:
                    chunk_text = f"[Para {para_label} cont.] {chunk_text}"
                chunks.append({
                    "page_number": page_num,
                    "chunk_index": chunk_index,
                    "text": chunk_text,
                    "char_count": len(chunk_text),
                })
                chunk_index += 1

    return chunks


# ─────────────────────────────────────────────
# EMBEDDING
# ─────────────────────────────────────────────

def get_embeddings_sync(texts: List[str]) -> List[List[float]]:
    """Call OpenAI embeddings API (sync, batched at 50)."""
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    all_embeddings: List[List[float]] = []

    batch_size = 50
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=batch,
            encoding_format="float",
        )
        all_embeddings.extend(item.embedding for item in response.data)

    return all_embeddings


# ─────────────────────────────────────────────
# FULL GUIDE PROCESSING  (called from view)
# ─────────────────────────────────────────────

def process_guide_pdf(guide) -> int:
    """
    Synchronous pipeline:
      1. Extract text (PyPDF2, text-based only)
      2. Chunk
      3. Embed (OpenAI)
      4. Save GuideChunk rows
    Returns total chunks saved.
    """
    from .models import GuideChunk  # avoid circular at module level

    # Read PDF bytes from storage
    guide.pdf_file.seek(0)
    pdf_bytes = guide.pdf_file.read()

    # Step 1 — extract
    guide.processing_status = 'processing'
    guide.save(update_fields=['processing_status'])

    pages_text = extract_text_from_pdf(pdf_bytes)
    guide.total_pages = len(pages_text)
    guide.save(update_fields=['total_pages'])

    # Step 2 — chunk
    chunks = split_into_chunks(pages_text)
    if not chunks:
        guide.processing_status = 'completed'
        guide.total_chunks = 0
        guide.save(update_fields=['processing_status', 'total_chunks'])
        return 0

    # Step 3 — embed
    texts = [c['text'] for c in chunks]
    embeddings = get_embeddings_sync(texts)

    # Step 4 — persist
    GuideChunk.objects.filter(guide=guide).delete()

    chunk_objects = [
        GuideChunk(
            guide=guide,
            page_number=chunk['page_number'],
            chunk_index=chunk['chunk_index'],
            text=chunk['text'],
            embedding=json.dumps(emb).encode('utf-8'),
            char_count=chunk['char_count'],
            embedding_model='text-embedding-3-small',
        )
        for chunk, emb in zip(chunks, embeddings)
    ]
    GuideChunk.objects.bulk_create(chunk_objects, batch_size=100)

    guide.processing_status = 'completed'
    guide.total_chunks = len(chunk_objects)
    guide.save(update_fields=['processing_status', 'total_chunks'])

    logger.info("Guide '%s' processed: %s pages, %s chunks", guide.name, len(pages_text), len(chunk_objects))
    return len(chunk_objects)


# ─────────────────────────────────────────────
# SEMANTIC SEARCH  across multiple guides
# ─────────────────────────────────────────────

def get_relevant_chunks_for_guides(
    guide_ids, query_embedding, max_chunks=10, query_text=''
):
    from .models import GuideChunk
    from django.db.models import Q

    # ── Step 1: Keyword search — paragraph refs AND important noun phrases ──
    keyword_chunks = []
    keyword_ids = set()

    if query_text:
        q_filter = Q()

        # Existing: paragraph number refs like 8.30
        para_refs = re.findall(r'\b(\d{1,3}\.\d{1,3})\b', query_text)
        for ref in para_refs:
            q_filter |= Q(text__icontains=ref)

        # NEW: extract meaningful multi-word phrases (3+ chars, skip stopwords)
        stopwords = {'what','is','the','a','an','of','in','per','between','and',
                     'or','for','are','how','does','why','when','difference'}
        words = [w.lower().strip('?.,') for w in query_text.split()
                 if len(w) > 3 and w.lower() not in stopwords]

        # Boost: if 2+ significant keywords, search for chunks containing them
        if len(words) >= 2:
            keyword_q = Q()
            for w in words[:5]:  # cap at 5 to avoid over-filtering
                keyword_q &= Q(text__icontains=w)
            q_filter |= keyword_q

        if q_filter:
            kw_qs = list(
                GuideChunk.objects.filter(guide_id__in=guide_ids)
                .filter(q_filter)
                .select_related('guide')
                .only('id', 'guide_id', 'guide__name', 'page_number',
                      'chunk_index', 'text', 'embedding')
            )
            for kc in kw_qs:
                keyword_ids.add(kc.id)
                keyword_chunks.append(kc)

    # ── Step 2: Vector similarity search ─────────────────────────────────────
    all_chunks = list(
        GuideChunk.objects.filter(guide_id__in=guide_ids)
        .select_related('guide')
        .only('id', 'guide_id', 'guide__name', 'page_number', 'chunk_index', 'text', 'embedding')
    )

    vector_top_chunks: List = []
    if all_chunks:
        matrix = []
        valid_chunks = []
        for chunk in all_chunks:
            try:
                vec = json.loads(bytes(chunk.embedding).decode('utf-8'))
                matrix.append(vec)
                valid_chunks.append(chunk)
            except Exception:
                continue

        if matrix:
            matrix_np = np.array(matrix, dtype=np.float32)
            query_np = np.array(query_embedding, dtype=np.float32)
            dot_products = matrix_np @ query_np
            norms = np.linalg.norm(matrix_np, axis=1) * np.linalg.norm(query_np)
            similarities = np.where(norms > 0, dot_products / norms, 0.0)
            k = min(max_chunks, len(valid_chunks))
            top_indices = np.argpartition(similarities, -k)[-k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]
            vector_top_chunks = [valid_chunks[i] for i in top_indices]

    # ── Step 3: Merge — keyword chunks first, then vector results (deduped) ──
    seen_ids: set = set()
    merged: List = []
    for chunk in keyword_chunks:
        if chunk.id not in seen_ids:
            seen_ids.add(chunk.id)
            merged.append(chunk)
    for chunk in vector_top_chunks:
        if chunk.id not in seen_ids:
            seen_ids.add(chunk.id)
            merged.append(chunk)

    # Always return at least max_chunks or all keyword hits (whichever is more)
    limit = max(max_chunks, len(keyword_chunks))
    return merged[:limit]


