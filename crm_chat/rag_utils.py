# """
# RAG Utilities for Large PDF Processing
# Handles:
# - PDF text extraction
# - Intelligent chunking
# - Vector embeddings
# - Semantic search
# """

# import io
# import logging
# import numpy as np
# from typing import List, Tuple, Dict, Optional
# import PyPDF2
# import json
# import hashlib

# logger = logging.getLogger(__name__)

# # Constants
# CHUNK_SIZE = 1000  # tokens per chunk (approximate)
# CHUNK_OVERLAP = 200  # overlap between chunks
# EMBEDDING_DIMENSION = 1536  # for OpenAI text-embedding-3-small


# def extract_text_from_pdf(pdf_bytes: bytes) -> Dict[int, str]:
#     """
#     Extract text from PDF bytes.
#     Returns: {page_number: text}
#     """
#     try:
#         reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
#         pages_text = {}
        
#         for page_num, page in enumerate(reader.pages, 1):
#             try:
#                 text = page.extract_text() or ""
#                 pages_text[page_num] = text
#             except Exception as e:
#                 logger.warning(f"Failed to extract page {page_num}: {e}")
#                 pages_text[page_num] = ""
        
#         return pages_text
#     except Exception as e:
#         logger.error(f"PDF extraction failed: {e}")
#         raise


# def split_into_chunks(pages_text: Dict[int, str]) -> List[Dict]:
#     """
#     Split extracted text into overlapping chunks.
    
#     Returns: [
#         {
#             'page_number': int,
#             'chunk_index': int,
#             'text': str,
#             'char_count': int,
#         },
#         ...
#     ]
#     """
#     chunks = []
#     chunk_index = 0
#     logger.info(
#         "Starting split_into_chunks pages=%s chunk_size=%s overlap=%s",
#         len(pages_text),
#         CHUNK_SIZE,
#         CHUNK_OVERLAP,
#     )
    
#     for page_num in sorted(pages_text.keys()):
#         text = pages_text[page_num]
        
#         # Simple token approximation: 1 token ≈ 4 chars
#         words = text.split()
#         page_chunk_start = len(chunks)
#         logger.debug(
#             "Chunking page=%s word_count=%s char_count=%s",
#             page_num,
#             len(words),
#             len(text or ""),
#         )
        
#         # Create chunks with overlap
#         for i in range(0, len(words), CHUNK_SIZE - CHUNK_OVERLAP):
#             chunk_words = words[i:i + CHUNK_SIZE]
#             chunk_text = " ".join(chunk_words)
            
#             if chunk_text.strip():  # Skip empty chunks
#                 chunks.append({
#                     'page_number': page_num,
#                     'chunk_index': chunk_index,
#                     'text': chunk_text,
#                     'char_count': len(chunk_text),
#                 })
#                 logger.debug(
#                     "Created chunk page=%s chunk_index=%s char_count=%s word_range=%s-%s",
#                     page_num,
#                     chunk_index,
#                     len(chunk_text),
#                     i,
#                     i + len(chunk_words),
#                 )
#                 chunk_index += 1

#         logger.info(
#             "Completed chunking page=%s chunks_created=%s running_total=%s",
#             page_num,
#             len(chunks) - page_chunk_start,
#             len(chunks),
#         )
    
#     logger.info("Finished split_into_chunks total_chunks=%s", len(chunks))
#     return chunks


# async def get_embeddings(texts: List[str], client) -> List[List[float]]:
#     """
#     Get embeddings from OpenAI API.
    
#     Args:
#         texts: List of texts to embed
#         client: OpenAI client
    
#     Returns: List of embedding vectors
#     """
#     try:
#         response = await client.embeddings.create(
#             model="text-embedding-3-small",
#             input=texts,
#             encoding_format="float"
#         )
        
#         embeddings = [item.embedding for item in response.data]
#         return embeddings
#     except Exception as e:
#         logger.error(f"Embedding request failed: {e}")
#         raise


# def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
#     """Calculate cosine similarity between two vectors."""
#     arr1 = np.array(vec1)
#     arr2 = np.array(vec2)
    
#     dot_product = np.dot(arr1, arr2)
#     norm1 = np.linalg.norm(arr1)
#     norm2 = np.linalg.norm(arr2)
    
#     if norm1 == 0 or norm2 == 0:
#         return 0.0
    
#     return float(dot_product / (norm1 * norm2))


# def semantic_search(
#     query_embedding: List[float],
#     chunk_embeddings: List[Tuple[int, List[float]]],  # (chunk_id, embedding)
#     top_k: int = 10,
#     min_similarity: float = 0.3
# ) -> List[int]:
#     """
#     Returns: List of chunk indices (highest similarity first)
#     """
#     similarities = []
    
