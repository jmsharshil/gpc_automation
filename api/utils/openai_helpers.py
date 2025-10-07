# utils/openai_helpers.py (replace the previous call_openai_compare or its extraction logic)

import json
from django.conf import settings
from openai import OpenAI

client = OpenAI(api_key=settings.OPENAI_API_KEY)

SIMILARITY_VALUES = {"high": "High", "medium": "Medium", "low": "Low"}

def _clean_text(s):
    return (s or "").strip()

def parse_model_json(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                return None
    return None

def _fallback_heuristic(company_desc, user_desc, note=None):
    a = (company_desc or "").lower()
    b = (user_desc or "").lower()
    if not a or not b:
        sim = "Low"
        rationale = "Company or user description missing; cannot compare."
    else:
        a_words = set([w for w in a.split() if len(w) > 3])
        b_words = set([w for w in b.split() if len(w) > 3])
        inter = a_words & b_words
        overlap = len(inter) / max(1, min(len(a_words), len(b_words)))
        if overlap > 0.4:
            sim = "High"
        elif overlap > 0.15:
            sim = "Medium"
        else:
            sim = "Low"
        rationale = f"Keyword overlap heuristic ({len(inter)} shared words)."
    if note:
        rationale = f"{rationale} Note: {note}"
    return {"similarity": sim, "rationale": rationale}

def _extract_text_from_chat_response(resp):
    """
    Robust extractor: supports dict-like or object shapes returned by different SDK versions.
    Returns string ('' if not found).
    """
    try:
        choices = getattr(resp, "choices", None) or resp.get("choices", None)
    except Exception:
        # resp isn't subscriptable or has no get - just return empty
        return ""
    if not choices:
        return ""

    choice = choices[0]
    # Try several access patterns
    # 1) choice.message could be a dict
    msg = getattr(choice, "message", None)
    if msg is None and isinstance(choice, dict):
        msg = choice.get("message")
    if isinstance(msg, dict):
        return msg.get("content", "") or msg.get("text", "") or ""
    if msg is not None:
        # msg is likely an object with attribute 'content'
        return getattr(msg, "content", "") or getattr(msg, "text", "") or ""
    # 2) maybe choice has 'text' or 'content' directly (older shapes)
    text = getattr(choice, "text", None) or (choice.get("text") if isinstance(choice, dict) else None)
    if text:
        return text
    content = getattr(choice, "content", None) or (choice.get("content") if isinstance(choice, dict) else None)
    return content or ""

def call_openai_compare(company_desc, user_desc, model=None, timeout=None):
    model = model or getattr(settings, "OPENAI_MODEL", "gpt-3.5-turbo")
    timeout = timeout or getattr(settings, "OPENAI_TIMEOUT", 15)

#     prompt = f"""
# You are an assistant that compares business descriptions.

# Input:
# - company_description: \"\"\"{company_desc or ''}\"\"\"
# - subject_description: \"\"\"{user_desc or ''}\"\"\"

# Task:
# You are performing comparable company screening for a private company valuation using the Guideline Public Company (GPC) Method.

# Steps:
# 1. Read and understand the subject company’s business model, technology, and end markets.
# 2. Read the public company’s business description.
# 3. Apply the screening logic as follows:
#    - Each entry in screening_keywords is treated as a separate screening phrase.
#    - For each phrase:
#        - Split the phrase into individual words.
#        - The phrase matches only if **all words** in that phrase appear anywhere in the business description, in any order or sentence.
#        - Matching is **case-insensitive**.
#        - **Word stemming is applied**: words are matched to their root forms (for example, “acquire” matches “acquiring”, “acquired”, “acquisition”).
#        - If any word in a phrase is missing, that phrase does not match.
#    - Apply **OR logic** across all phrases:
#        - If at least one phrase fully matches, set `passes_screen` to "Yes".
#        - If none match, set `passes_screen` to "No".
# 4. Based on qualitative similarity between the subject company and the public company, assign a similarity category:
#    - "High" → Strong overlap in industry, technology, and target markets.
#    - "Medium" → Partial overlap in technology or market focus.
#    - "Low" → Minimal or no overlap in business model, industry, or technology.
 
# Return a JSON object exactly with these keys:
# - similarity: must be one of "High", "Medium", or "Low" (exactly those strings, capitalized).
# - passes_screen: must be "Yes" if at least one screening phrase matched, otherwise "No".
# - rationale: a concise (1–3 sentence) explanation describing the similarity level and screening outcome.
 
# Constraints:
# - Output only valid JSON (no extra commentary or formatting).
# - Matching is case-insensitive, allows words to appear in any order or sentence, and applies stemming.
# - All words in a multi-word phrase must be present for a match.
# - Keep rationale concise (1–3 sentences).
# - If company_description is empty, set similarity to "Low", passes_screen to "No", and rationale to "Insufficient company information to assess comparability."
# """


    prompt = f"""
You are an assistant that compares business descriptions.

Input:
- company_description: \"\"\"{company_desc or ''}\"\"\"
- user_description: \"\"\"{user_desc or ''}\"\"\"

Task:
Return a JSON object exactly with these keys:
- similarity: must be one of "High", "Medium", or "Low" (exactly those strings, capitalized).
- rationale: a short explanation (1-3 sentences) explaining why you chose that similarity.

Constraints:
- Produce only valid JSON (no extra commentary).
- Keep rationale concise (one to three short sentences).
- If company_description is empty, set similarity to "Low" and rationale to explain missing data.
"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that compares short business descriptions."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=250,
            temperature=0.0,
            timeout=timeout,
        )

        text = _extract_text_from_chat_response(resp)
        parsed = parse_model_json(text)
        if not parsed:
            return _fallback_heuristic(company_desc, user_desc, note=f"unparsable_response:{(text or '')[:160]}")
        sim = parsed.get("similarity") or parsed.get("Similarity") or parsed.get("score")
        rationale = parsed.get("rationale") or parsed.get("explanation") or ""
        sim_val = None
        if isinstance(sim, str):
            k = sim.strip().lower()
            sim_val = SIMILARITY_VALUES.get(k)
            if not sim_val:
                if "high" in k:
                    sim_val = "High"
                elif "medium" in k:
                    sim_val = "Medium"
                elif "low" in k:
                    sim_val = "Low"
        if not sim_val:
            return _fallback_heuristic(company_desc, user_desc, note="invalid_similarity_value")
        return {"similarity": sim_val, "rationale": _clean_text(rationale)}
    except Exception as e:
        return _fallback_heuristic(company_desc, user_desc, note=f"openai_error:{str(e)[:200]}")
