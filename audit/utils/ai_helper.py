import numpy as np
from audit.models import DocumentChunk


def cosine_similarity(a, b):
    a = np.array(a)
    b = np.array(b)
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def get_top_chunks(query_embedding, top_k=5):
    chunks = DocumentChunk.objects.filter(status="completed")[:2000]

    query_vec = np.array(query_embedding)

    scored = []

    for chunk in chunks:
        chunk_vec = np.array(chunk.embedding)

        score = np.dot(query_vec, chunk_vec) / (
            np.linalg.norm(query_vec) * np.linalg.norm(chunk_vec)
        )

        scored.append((score, chunk.content))

    scored.sort(key=lambda x: x[0], reverse=True)

    return [c[1] for c in scored[:top_k]]