#     for chunk_id, chunk_emb in chunk_embeddings:
#         sim = cosine_similarity(query_embedding, chunk_emb)
#         if sim >= min_similarity:
#             similarities.append((chunk_id, sim))
    
#     # Sort by similarity (descending)
#     similarities.sort(key=lambda x: x[1], reverse=True)
    
#     # Return top-k chunk indices
#     return [chunk_id for chunk_id, _ in similarities[:top_k]]


# def build_context_from_chunks(
#     chunks: List[Dict],
#     chunk_ids: List[int],
#     max_context_chars: int = 10000
# ) -> str:
#     """
#     Build context string from relevant chunks.
    
#     Args:
#         chunks: All chunks
#         chunk_ids: Indices of relevant chunks
#         max_context_chars: Maximum total characters to include
    
#     Returns: Formatted context string
#     """
#     context_parts = []
#     total_chars = 0
    
#     for chunk_id in chunk_ids:
#         if chunk_id >= len(chunks):
#             continue
        
#         chunk = chunks[chunk_id]
#         page_ref = f"[Page {chunk['page_number']}, Chunk {chunk['chunk_index']}]"
#         chunk_text = f"{page_ref}\n{chunk['text']}"
        
#         if total_chars + len(chunk_text) > max_context_chars:
#             break
        
#         context_parts.append(chunk_text)
#         total_chars += len(chunk_text)
    
#     separator = "\n\n" + "="*50 + "\n\n"
#     return separator.join(context_parts)


# def truncate_text(text: str, max_chars: int = 100000) -> str:
#     """Truncate text to max length."""
#     if len(text) > max_chars:
#         return text[:max_chars-3] + "..."
#     return text


# class RAGPipeline:
#     """End-to-end RAG pipeline for document Q&A"""
    
#     def __init__(self, openai_client=None):
#         self.client = openai_client
#         self.chunks = []
#         self.chunk_embeddings = []
    
#     async def process_pdf(self, pdf_bytes: bytes) -> int:
#         """
#         Process PDF and store embeddings.
#         Returns: Number of chunks created
#         """
#         # Extract text
#         pages_text = extract_text_from_pdf(pdf_bytes)
#         logger.info(f"Extracted {len(pages_text)} pages")
        
#         # Create chunks
#         self.chunks = split_into_chunks(pages_text)
#         logger.info(f"Created {len(self.chunks)} chunks")
        
#         # Get embeddings (batch by 100 to avoid rate limits)
#         texts_to_embed = [chunk['text'] for chunk in self.chunks]
#         all_embeddings = []
        
#         batch_size = 100
#         for i in range(0, len(texts_to_embed), batch_size):
#             batch = texts_to_embed[i:i+batch_size]
#             batch_embeddings = await get_embeddings(batch, self.client)
#             all_embeddings.extend(batch_embeddings)
#             logger.info(f"Embedded {min(i+batch_size, len(texts_to_embed))}/{len(texts_to_embed)} chunks")
        
#         self.chunk_embeddings = all_embeddings
#         return len(self.chunks)
    
#     async def answer_question(
#         self,
#         question: str,
#         max_context_chunks: int = 10,
#         max_context_chars: int = 10000
#     ) -> Tuple[str, List[int], str]:
#         """
#         Answer a question using the document.
        
#         Returns: (answer, relevant_chunk_ids, context_used)
#         """
#         if not self.chunks:
#             raise ValueError("No document loaded. Process a PDF first.")
        
#         # Get question embedding
#         question_embedding = await get_embeddings([question], self.client)
#         query_embedding = question_embedding[0]
        
#         # Find relevant chunks
#         chunk_embeddings_with_ids = [
#             (i, emb) for i, emb in enumerate(self.chunk_embeddings)
#         ]
#         relevant_chunk_ids = semantic_search(
#             query_embedding,
#             chunk_embeddings_with_ids,
#             top_k=max_context_chunks
#         )
        
#         # Build context
#         context = build_context_from_chunks(
#             self.chunks,
#             relevant_chunk_ids,
#             max_context_chars
#         )
        
#         # Build prompt
#         system_prompt = """You are a helpful assistant that answers questions based ONLY on the provided document context.

# IMPORTANT RULES:
# 1. Only use information from the provided context
# 2. If the answer is not in the context, say "The document does not contain information about this topic"
# 3. Cite the page numbers where you found information
# 4. Be concise and accurate"""
        
#         user_prompt = f"""Document Context:
# {context}

# Question: {question}

# Answer based ONLY on the information above. Do not use external knowledge."""
        
#         return system_prompt, user_prompt, relevant_chunk_ids, context

