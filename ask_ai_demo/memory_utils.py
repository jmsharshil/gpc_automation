import threading
import logging
from django.conf import settings
from .models import ChatDemo, UserOpenAISettingDemo

logger = logging.getLogger(__name__)

def _update_user_long_term_memory(user_id, chat_id):
    try:
        chat = ChatDemo.objects.filter(pk=chat_id, owner_id=user_id).first()
        if not chat:
            return

        setting, _ = UserOpenAISettingDemo.objects.get_or_create(user_id=user_id)
        
        # Get the recent 4 messages from this chat
        recent_messages = list(chat.messages_demo.all().order_by('-created_at')[:4])[::-1]
        if not recent_messages:
            return

        chat_transcript = ""
        for msg in recent_messages:
            chat_transcript += f"{msg.role.capitalize()}: {msg.content}\n"

        from openai import OpenAI
        client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
        
        system_msg = (
            "You are an AI Memory Manager. Your job is to extract long-term user preferences, "
            "facts, and knowledge from conversations and append them to a user's Context Memory.\n\n"
            "Existing Context Memory:\n"
            f"{setting.context_memory}\n\n"
            "Instructions:\n"
            "1. Review the recent chat transcript below.\n"
            "2. Identify any genuine, permanent user preferences (e.g. favorite styling, company name, default currency, preferred document formats) that should be remembered across all future conversations.\n"
            "3. If nothing new should be remembered, return the EXACT Existing Context Memory without changes.\n"
            "4. If there are new insights, APPEND them to the Existing Context Memory. Organize them clearly under specific Topics (e.g. 'Topic: [Preferences]', 'Topic: [Company Background]'). "
            "Do NOT overwrite or summarize the existing memory; only add new bullet points under the relevant topic or create a new topic.\n"
            "5. Keep it concise, organized, and objective.\n"
            "6. CRITICAL: DO NOT include short-term chatter, one-off file/image uploads, questions asked by the user, or transient tasks (e.g. DO NOT write 'User uploaded a screenshot of X' or 'User asked to explain Y'). ONLY output the fully updated memory text."
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini", # Keep it fast and cheap
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": f"Recent Chat Transcript:\n{chat_transcript}"}
            ],
            temperature=0.1,
            max_tokens=2000,
        )

        new_memory = response.choices[0].message.content.strip()
        
        # Save the updated memory
        setting.context_memory = new_memory
        setting.save(update_fields=['context_memory'])
        
        logger.info(f"Updated context_memory for user {user_id}")

    except Exception as e:
        logger.error(f"Failed to update context memory for user {user_id}: {str(e)}")

def trigger_memory_update_async(user_id, chat_id):
    """Spawns a background thread to update the user's context memory."""
    threading.Thread(target=_update_user_long_term_memory, args=(user_id, chat_id)).start()


# =====================================================================
# BUSINESS MEMORY & HYBRID RAG UTILITIES
# =====================================================================

def extract_business_memory_from_document(existing_memory: str, document_text: str, document_name: str) -> str:
    """
    Analyzes document text and extracts long-term organization rules, valuation methodologies,
    operating procedures, guidelines, and core company facts, then appends them into the
    company's Business Context Memory under organized topics.
    """
    from openai import OpenAI
    client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))

    system_prompt = (
        "You are an Enterprise AI Business Memory Manager. Your role is to analyze corporate documents "
        "and extract long-term organization rules, valuation methodologies, operating procedures, guidelines, "
        "and core company facts, then append them into the company's Business Context Memory.\n\n"
        "Existing Business Context Memory:\n"
        f"{existing_memory}\n\n"
        "Instructions:\n"
        f"1. Review the content extracted from '{document_name}' below.\n"
        "2. Identify all important permanent business rules, valuation standards, client policies, templates, terminology, or company facts.\n"
        "3. Organize the insights under clear Topic headers (e.g., 'Topic: [Company Background]', 'Topic: [Valuation Standards]', 'Topic: [Reporting Policies]').\n"
        "4. PRESERVE all existing topics and memory; only APPEND new points or create new topics.\n"
        "5. Keep bullet points concise, professional, and actionable.\n"
        "6. Do not include temporary chatter, page numbers, or formatting artifacts. Output ONLY the updated memory markdown text."
    )

    clipped_doc_text = document_text[:80_000]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Document Text:\n{clipped_doc_text}"}
            ],
            temperature=0.1,
            max_tokens=3000,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Failed to distill business memory from document {document_name}: {e}")
        return existing_memory


