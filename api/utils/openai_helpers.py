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
        return ""
    if not choices:
        return ""

    choice = choices[0]
    msg = getattr(choice, "message", None)
    if msg is None and isinstance(choice, dict):
        msg = choice.get("message")
    if isinstance(msg, dict):
        return msg.get("content", "") or msg.get("text", "") or ""
    if msg is not None:
        return getattr(msg, "content", "") or getattr(msg, "text", "") or ""
    text = getattr(choice, "text", None) or (choice.get("text") if isinstance(choice, dict) else None)
    if text:
        return text
    content = getattr(choice, "content", None) or (choice.get("content") if isinstance(choice, dict) else None)
    return content or ""

def call_openai_compare(company_desc, user_desc, model=None, timeout=None):
    model = model or getattr(settings, "OPENAI_MODEL", "gpt-3.5-turbo")
    timeout = timeout or getattr(settings, "OPENAI_TIMEOUT", 15)

    prompt = f"""
        Subject Company:

        \"\"\"{user_desc or ''}\"\"\"

        Comparable Company:

        \"\"\"{company_desc or ''}\"\"\"

        Task:

        Using professional judgment, assess how similar the two companies’ business models are.

        Consider:
        - Value proposition
        - Revenue model
        - Target customers
        - Product/service type
        - Role in the value chain

        Return ONLY valid JSON:
        {{
        "similarity": "High" | "Medium" | "Low",
        "rationale": "2–3 sentence analyst-style explanation",
        "confidence": "High" | "Medium" | "Low"
        }}
        """

# New Promot - responding "High" more often - Harshil
#     prompt = f"""
# You are a financial valuation expert comparing companies using the Guideline Public Company (GPC) Method. Your goal is to identify comparable companies for accurate valuation.

# Input:
# - subject_description: \"\"\"{user_desc or ''}\"\"\"
# - company_description: \"\"\"{company_desc or ''}\"\"\"

# Task:
# Perform a comprehensive comparability analysis to find companies that could serve as valuation comparables.

# **1. KEYWORD SCREENING (if subject_description contains keywords):**
#    - If subject_description is keywords/phrases (< 50 words):
#      * Apply word stemming: "manage" matches "management", "managed", "managing"
#      * Each phrase must have ALL words present (after stemming)
#      * OR logic: ANY matching phrase → passes_screen = "Yes"

# **2. COMPARABILITY ANALYSIS:**

# Evaluate these key dimensions:

# A. **Industry & Market Focus**
#    - Same or adjacent industries
#    - Overlapping market segments
#    - Similar end customers or use cases
#    - Examples: "Financial technology" ≈ "Fintech solutions" ≈ "Banking software" = Strong match

# B. **Business Model & Operations**
#    - Revenue model (SaaS, licensing, hardware sales, services, etc.)
#    - Business type (B2B, B2C, marketplace, platform)
#    - Delivery method (cloud, on-premise, hybrid)
#    - Examples: "Cloud-based subscription" ≈ "SaaS platform" = Strong match

# C. **Product/Service Category**
#    - What problem do they solve?
#    - Core offerings and solutions
#    - Technology or methodology used
#    - Examples: "HR software" ≈ "Human capital management" ≈ "Workforce solutions" = Strong match

# D. **Customer & Market Segment**
#    - Target company size (enterprise, mid-market, SMB)
#    - Geographic markets
#    - Industry verticals served
#    - Examples: "Enterprise customers" ≈ "Large organizations" = Strong match

# **SIMILARITY SCORING - BE GENEROUS WITH HIGH:**

# **"High" - Assign when:**
# - Companies operate in the SAME or CLOSELY RELATED industries
# - Share SIMILAR business models or revenue approaches
# - Target SIMILAR customer segments or markets
# - Offer COMPARABLE products/services (even with different terminology)
# - Would be considered REASONABLE comparables by valuation professionals
# - At least 2 dimensions show STRONG alignment
# - **Think broadly**: If an investor would group them together, it's likely "High"

# Examples of HIGH similarity:
# - "Healthcare SaaS" vs "Medical software solutions"
# - "E-commerce platform" vs "Online retail technology"
# - "Cybersecurity services" vs "Information security solutions"
# - "AI-powered analytics" vs "Machine learning data insights"
# - "Payment processing" vs "Transaction management systems"