import io
import json
import base64
import logging
import asyncio
import numpy as np
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor
 
import PyPDF2
import fitz
from PIL import Image
from openai import OpenAI
from django.conf import settings
from io import BytesIO
from docx import Document
from pptx import Presentation
import pandas as pd
 
logger = logging.getLogger(__name__)
 
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200
MAX_CONCURRENT_OCR = 25
 
 
# =====================================================
# GPT-VISION OCR  (only called for image-based pages)
# =====================================================
 
def vision_ocr_image(image_bytes: bytes) -> str:
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are an OCR engine. Extract ALL visible text exactly as it appears. "
                                "Gray boxes indicate redacted content — skip them and continue extracting surrounding text. "
                                "Output ONLY the raw text with no commentary, no explanations, and no refusals. "
                                "If the image contains no text at all, output exactly: [NO TEXT]"
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}
                        }
                    ]
                }
            ],
            max_tokens=4000
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("OCR failed: %s", e)
        return ""
 
 
def page_to_png_bytes(pdf_bytes: bytes, page_number: int) -> bytes:
    """Render a PDF page to PNG bytes using PyMuPDF at 2× resolution."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(page_number - 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    return pix.tobytes("png")
 
 
# def sanitize_image_for_ocr(image_bytes: bytes) -> bytes:
#     """Replace black/blue redaction blocks with gray so GPT-Vision doesn't refuse."""
#     img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
#     arr = np.array(img)
#     black_mask = (arr[:, :, 0] < 30) & (arr[:, :, 1] < 30) & (arr[:, :, 2] < 30)
#     blue_mask  = (arr[:, :, 2] > 150) & (arr[:, :, 0] < 80) & (arr[:, :, 1] < 80)
#     arr[black_mask | blue_mask] = [200, 200, 200]
#     buf = io.BytesIO()
#     Image.fromarray(arr).save(buf, format="PNG")
#     return buf.getvalue()
 
 
def sanitize_image_for_ocr(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)

    # Detect black and blue pixels
    black_mask = (arr[:, :, 0] < 30) & (arr[:, :, 1] < 30) & (arr[:, :, 2] < 30)
    blue_mask  = (arr[:, :, 2] > 150) & (arr[:, :, 0] < 80) & (arr[:, :, 1] < 80)
    combined_mask = (black_mask | blue_mask)

    try:
        import cv2 # type: ignore

        combined_mask_uint8 = combined_mask.astype(np.uint8) * 255

        num_features, labeled, stats, _ = cv2.connectedComponentsWithStats(
            combined_mask_uint8, connectivity=8
        )

        MIN_REDACTION_PIXELS = 500

        for region_id in range(1, num_features):
            region_size = stats[region_id, cv2.CC_STAT_AREA]
            if region_size > MIN_REDACTION_PIXELS:
                arr[labeled == region_id] = [255, 255, 255]

    except Exception as e:
        # logger.warning("cv2 not available, using fallback: %s", e)

        # 🔁 Fallback: simple masking (no connected components)
        arr[combined_mask] = [255, 255, 255]

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()
 
 
def _ocr_page_sync(page_num: int, pdf_bytes: bytes) -> tuple[int, str]:
    """Blocking: renders page → sanitizes → calls GPT-Vision. Runs in a thread."""
    img_bytes = page_to_png_bytes(pdf_bytes, page_num)
    img_bytes = sanitize_image_for_ocr(img_bytes)
    text = vision_ocr_image(img_bytes)
    return page_num, text
 
 
# =====================================================
# MAIN EXTRACTION  — hybrid: PyPDF2 first, OCR fallback
# =====================================================
 
async def extract_text_from_pdf_async(pdf_bytes: bytes) -> dict[int, str]:
    """
    Pass 1 — PyPDF2 fast text extraction (free, instant, perfect for text PDFs).
              This is IDENTICAL to the original working RAG pipeline.
    Pass 2 — GPT-Vision OCR only for pages whose extracted text is < 30 chars
              (i.e. scanned / image-only pages).  Runs concurrently.
    """
    reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
 
    pages_text: dict[int, str] = {}
    ocr_needed: list[int] = []
 
    # ── Pass 1: original PyPDF2 path (unchanged from your working version) ──
    for page_num, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
 
        if len(text.strip()) >= 30:
            # Text-based page — store as-is, exactly like the original pipeline
            pages_text[page_num] = text
            logger.debug("Page %s: text-based (%s chars)", page_num, len(text))
        else:
            # Image-based page — queue for OCR
            ocr_needed.append(page_num)
            logger.info("Page %s: image-based, queuing for GPT-Vision OCR", page_num)
 
    logger.info(
        "PDF pass-1 done: total=%s text_pages=%s ocr_needed=%s",
        total_pages, len(pages_text), len(ocr_needed)
    )
 
    if not ocr_needed:
        # Pure text PDF — return immediately, no API calls at all
        return pages_text
 
    # ── Pass 2: parallel GPT-Vision OCR for image-only pages ──
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_OCR)
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_OCR)
 
    async def ocr_one(page_num: int) -> tuple[int, str]:
        async with semaphore:
            return await loop.run_in_executor(
                executor, _ocr_page_sync, page_num, pdf_bytes
            )
 
    results = await asyncio.gather(
        *[ocr_one(p) for p in ocr_needed],
        return_exceptions=True
    )
 
    for result in results:
        if isinstance(result, Exception):
            logger.error("OCR task failed: %s", result)
            continue
        page_num, text = result
        pages_text[page_num] = text
 
    executor.shutdown(wait=False)
    logger.info("PDF extraction complete: %s total pages", total_pages)
    return pages_text
 
 
