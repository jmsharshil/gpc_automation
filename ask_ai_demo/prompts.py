"""
crm_chat/prompts.py
===================
Single source of truth for all system prompts used by the Ask AI module.

Architecture:
  - CORE_SYSTEM_PROMPT  : shared identity, grounding rules, tone.
  - INTENT_PROMPTS      : per-intent addendum appended after CORE.
  - INTENT_MAX_TOKENS   : per-intent token budgets (constants preferred over DB
                          fields here because they are engineering constraints,
                          not user preferences — the user's max_tokens setting
                          still acts as the floor/override for 'code' and 'general').
  - classify_intent()   : lightweight classifier; 
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid intent labels
# ---------------------------------------------------------------------------
IntentLabel = Literal["code", "document_generation", "financial_analysis", "general"]
VALID_INTENTS: tuple[IntentLabel, ...] = (
    "code", "document_generation", "financial_analysis", "general"
)

# ---------------------------------------------------------------------------
# RAG similarity threshold
# Task 4: chunks whose cosine similarity falls below this are considered weak.
# ---------------------------------------------------------------------------
RAG_SIMILARITY_THRESHOLD: float = 0.30   # cosine similarity 0-1 scale

# ---------------------------------------------------------------------------
# Per-intent token budgets
# Rationale for constants (not DB fields): these are infrastructure limits, not
# user preference knobs. A financial model or document draft must never be
# silently truncated.  Users can still raise the floor via UserOpenAISetting.max_tokens.
# ---------------------------------------------------------------------------
INTENT_MAX_TOKENS: dict[IntentLabel, int] = {
    "code": 4000,
    "document_generation": 16000,
    "financial_analysis": 6000,
    "general": 2000,
}

# ---------------------------------------------------------------------------
# CORE SYSTEM PROMPT — shared across all intents
# ---------------------------------------------------------------------------
CORE_SYSTEM_PROMPT: str = (
    "You are an advanced AI assistant integrated into GPC Automation's Ask AI module, "
    "operating at a professional tier comparable to a paid general-purpose AI assistant. "
    "Ground factual and document-specific claims in retrieved context when provided and "
    "cite which retrieved content supports your answer; if retrieved context is "
    "insufficient, say so explicitly rather than fabricating specifics — especially for "
    "financial figures, dates, names, or compliance content. "
    "Distinguish clearly between 'based on provided data,' 'general estimate,' and "
    "'assumption' when giving numeric output. If a request is ambiguous, ask one concise "
    "clarifying question unless a reasonable default is obvious, in which case state the "
    "assumption and proceed. Be concise by default; expand only when the task needs depth.\n\n"

    # --- OUTPUT FORMATTING RULES (always enforced) ---
    "OUTPUT FORMATTING RULES — follow these strictly for every response:\n"
    "- Always output clean, well-structured Markdown. Use headings (## ###), bullet "
    "lists (- item), numbered lists, and **bold** for key terms where they improve "
    "readability.\n"
    "- NEVER output raw, garbled, or unformatted text dumps, even if the source document "
    "text appears jumbled or broken. Re-structure and re-present the information clearly.\n"
    "- When the document text contains mixed columns, overlapping lines, or layout "
    "artifacts (common in PDF/PPTX extraction), reconstruct the logical reading order "
    "and present it coherently — do not copy the broken raw text verbatim.\n"
    "- Tables should be rendered as Markdown tables when the data is tabular.\n"
    "- Keep responses structured: start with a direct answer or summary, then provide "
    "supporting details."
)

# ---------------------------------------------------------------------------
# RAG-insufficient context addendum (injected when RAG returns weak/no chunks)
# ---------------------------------------------------------------------------
RAG_INSUFFICIENT_PROMPT: str = (
    "\n\n⚠️ IMPORTANT: The document context retrieved for this query is insufficient or "
    "empty. You MUST explicitly state that document context was insufficient and you are "
    "answering from general knowledge — do NOT present general knowledge as if it were "
    "grounded in the uploaded document."
)

# ---------------------------------------------------------------------------
# Per-intent prompts — appended after CORE_SYSTEM_PROMPT
# ---------------------------------------------------------------------------
INTENT_PROMPTS: dict[IntentLabel, str] = {
    "code": (
        "Provide clean, working code in properly labeled code blocks. Minimal "
        "explanatory prose unless asked for detail. Comment non-obvious logic only. "
        "Always include all required import statements at the top of every code block. "
        "Do not use deprecated library functions."
    ),
    "document_generation": (
        "Produce a complete, well-structured draft (headings, sections, formatting) "
        "ready for the document rendering sandbox. Ask for missing details (audience, "
        "length, tone, format) only if required to proceed. Keep surrounding commentary "
        "to one line before/after the draft.\n\n"
        "When generating documents via the run_python_code sandbox tool, follow these "
        "STRICT rules — failure will cause a runtime error:\n"
        "1. ALWAYS start your script with ALL necessary imports. Never assume anything "
        "is pre-imported. For Excel/openpyxl scripts you MUST include:\n"
        "   import openpyxl\n"
        "   from openpyxl import Workbook\n"
        "   from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side, "
        "GradientFill, numbers)\n"
        "   from openpyxl.utils import get_column_letter\n"
        "   import pandas as pd\n"
        "   import numpy as np\n"
        "   import os, uuid\n"
        "2. For PowerPoint scripts (python-pptx) always import:\n"
        "   from pptx import Presentation\n"
        "   from pptx.util import Inches, Pt, Emu\n"
        "   from pptx.dml.color import RGBColor\n"
        "   from pptx.enum.text import PP_ALIGN\n"
        "3. For Word documents always import:\n"
        "   from docx import Document\n"
        "   from docx.shared import Pt, Inches, RGBColor\n"
        "4. For PDF (reportlab) always import:\n"
        "   from reportlab.lib.pagesizes import letter, A4\n"
        "   from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph\n"
        "   from reportlab.lib.styles import getSampleStyleSheet\n"
        "   from reportlab.lib import colors\n"
        "5. Save the deliverable ONLY in /tmp/outputs/ (inputs are read-only in /tmp/inputs).\n"
        "6. Do NOT use deprecated numpy functions: numpy.irr, numpy.npv, numpy.pv. "
        "Implement financial math manually using pure Python loops if needed.\n"
        "7. Always use import random if generating random data.\n"
        "8. Double-check every class or function name you use exists in the library "
        "before using it.\n"
        "9. For Excel: NEVER reference a variable before it is defined in the script. Every style "
        "variable (e.g. header_fill, header_font, thin_border) MUST be assigned with a "
        "full definition (e.g. header_fill = PatternFill(...)) BEFORE it is applied to "
        "any cell. Do not use a variable name unless you explicitly assigned it in the "
        "same script. If you apply the same style to many cells, define the style object "
        "once at the top of the script, then reuse it.\n"
        "10. For Excel: If you use custom styles like navy_fill or navy_font, you MUST define them: "
        "navy_fill = PatternFill(fill_type='solid', fgColor='17365D') "
        "navy_font = Font(name='Arial', size=10, bold=True, color='FFFFFF').\n"
        "11. PREVENT TEXT OVERFLOW: Ensure all text fits within its container (shapes, tables, document margins). "
        "For PPTX shapes, you MUST enable word wrap and auto-size to fit text by importing `from pptx.enum.text import MSO_AUTO_SIZE` "
        "and setting `shape.text_frame.word_wrap = True` AND `shape.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE`. "
        "Adjust widths/heights so that text does not overflow out of bounds."
    ),
    "financial_analysis": (
        "Structure the response as: (1) a direct, structured answer with headings/"
        "bullets/examples, then (2) a separate 'Suggestions / Next Steps' section with "
        "actionable recommendations, risks, and best practices, not repeating section 1. "
        "Label all assumptions explicitly. Distinguish between document-grounded figures "
        "and general estimates. "
        "If the source document text appears garbled or layout-broken (e.g., from PDF "
        "extraction), RECONSTRUCT and REFORMAT the financial data cleanly — never "
        "copy broken raw text verbatim."
    ),
    "general": (
        "Answer directly and concisely. Use Markdown formatting (bullets, bold, headings) "
        "to improve readability. If document context is provided and the raw text appears "
        "garbled or layout-broken, reconstruct and reformat the content clearly before "
        "answering — never output raw broken text verbatim."
    ),
}


def build_system_prompt(intent: IntentLabel, rag_insufficient: bool = False) -> str:
    """Compose the final system prompt = CORE + intent addendum + optional RAG warning."""
    prompt = CORE_SYSTEM_PROMPT + "\n\n" + INTENT_PROMPTS[intent]
    if rag_insufficient:
        prompt += RAG_INSUFFICIENT_PROMPT
    return prompt


# ---------------------------------------------------------------------------
# Intent classifier
# ---------------------------------------------------------------------------

# Classifier system prompt — kept minimal; the model returns a single word.
_CLASSIFIER_SYSTEM = (
    "You are a strict intent classifier and memory router. Given the user's latest message, recent chat history, "
    "and saved User Context Memory, perform two tasks:\n"
    "1. Classify the intent of the user's LATEST message into EXACTLY ONE of these words: code | document_generation | financial_analysis | general\n"
    "2. Extract ONLY the parts of the User Context Memory that are directly relevant to the user's LATEST request. "
    "If none are relevant, return an empty string.\n\n"
    "Rules for intent (the user's LATEST message takes absolute precedence):\n"
    "- document_generation: ONLY select this when the user's LATEST message explicitly asks to create, generate, produce, build, or download/modify a file "
    "(e.g., Excel workbook, PowerPoint presentation, Word document, PDF, spreadsheet, report). "
    "CRITICAL CONSTRAINTS:\n"
    "  * If the user says 'hi', 'hello', 'hey', 'thanks', or any greeting, it is ALWAYS 'general'. NEVER 'document_generation'.\n"
    "  * If the user says 'explain this', 'what is this', 'summarize', 'help me understand', or asks questions about an uploaded image/document without requesting a new file, it is ALWAYS 'general' (or 'financial_analysis' if a finance question). NEVER 'document_generation'!\n"
    "  * Prior document generation in recent chat history MUST NOT cause a new greeting, question, or image explanation to become document_generation.\n"
    "- code: user wants working programming code, a script, or a debugging/programming solution as text.\n"
    "- financial_analysis: user asks about finance, valuation, LBO, DCF, metrics, KPIs — as text/explanation, not a downloadable file.\n"
    "- general: greetings ('hi', 'hello'), asking to explain or describe an uploaded image/screenshot, general Q&A, or anything where no file is requested.\n\n"
    "Rules for memory:\n"
    "- Return only the relevant lines/topics if they directly apply to the CURRENT user message. If none are relevant or the message is a greeting/general question, return an empty string.\n\n"
    "Output Format:\n"
    "You MUST return a valid JSON object with exactly two keys: 'intent' and 'relevant_memory'.\n"
    "Example: {\"intent\": \"general\", \"relevant_memory\": \"\"}"
)

def classify_intent(
    user_message: str,
    recent_history: list[dict] | None = None,
    context_memory: str = "",
    *,
    api_key: str | None = None,
    classifier_model: str = "gpt-4o-mini",
) -> tuple[IntentLabel, str]:
    """
    Classify the user's intent and extract relevant memory using a cheap, fast model call.

    Returns:
        (intent_label, relevant_memory_string)
    """
    clean_msg = (user_message or "").strip().lower().rstrip(".,;:!?")
 
    # Fast heuristics for obvious greetings
    if clean_msg in {"hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening", "thanks", "thank you"}:
        return "general", ""
 
    # Fast heuristics for pure explanation/inspection requests
    if clean_msg in {"explain this", "explain", "what is this", "what's this", "tell me about this", "describe this", "what does this mean", "summarize", "summarize this"}:
        return "general", ""
    
    from django.conf import settings as django_settings
    from openai import OpenAI
    import json
 
    try:
        key = api_key or getattr(django_settings, 'OPENAI_API_KEY', None)
        client = OpenAI(api_key=key)

        messages: list[dict] = [{"role": "system", "content": _CLASSIFIER_SYSTEM}]

        # Include last 3 turns of history for context (keep payload tiny)
        if recent_history:
            for m in recent_history[-3:]:
                messages.append({"role": m["role"], "content": (m.get("content") or "")[:300]})

        user_content = f"User Message:\n{user_message[:500]}\n\nUser Context Memory:\n{context_memory}"
        messages.append({"role": "user", "content": user_content})

        resp = client.chat.completions.create(
            model=classifier_model,
            messages=messages,
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        
        raw_intent = str(parsed.get("intent", "general")).strip().lower()
        label = raw_intent.split()[0].rstrip(".,;:!?") if raw_intent.split() else "general"
        if label not in VALID_INTENTS:
            label = "general"
            
        relevant_memory = str(parsed.get("relevant_memory", "")).strip()

        logger.debug("Intent classified as '%s' for message: %.60s", label, user_message)
        return label, relevant_memory  # type: ignore[return-value]

    except Exception as exc:
        logger.error("Intent classification failed (%s); defaulting to 'general'.", exc)
        return "general", context_memory


def get_intent_max_tokens(intent: IntentLabel, user_max_tokens: int) -> int:
    """
    Return the effective token budget for this intent.
    The intent budget is the minimum NEEDED; the user setting can raise it but
    not lower it below the intent floor.
    """
    intent_floor = INTENT_MAX_TOKENS.get(intent, 2000)
    return max(intent_floor, user_max_tokens)