# **"Medium" - Assign when:**
# - Companies are in RELATED but not identical industries
# - Different business models but serve similar markets
# - Adjacent product categories or customer segments
# - 1 dimension shows strong alignment OR 2 dimensions show moderate alignment
# - Could be considered as comparables with some adjustments

# **"Low" - Assign when:**
# - Completely DIFFERENT industries with no overlap
# - Fundamentally different business models AND markets
# - No meaningful connection in products, customers, or operations
# - Would NOT be used as comparables in valuation

# **IMPORTANT GUIDELINES:**
# 1. **Be inclusive, not exclusive**: When in doubt between High and Medium, choose High
# 2. **Focus on economic substance**: Look at what they DO, not just keywords
# 3. **Apply synonym matching liberally**: Different words often mean the same thing
# 4. **Consider industry context**: Understand sector-specific terminology
# 5. **Think like an investor**: Would these companies trade at similar multiples?
# 6. **Conceptual similarity matters**: "software for hospitals" = "healthcare IT solutions" = HIGH
# 7. **Don't penalize for description brevity**: Limited info shouldn't automatically mean Low

# **SPECIAL RULES:**
# - If company_description is empty/vague → "Low" similarity
# - Use word stemming and synonym recognition throughout
# - Output confidence level based on description quality

# Return ONLY valid JSON:
# {{
#   "similarity": "High" | "Medium" | "Low",
#   "passes_screen": "Yes" | "No",
#   "rationale": "Explain the match with specific details (2-3 sentences)",
#   "confidence": "High" | "Medium" | "Low"
# }}
# """

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an experienced investment analyst who identifies comparable companies for valuations. You understand that comparability exists on a spectrum and that companies in related industries or with similar business models are often valid comparables. Be inclusive in your High ratings - if two companies would reasonably be grouped together by investors or analysts, rate them as High similarity."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.4,
            timeout=timeout,
        )

        text = _extract_text_from_chat_response(resp)
        parsed = parse_model_json(text)
        if not parsed:
            return _fallback_heuristic(company_desc, user_desc, note=f"unparsable_response:{(text or '')[:160]}")
        
        sim = parsed.get("similarity") or parsed.get("Similarity") or parsed.get("score")
        rationale = parsed.get("rationale") or parsed.get("explanation") or ""
        confidence = parsed.get("confidence") or "Medium"
        
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
        
        result = {
            "similarity": sim_val,
            "rationale": _clean_text(rationale)
        }
        
        # Add confidence if available
        if confidence:
            result["confidence"] = confidence
            
        return result
    
    except Exception as e:
        return _fallback_heuristic(company_desc, user_desc, note=f"openai_error:{str(e)[:200]}")


## Below Working code, just changing result more as High Priority.

# # utils/openai_helpers.py (replace the previous call_openai_compare or its extraction logic)

# import json
# from django.conf import settings
# from openai import OpenAI

# client = OpenAI(api_key=settings.OPENAI_API_KEY)

# SIMILARITY_VALUES = {"high": "High", "medium": "Medium", "low": "Low"}

# def _clean_text(s):
#     return (s or "").strip()

# def parse_model_json(text):
#     text = (text or "").strip()
#     if not text:
#         return None
#     try:
#         return json.loads(text)
#     except Exception:
#         start = text.find('{')
#         end = text.rfind('}')
#         if start != -1 and end != -1 and end > start:
#             try:
#                 return json.loads(text[start:end+1])
#             except Exception:
#                 return None
#     return None

# def _fallback_heuristic(company_desc, user_desc, note=None):
#     a = (company_desc or "").lower()
#     b = (user_desc or "").lower()
#     if not a or not b:
#         sim = "Low"
#         rationale = "Company or user description missing; cannot compare."
#     else:
#         a_words = set([w for w in a.split() if len(w) > 3])
#         b_words = set([w for w in b.split() if len(w) > 3])
#         inter = a_words & b_words
#         overlap = len(inter) / max(1, min(len(a_words), len(b_words)))
#         if overlap > 0.4:
#             sim = "High"
#         elif overlap > 0.15:
#             sim = "Medium"
#         else:
#             sim = "Low"
#         rationale = f"Keyword overlap heuristic ({len(inter)} shared words)."
#     if note:
#         rationale = f"{rationale} Note: {note}"
#     return {"similarity": sim, "rationale": rationale}

