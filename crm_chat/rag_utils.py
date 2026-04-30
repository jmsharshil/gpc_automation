"""
RAG Utilities for Large PDF Processing
Handles:
- PDF text extraction
- Intelligent chunking
- Vector embeddings
- Semantic search
"""

import io
import logging
import numpy as np
from typing import List, Tuple, Dict, Optional
import PyPDF2
import json
import hashlib
import fitz
import pytesseract
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"
logger = logging.getLogger(__name__)

# Constants
CHUNK_SIZE = 1000  # tokens per chunk (approximate)
CHUNK_OVERLAP = 200  # overlap between chunks
EMBEDDING_DIMENSION = 1536  # for OpenAI text-embedding-3-small

def extract_text_from_pdf(pdf_bytes: bytes) -> Dict[int, str]:
    """
    Extract text from PDF bytes.
    - Text-based pages → fast PyMuPDF extraction
    - Image-based pages → OCR fallback via pytesseract
    Works with: text-only, image-only, and mixed PDFs.
    Returns: {page_number: text}
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text = {}
 
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text().strip()
 
            if len(text) < 50:  # image-based page → OCR
                logger.info(
                    "Page %s is image-based (%s chars), applying OCR",
                    page_num + 1,
                    len(text)
                )
                try:
                    mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
                    pix = page.get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    text = pytesseract.image_to_string(img, lang="eng")
                    logger.info(
                        "OCR extracted %s chars from page %s",
                        len(text),
                        page_num + 1
                    )
                except Exception as ocr_err:
                    logger.warning(
                        "OCR failed for page %s: %s",
                        page_num + 1,
                        ocr_err
                    )
                    text = ""
 
            pages_text[page_num + 1] = text
 
        logger.info(
            "Extraction complete: total_pages=%s text_pages=%s ocr_pages=%s",
            len(doc),
            sum(1 for t in pages_text.values() if len(t) >= 50),
            sum(1 for t in pages_text.values() if len(t) < 50),
        )
        return pages_text
 
    except Exception as e:
        logger.error("PDF extraction failed: %s", e)
        raise

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


def split_into_chunks(pages_text: Dict[int, str]) -> List[Dict]:
    """
    Split extracted text into overlapping chunks.
    
    Returns: [
        {
            'page_number': int,
            'chunk_index': int,
            'text': str,
            'char_count': int,
        },
        ...
    ]
    """
    chunks = []
    chunk_index = 0
    logger.info(
        "Starting split_into_chunks pages=%s chunk_size=%s overlap=%s",
        len(pages_text),
        CHUNK_SIZE,
        CHUNK_OVERLAP,
    )
    
    for page_num in sorted(pages_text.keys()):
        text = pages_text[page_num]
        
        # Simple token approximation: 1 token ≈ 4 chars
        words = text.split()
        page_chunk_start = len(chunks)
        logger.debug(
            "Chunking page=%s word_count=%s char_count=%s",
            page_num,
            len(words),
            len(text or ""),
        )
        
        # Create chunks with overlap
        for i in range(0, len(words), CHUNK_SIZE - CHUNK_OVERLAP):
            chunk_words = words[i:i + CHUNK_SIZE]
            chunk_text = " ".join(chunk_words)
            
            if chunk_text.strip():  # Skip empty chunks
                chunks.append({
                    'page_number': page_num,
                    'chunk_index': chunk_index,
                    'text': chunk_text,
                    'char_count': len(chunk_text),
                })
                logger.debug(
                    "Created chunk page=%s chunk_index=%s char_count=%s word_range=%s-%s",
                    page_num,
                    chunk_index,
                    len(chunk_text),
                    i,
                    i + len(chunk_words),
                )
                chunk_index += 1

        logger.info(
            "Completed chunking page=%s chunks_created=%s running_total=%s",
            page_num,
            len(chunks) - page_chunk_start,
            len(chunks),
        )
    
    logger.info("Finished split_into_chunks total_chunks=%s", len(chunks))
    return chunks


async def get_embeddings(texts: List[str], client) -> List[List[float]]:
    """
    Get embeddings from OpenAI API.
    
    Args:
        texts: List of texts to embed
        client: OpenAI client
    
    Returns: List of embedding vectors
    """
    try:
        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input=texts,
            encoding_format="float"
        )
        
        embeddings = [item.embedding for item in response.data]
        return embeddings
    except Exception as e:
        logger.error(f"Embedding request failed: {e}")
        raise


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    arr1 = np.array(vec1)
    arr2 = np.array(vec2)
    
    dot_product = np.dot(arr1, arr2)
    norm1 = np.linalg.norm(arr1)
    norm2 = np.linalg.norm(arr2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(dot_product / (norm1 * norm2))


def semantic_search(
    query_embedding: List[float],
    chunk_embeddings: List[Tuple[int, List[float]]],  # (chunk_id, embedding)
    top_k: int = 10,
    min_similarity: float = 0.3
) -> List[int]:
    """
    Returns: List of chunk indices (highest similarity first)
    """
    similarities = []
    
    for chunk_id, chunk_emb in chunk_embeddings:
        sim = cosine_similarity(query_embedding, chunk_emb)
        if sim >= min_similarity:
            similarities.append((chunk_id, sim))
    
    # Sort by similarity (descending)
    similarities.sort(key=lambda x: x[1], reverse=True)
    
    # Return top-k chunk indices
    return [chunk_id for chunk_id, _ in similarities[:top_k]]


def build_context_from_chunks(
    chunks: List[Dict],
    chunk_ids: List[int],
    max_context_chars: int = 10000
) -> str:
    """
    Build context string from relevant chunks.
    
    Args:
        chunks: All chunks
        chunk_ids: Indices of relevant chunks
        max_context_chars: Maximum total characters to include
    
    Returns: Formatted context string
    """
    context_parts = []
    total_chars = 0
    
    for chunk_id in chunk_ids:
        if chunk_id >= len(chunks):
            continue
        
        chunk = chunks[chunk_id]
        page_ref = f"[Page {chunk['page_number']}, Chunk {chunk['chunk_index']}]"
        chunk_text = f"{page_ref}\n{chunk['text']}"
        
        if total_chars + len(chunk_text) > max_context_chars:
            break
        
        context_parts.append(chunk_text)
        total_chars += len(chunk_text)
    
    separator = "\n\n" + "="*50 + "\n\n"
    return separator.join(context_parts)


def truncate_text(text: str, max_chars: int = 100000) -> str:
    """Truncate text to max length."""
    if len(text) > max_chars:
        return text[:max_chars-3] + "..."
    return text


class RAGPipeline:
    """End-to-end RAG pipeline for document Q&A"""
    
    def __init__(self, openai_client=None):
        self.client = openai_client
        self.chunks = []
        self.chunk_embeddings = []
    
    async def process_pdf(self, pdf_bytes: bytes) -> int:
        """
        Process PDF and store embeddings.
        Returns: Number of chunks created
        """
        # Extract text
        pages_text = extract_text_from_pdf(pdf_bytes)
        logger.info(f"Extracted {len(pages_text)} pages")
        
        # Create chunks
        self.chunks = split_into_chunks(pages_text)
        logger.info(f"Created {len(self.chunks)} chunks")
        
        # Get embeddings (batch by 100 to avoid rate limits)
        texts_to_embed = [chunk['text'] for chunk in self.chunks]
        all_embeddings = []
        
        batch_size = 100
        for i in range(0, len(texts_to_embed), batch_size):
            batch = texts_to_embed[i:i+batch_size]
            batch_embeddings = await get_embeddings(batch, self.client)
            all_embeddings.extend(batch_embeddings)
            logger.info(f"Embedded {min(i+batch_size, len(texts_to_embed))}/{len(texts_to_embed)} chunks")
        
        self.chunk_embeddings = all_embeddings
        return len(self.chunks)
    
    async def answer_question(
        self,
        question: str,
        max_context_chunks: int = 10,
        max_context_chars: int = 10000
    ) -> Tuple[str, List[int], str]:
        """
        Answer a question using the document.
        
        Returns: (answer, relevant_chunk_ids, context_used)
        """
        if not self.chunks:
            raise ValueError("No document loaded. Process a PDF first.")
        
        # Get question embedding
        question_embedding = await get_embeddings([question], self.client)
        query_embedding = question_embedding[0]
        
        # Find relevant chunks
        chunk_embeddings_with_ids = [
            (i, emb) for i, emb in enumerate(self.chunk_embeddings)
        ]
        relevant_chunk_ids = semantic_search(
            query_embedding,
            chunk_embeddings_with_ids,
            top_k=max_context_chunks
        )
        
        # Build context
        context = build_context_from_chunks(
            self.chunks,
            relevant_chunk_ids,
            max_context_chars
        )
        
        # Build prompt
        system_prompt = """You are a helpful assistant that answers questions based ONLY on the provided document context.

IMPORTANT RULES:
1. Only use information from the provided context
2. If the answer is not in the context, say "The document does not contain information about this topic"
3. Cite the page numbers where you found information
4. Be concise and accurate"""
        
        user_prompt = f"""Document Context:
{context}

Question: {question}

Answer based ONLY on the information above. Do not use external knowledge."""
        
        return system_prompt, user_prompt, relevant_chunk_ids, context