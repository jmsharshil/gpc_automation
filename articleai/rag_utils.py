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