# def _extract_text_from_chat_response(resp):
#     """
#     Robust extractor: supports dict-like or object shapes returned by different SDK versions.
#     Returns string ('' if not found).
#     """
#     try:
#         choices = getattr(resp, "choices", None) or resp.get("choices", None)
#     except Exception:
#         # resp isn't subscriptable or has no get - just return empty
#         return ""
#     if not choices:
#         return ""

#     choice = choices[0]
#     # Try several access patterns
#     # 1) choice.message could be a dict
#     msg = getattr(choice, "message", None)
#     if msg is None and isinstance(choice, dict):
#         msg = choice.get("message")
#     if isinstance(msg, dict):
#         return msg.get("content", "") or msg.get("text", "") or ""
#     if msg is not None:
#         # msg is likely an object with attribute 'content'
#         return getattr(msg, "content", "") or getattr(msg, "text", "") or ""
#     # 2) maybe choice has 'text' or 'content' directly (older shapes)
#     text = getattr(choice, "text", None) or (choice.get("text") if isinstance(choice, dict) else None)
#     if text:
#         return text
#     content = getattr(choice, "content", None) or (choice.get("content") if isinstance(choice, dict) else None)
#     return content or ""

# def call_openai_compare(company_desc, user_desc, model=None, timeout=None):
#     model = model or getattr(settings, "OPENAI_MODEL", "gpt-3.5-turbo")
#     timeout = timeout or getattr(settings, "OPENAI_TIMEOUT", 15)

# ## Harshil made new prompt with word steamming addition - objective "High" matches more likely
#     prompt = f"""
    
#     You are an assistant that compares business descriptions.

# Input:
# - company_description: \"\"\"{company_desc or ''}\"\"\"
# - subject_description: \"\"\"{user_desc or ''}\"\"\"
    
#     Task:
# You are performing comparable company screening for a private company valuation using the Guideline Public Company (GPC) Method.

# Steps:
# 1. Read and understand the subject company’s business model, technology, and end markets.
# 2. Read the public company’s business description.
# 3. Screening Logic:
#    - Each entry in screening_keywords is treated as a separate screening phrase.
#    - For each phrase:
#        - Split the phrase into individual words.
#        - A word is considered present if **any morphological form of that word** (via stemming) appears anywhere in the business description.  
#          Examples: "manage" should match "manages", "manager", "managed", "management".
#        - Stemming must be applied bidirectionally:
#            - The root form of the keyword matches variations in the descriptions.
#            - Variations in the descriptions are reduced to root form to check against the keyword.
#        - The phrase matches only if **all** stemmed words appear (case-insensitive, any order).
#    - OR logic across all screening phrases:
#        - If at least one phrase fully matches, `passes_screen = "Yes"`.
#        - If none match, `passes_screen = "No"`.

# 4. Similarity assessment:
#    - Consider industry, technology, market focus, and business activities.
#    - Apply **word-stemming** when comparing subject and company descriptions, so that conceptual matches are not missed due to different word forms.
#    - Assign similarity:
#        - "High" → Strong overlap in industry, technology, or markets.
#        - "Medium" → Partial overlap.
#        - "Low" → Minimal or no overlap.

# Return a JSON object exactly with these keys:
# - similarity: "High", "Medium", or "Low"
# - passes_screen: "Yes" or "No"
# - rationale: concise 1–3 sentences explaining similarity level and screening outcome.

# Constraints:
# - Output only valid JSON.
# - Matching is case-insensitive and applies full word-stemming.
# - Words in phrases must all be present (after stemming) for a match.
# - If company_description is empty:
#    similarity = "Low"
#    passes_screen = "No"
#    rationale = "Insufficient company information to assess comparability."
    
#      """


# ## Harpreet Changed Old Prompt with this Prompt - Stable Prompt
# #     prompt = f"""
# # You are an assistant that compares business descriptions.

# # Input:
# # - company_description: \"\"\"{company_desc or ''}\"\"\"
# # - subject_description: \"\"\"{user_desc or ''}\"\"\"