def extract_text_from_pdf(pdf_bytes: bytes) -> dict[int, str]:
    """Sync wrapper — for any caller that can't use async."""
    return asyncio.run(extract_text_from_pdf_async(pdf_bytes))
 
async def extract_text_from_docx_async(file_bytes: bytes):
    doc = Document(BytesIO(file_bytes))
    
    pages = {}
    text_parts = []

    for para in doc.paragraphs:
        if para.text.strip():
            text_parts.append(para.text)

    pages[1] = "\n".join(text_parts)

    return pages

async def extract_text_from_excel_async(file_bytes: bytes):
    excel_file = pd.ExcelFile(BytesIO(file_bytes))

    pages = {}

    for idx, sheet_name in enumerate(excel_file.sheet_names, start=1):

        df = pd.read_excel(
            excel_file,
            sheet_name=sheet_name,
            dtype=str
        ).fillna("")

        pages[idx] = (
            f"Sheet: {sheet_name}\n\n"
            + df.to_string(index=False)
        )

    return pages

async def extract_text_from_ppt_async(file_bytes: bytes):
    prs = Presentation(BytesIO(file_bytes))

    pages = {}

    for slide_no, slide in enumerate(prs.slides, start=1):

        slide_text = []

        for shape in slide.shapes:

            if hasattr(shape, "text"):
                txt = shape.text.strip()

                if txt:
                    slide_text.append(txt)

        pages[slide_no] = "\n".join(slide_text)

    return pages

async def extract_text_from_csv_async(file_bytes: bytes):
    df = pd.read_csv(
        BytesIO(file_bytes),
        dtype=str
    ).fillna("")

    return {
        1: df.to_string(index=False)
    }
    
async def extract_text_from_txt_async(file_bytes: bytes):
    return {
        1: file_bytes.decode(
            "utf-8",
            errors="ignore"
        )
    }
 
# =====================================================
# UPLOADED FILE ENTRY POINT  (PDF or image)
# =====================================================
 
async def extract_text_from_uploaded_file_async(
    file_name: str, file_bytes: bytes
) -> dict[int, str]:
    lower = file_name.lower()
    if lower.endswith(".pdf"):
        return await extract_text_from_pdf_async(file_bytes)
    elif lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        # Single image — one OCR call, no parallelism needed
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, vision_ocr_image, file_bytes)
        return {1: text}
    elif lower.endswith(".docx"):
        return await extract_text_from_docx_async(file_bytes)

    elif lower.endswith((".xlsx", ".xls")):
        return await extract_text_from_excel_async(file_bytes)

    elif lower.endswith((".pptx", ".ppt")):
        return await extract_text_from_ppt_async(file_bytes)

    elif lower.endswith(".csv"):
        return await extract_text_from_csv_async(file_bytes)

    elif lower.endswith(".txt"):
        return await extract_text_from_txt_async(file_bytes)

    return {1: ""}
 
 
# =====================================================
# CHUNKING  (completely unchanged)
# =====================================================
 
def split_into_chunks(pages_text: Dict[int, str]) -> List[Dict]:
    chunks = []
    chunk_index = 0
 
    for page_num in sorted(pages_text.keys()):
        text = pages_text[page_num]
        words = text.split()
 
        for i in range(0, len(words), CHUNK_SIZE - CHUNK_OVERLAP):
            chunk_words = words[i:i + CHUNK_SIZE]
            chunk_text = " ".join(chunk_words)
 
            if chunk_text.strip():
                chunks.append({
                    "page_number": page_num,
                    "chunk_index": chunk_index,
                    "text": chunk_text,
                    "char_count": len(chunk_text),
                })
                chunk_index += 1
 
    return chunks