def process_business_document_pipeline(business_memory, file_obj, uploaded_by=None):
    """
    Hybrid Pipeline:
    1. Extracts text from document (PDF, DOCX, XLSX, PPTX, CSV, TXT, images).
    2. Saves BusinessDocument record and file attachment.
    3. Splits text into chunks and generates OpenAI embeddings for RAG.
    4. Saves BusinessDocumentChunk records for semantic vector search.
    5. Distills high-level rules & policies via LLM and appends into business_memory.content.
    """
    import asyncio
    import json
    from .models import BusinessDocument, BusinessDocumentChunk
    from .rag_utils import extract_text_from_uploaded_file_async, split_into_chunks
    from openai import OpenAI

    file_name = file_obj.name
    file_bytes = file_obj.read()
    file_obj.seek(0)

    # 1. Extract text from document using the async multi-format extractor in an isolated loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pages_text = loop.run_until_complete(
            extract_text_from_uploaded_file_async(file_name, file_bytes)
        )
    finally:
        loop.close()

    total_pages = len(pages_text) if pages_text else 1
    full_text = "\n\n".join(pages_text.values()).strip() if pages_text else ""

    # 2. Save BusinessDocument record
    file_ext = file_name.split('.')[-1].lower() if '.' in file_name else ''
    business_doc = BusinessDocument.objects.create(
        business_memory=business_memory,
        file=file_obj,
        name=file_name,
        file_type=file_ext,
        total_pages=total_pages,
        total_chunks=0,
        uploaded_by=uploaded_by,
    )

    # 3. Create RAG Chunks and Embeddings
    chunks = split_into_chunks(pages_text) if pages_text else []
    if chunks:
        client = OpenAI(api_key=getattr(settings, 'OPENAI_API_KEY', None))
        batch_size = 50
        all_embeddings = []

        for i in range(0, len(chunks), batch_size):
            batch_chunks = chunks[i:i + batch_size]
            batch_texts = [c['text'] for c in batch_chunks]
            try:
                emb_res = client.embeddings.create(
                    model="text-embedding-3-small",
                    input=batch_texts,
                    encoding_format="float"
                )
                all_embeddings.extend([item.embedding for item in emb_res.data])
            except Exception as e:
                logger.error(f"Failed to generate embeddings for business doc {file_name}: {e}")
                break

        chunk_objs = []
        for chunk, emb_vector in zip(chunks, all_embeddings):
            emb_json = json.dumps(emb_vector)
            chunk_objs.append(BusinessDocumentChunk(
                business_document=business_doc,
                page_number=chunk['page_number'],
                chunk_index=chunk['chunk_index'],
                text=chunk['text'],
                embedding=emb_json.encode('utf-8'),
                char_count=chunk['char_count'],
                embedding_model='text-embedding-3-small'
            ))

        if chunk_objs:
            BusinessDocumentChunk.objects.bulk_create(chunk_objs, batch_size=100)
            business_doc.total_chunks = len(chunk_objs)
            business_doc.save(update_fields=['total_chunks'])

    # 4. Distill high-level rules & policies into BusinessContextMemory.content
    if full_text:
        updated_content = extract_business_memory_from_document(
            existing_memory=business_memory.content or "",
            document_text=full_text,
            document_name=file_name
        )
        business_memory.content = updated_content
        business_memory.save(update_fields=['content', 'updated_at'])

    return business_doc


def get_relevant_business_chunks_for_query(query_embedding: list, max_chunks: int = 3) -> tuple:
    """
    Finds most relevant chunks across all active business documents using vectorized cosine similarity.
    Returns: (chunks: List[BusinessDocumentChunk], max_similarity: float)
    """
    import json
    import numpy as np
    from .models import BusinessDocumentChunk

    all_chunks = list(
        BusinessDocumentChunk.objects.filter(
            business_document__business_memory__is_active=True
        ).select_related('business_document').only(
            'id', 'embedding', 'page_number', 'chunk_index', 'text', 'business_document__name'
        )
    )

    if not all_chunks:
        return [], 0.0

    matrix = []
    valid_chunks = []
    for chunk in all_chunks:
        try:
            vec = json.loads(bytes(chunk.embedding).decode('utf-8'))
            matrix.append(vec)
            valid_chunks.append(chunk)
        except Exception:
            continue

    if not matrix:
        return [], 0.0

    max_chunks = min(max_chunks, len(valid_chunks))

    matrix_np = np.array(matrix, dtype=np.float32)
    query_np = np.array(query_embedding, dtype=np.float32)

    dot_products = matrix_np @ query_np
    norms = np.linalg.norm(matrix_np, axis=1) * np.linalg.norm(query_np)
    similarities = np.where(norms > 0, dot_products / norms, 0.0)

    top_indices = np.argpartition(similarities, -max_chunks)[-max_chunks:]
    top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

    top_chunks = [valid_chunks[i] for i in top_indices]
    max_sim = float(similarities[top_indices[0]]) if len(top_indices) else 0.0

    return top_chunks, max_sim