# # Task:
# # You are performing comparable company screening for a private company valuation using the Guideline Public Company (GPC) Method.

# # Steps:
# # 1. Read and understand the subject company’s business model, technology, and end markets.
# # 2. Read the public company’s business description.
# # 3. Apply the screening logic as follows:
# #    - Each entry in screening_keywords is treated as a separate screening phrase.
# #    - For each phrase:
# #        - Split the phrase into individual words.
# #        - The phrase matches only if **all words** in that phrase appear anywhere in the business description, in any order or sentence.
# #        - Matching is **case-insensitive**.
# #        - **Word stemming is applied**: words are matched to their root forms (for example, “acquire” matches “acquiring”, “acquired”, “acquisition”).
# #        - If any word in a phrase is missing, that phrase does not match.
# #    - Apply **OR logic** across all phrases:
# #        - If at least one phrase fully matches, set `passes_screen` to "Yes".
# #        - If none match, set `passes_screen` to "No".
# # 4. Based on qualitative similarity between the subject company and the public company, assign a similarity category:
# #    - "High" → Strong overlap in industry, technology, and target markets.
# #    - "Medium" → Partial overlap in technology or market focus.
# #    - "Low" → Minimal or no overlap in business model, industry, or technology.
 
# # Return a JSON object exactly with these keys:
# # - similarity: must be one of "High", "Medium", or "Low" (exactly those strings, capitalized).
# # - passes_screen: must be "Yes" if at least one screening phrase matched, otherwise "No".
# # - rationale: a concise (1–3 sentence) explanation describing the similarity level and screening outcome.
 
# # Constraints:
# # - Output only valid JSON (no extra commentary or formatting).
# # - Matching is case-insensitive, allows words to appear in any order or sentence, and applies stemming.
# # - All words in a multi-word phrase must be present for a match.
# # - Keep rationale concise (1–3 sentences).
# # - If company_description is empty, set similarity to "Low", passes_screen to "No", and rationale to "Insufficient company information to assess comparability."
# # """


# #-----------------------------------------------------------------------


# ## Initial prompt version without screening logic details
# #     prompt = f"""
# # You are an assistant that compares business descriptions.

# # Input:
# # - company_description: \"\"\"{company_desc or ''}\"\"\"
# # - user_description: \"\"\"{user_desc or ''}\"\"\"

# # Task:
# # Return a JSON object exactly with these keys:
# # - similarity: must be one of "High", "Medium", or "Low" (exactly those strings, capitalized).
# # - rationale: a short explanation (1-3 sentences) explaining why you chose that similarity.

# # Constraints:
# # - Produce only valid JSON (no extra commentary).
# # - Keep rationale concise (one to three short sentences).
# # - If company_description is empty, set similarity to "Low" and rationale to explain missing data.
# # """

#     try:
#         resp = client.chat.completions.create(
#             model=model,
#             messages=[
#                 {"role": "system", "content": "You are a helpful assistant that compares short business descriptions."},
#                 {"role": "user", "content": prompt},
#             ],
#             max_tokens=250,
#             temperature=0.0,
#             timeout=timeout,
#         )

#         text = _extract_text_from_chat_response(resp)
#         parsed = parse_model_json(text)
#         if not parsed:
#             return _fallback_heuristic(company_desc, user_desc, note=f"unparsable_response:{(text or '')[:160]}")
#         sim = parsed.get("similarity") or parsed.get("Similarity") or parsed.get("score")
#         rationale = parsed.get("rationale") or parsed.get("explanation") or ""
#         sim_val = None
#         if isinstance(sim, str):
#             k = sim.strip().lower()
#             sim_val = SIMILARITY_VALUES.get(k)
#             if not sim_val:
#                 if "high" in k:
#                     sim_val = "High"
#                 elif "medium" in k:
#                     sim_val = "Medium"
#                 elif "low" in k:
#                     sim_val = "Low"
#         if not sim_val:
#             return _fallback_heuristic(company_desc, user_desc, note="invalid_similarity_value")
#         return {"similarity": sim_val, "rationale": _clean_text(rationale)}
#     except Exception as e:
#         return _fallback_heuristic(company_desc, user_desc, note=f"openai_error:{str(e)[:200]}")
