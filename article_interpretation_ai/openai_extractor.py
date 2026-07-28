
"""
articles_app/openai_extractor.py
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from functools import lru_cache
from django.conf import settings as django_settings

# ─────────────────────────────────────────────────────────────────────────────
# Prompts — copied verbatim from extract_articles.py
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTIONS = """
You are extracting financing/security rights from Articles of Incorporation, Certificate of Incorporation, Charter, Amended and Restated Certificate, Certificate of Designation, or similar company constitutional documents.

Your job is to extract all securities/classes/series and return structured JSON only, matching the supplied schema. Do not include narrative text outside the JSON.

CORE PRINCIPLES
1. Extract only what is supported by the source document.
2. Do not guess.
3. Do not assume standard venture terms unless the document states them.
4. Do not assume all Preferred Stock series have the same rights unless the document expressly says so.
5. Every class/series must be evaluated separately.
6. If a value is not stated, use "Not stated".
7. If a field does not apply, use "N/A".
8. If the source is unclear, use "Needs review".
9. Every extracted value must have supporting source text or section reference where available.
10. Use High confidence only where the value is directly supported by the document or calculated from directly stated source values.

FIELDS TO EXTRACT FOR EACH SECURITY
For every security/class/series, extract these schema fields:
- security_name
- security_type
- authorized_count
- oip_original_issue_price
- liquidation_preference
- dividend_paying
- dividend_type
- dividend_rate
- conversion_ratio
- conversion_price
- participation_rights
- participation_cap
- seniority
- senior_to
- pari_passu_with
- junior_to
- applies_to
- source_basis
- confidence
- source_section
- source_text
- review_flags

SECURITY NAME RULES
Identify every actual security/class/series mentioned in the document, including Common Stock, Class A Common Stock, Class B Common Stock, Series Seed Preferred Stock, Series A Preferred Stock, Series B Preferred Stock, Series C Preferred Stock, Series D Preferred Stock, Series E Preferred Stock, Series F Preferred Stock, Series G Preferred Stock, Series H Preferred Stock, Series I Preferred Stock, Series J Preferred Stock, and any other named class or series.

Create one row/object per actual security/class/series.
Do not include aggregate placeholder rows such as "Preferred Stock" if individual series rows are already extracted and the aggregate row does not have independent rights.
If the document only describes Preferred Stock in aggregate and does not provide individual series terms, then include Preferred Stock as a row.

AUTHORIZED COUNT RULES
Extract authorized shares only.
Look for: authorized to issue, total number of shares, number of authorized shares, shares of capital stock, shares are hereby designated, designated as, consisting of, shall consist of.
Do not confuse authorized count with issued shares, outstanding shares, reserved shares, as-converted shares, shares issuable upon conversion, option pool shares, or warrant shares.

ISSUE / STRIKE PRICE / OIP RULES
For oip_original_issue_price, extract the security's Original Issue Price or equivalent issue price per share.
Prefer: Original Issue Price, Original Issuance Price, Original Purchase Price, Initial Issue Price, Issue Price, Purchase Price, Subscription Price, price per share, per share purchase price, stated value if clearly used as issue price, Series A Original Issue Price, Series B Original Issue Price, or definitions like "Original Issue Price shall mean $...".
If Original Issue Price is not directly stated, check whether liquidation preference or conversion clauses define it.
If the initial conversion price equals the Original Issue Price, use it only if the source clearly supports that relationship and add review flag "OIP inferred from initial conversion price".
If a document lists multiple per-series original issue prices in one clause/table, map each amount to the correct series.
Do not use option strike price, warrant exercise price, FMV, fair market value, public offering price, par value, common stock price, or exercise price unless clearly tied to the security's original issue/purchase price.
Keep OIP concise, preferably a per-share dollar amount such as "$0.246" or a concise formula.

LIQUIDATION PREFERENCE RULES
Extract liquidation preference per share or formula.
Look for: Liquidation Preference, Original Issue Price plus declared but unpaid dividends, prior and in preference, before any distribution, liquidation/dissolution/winding up, Deemed Liquidation Event, merger/consolidation/sale of assets, amount per share, times the Original Issue Price, 1x, 2x, 3x, plus accrued dividends, plus declared and unpaid dividends.
If liquidation preference is stated as a formula, keep the formula.
If liquidation preference equals OIP, state that.
If liquidation preference includes dividends, mention it.
If not applicable to Common Stock, use "N/A".

SENIORITY RULES
Determine ranking in liquidation preference.
Use source clauses such as rank senior to, rank junior to, rank pari passu with, on parity with, prior and in preference to, before any payment, first/second/thereafter, after payment in full, subject to prior rights.
For seniority, keep a concise classification or ranking explanation in the detailed JSON; the script will convert this to numeric ranking in the Cap Table Summary.
If multiple preferred series are pari passu, state pari passu and identify the relevant series in pari_passu_with.
If one series is paid before another, identify senior_to and junior_to accurately.
Common Stock should generally rank after Preferred Stock only if supported by liquidation waterfall or preference language.

PARTICIPATION RIGHTS RULES
Classify participation_rights as one of: Full Participation, Capped Participation, Non-Participating, Residual common participation, Not stated, N/A, or Needs review.
If holders receive liquidation preference and then participate with Common Stock in remaining proceeds, classify as Full Participation unless capped.
If holders participate only until receiving a maximum/cap amount, classify as Capped Participation.
If holders receive the greater of liquidation preference or as-converted amount, classify as Non-Participating.
If holders receive liquidation preference and no further distribution, classify as Non-Participating.
For Common Stock, participation rights are usually "N/A" unless the document states a special participation right.

PARTICIPATION CAP RULES
Extract cap per share or cap formula in participation_cap.
Look for: cap, capped at, maximum amount, until such holder has received, aggregate amount equal to, participation cap, 2x Original Issue Price, 3x Original Issue Price, including dividends, excluding dividends.
If no cap exists and participation is full, use "N/A" or "Not applicable".
If the security is non-participating, use "N/A".
If cap expressly includes dividends, include that fact in participation_cap or liquidation_preference/source_text so the script can populate "Cap price Includes Dividends".
If cap expressly excludes dividends or only refers to issue price/share amount, include that fact in participation_cap/source_text.
Do not guess whether cap includes dividends.

CONVERSION PRICE AND CONVERSION RATIO RULES
Extract conversion_price and conversion_ratio.
Look for: conversion price, initial conversion price, conversion ratio, convertible into Common Stock, Original Issue Price divided by Conversion Price, each share shall be convertible into, as-converted basis.
For conversion_ratio, use concise ratio format where possible: 1:1, 1:2, 2:1.
If OIP equals conversion price, conversion ratio is usually 1:1, but only calculate it if both values are available and the formula supports it.
If the ratio cannot be calculated but formula is stated, use "Formula stated" and describe the formula briefly.
Do not use long explanatory sentences in conversion_ratio.

DIVIDEND RULES
Extract dividend_paying, dividend_rate, and dividend_type.
For dividend_paying use: Yes, No, When declared, Not stated, or N/A.
For dividend_rate use percentage only where possible, such as 8% or 6%. Do not put dollar amounts or long sentences in dividend_rate. If the clause says a dollar dividend amount and also provides OIP or a percentage rate, compute or extract the percentage and return only the percent.
For dividend_type, explicitly classify whether the dividend is Simple or Compounding where the source says simple interest, simple dividends, compounded, compounding, or compounded annually/quarterly/monthly. Otherwise use Cumulative, Non-cumulative, When-and-if-declared, Not stated, or N/A.
Do not assume dividends are cumulative unless the document says cumulative, accrue, accumulated, or similar. Do not assume Simple or Compounding unless the source states it.
For Common Stock, dividend fields should usually be N/A unless special common dividend rights are expressly stated.

COMMON STOCK RULES
For securities classified as Common Stock:
- security_type should identify Common Stock or the relevant class.
- liquidation_preference is usually N/A unless special rights are stated.
- participation_rights is usually N/A unless special rights are stated.
- dividend_paying, dividend_rate, and dividend_type are usually N/A unless special rights are stated.
- seniority should rank after Preferred Stock only if supported by liquidation waterfall/preference language.

VALIDATION AND QUALITY CHECK RULES
Before finalizing the JSON, perform a source-based validation check for each security/class/series.
For every security row, confirm that the following fields were searched and evaluated:
1. Security Name
2. Security Type
3. Authorized Count
4. Original Issue Price / Issue Price / Strike Price
5. Liquidation Preference per Share
6. Seniority in Liquidation Preference
7. Participation Rights
8. Participation Cap per Share
9. Conversion Price
10. Conversion Ratio
11. Dividend Payable
12. Dividend Rate
13. Dividend Type
14. Whether cap price includes dividends

If a required field is missing from the source, do not leave it silently blank. Use "Not stated" and add review flag "Missing from source".
If a field is ambiguous, use "Needs review" and add review flag "Ambiguous source text".
If a field is inferred from a clause, mark source_basis as "Inferred from source clause".
If a value is calculated, mark source_basis as "Calculated from source values".
If a value has no supporting source text, do not mark it High confidence.

FINAL COMPLETENESS CHECK
For each security row, internally verify:
- Did we identify source text for Security Name?
- Did we search for Authorized Count?
- Did we search for OIP / Issue Price?
- Did we search for Liquidation Preference?
- Did we search for Seniority?
- Did we search for Participation Rights?
- Did we search for Conversion Price and Ratio?
- Did we search for Dividend Payable, Rate, and Type?
- Did we search whether cap price includes dividends?
- Are any fields missing?
- Are any values inferred?
- Are any values unsupported?
- Are any aggregate placeholder rows that should be removed?

Only output the structured JSON after this validation check.

CONFIDENCE RULES
Use High confidence only when the value is directly stated in the source or calculated from directly stated source values.
Use Medium confidence when the value is inferred from a liquidation waterfall, ranking clause, or formula.
Use Low confidence when source text is incomplete, ambiguous, OCR quality is poor, or manual review is required.

SOURCE RULES
source_basis should be one of: Explicit clause, Definition, Capitalization clause, Liquidation waterfall, Dividend clause, Conversion clause, Participation clause, Ranking clause, Protective provision only, Inferred from source clause, Calculated from source values, Not stated, Needs review.
source_text should be a short supporting excerpt. Do not provide very long passages.
review_flags should mention Missing from source, Ambiguous source text, Inferred, Calculated, OCR concern, or Needs manual review where applicable.

OUTPUT RULES
Return structured JSON only.
Use one row per actual security/class/series.
Do not include unsupported values.
Do not include placeholder aggregate Preferred Stock rows if individual preferred series rows are already shown.
Always preserve source_text and source_section in the detailed extraction.
""".strip()

QUALITY_REVIEW_INSTRUCTIONS = (INSTRUCTIONS + """

QUALITY REVIEW PASS - OIP AND SENIORITY FOCUS
You are now performing a targeted quality review of a prior extraction.
Your goal is to correct only weak or missing values, especially OIP / Issue Strike Price and Seniority in Liquidation Preference.

Critical OIP checks:
- Search definitions, capitalization tables, liquidation preference clauses, conversion clauses, and anti-dilution clauses.
- Map each amount to the exact series. Do not copy one series price to another unless the source states they are the same.
- Prefer explicit phrases: Original Issue Price, Original Issuance Price, Series A Original Issue Price, Original Purchase Price, Initial Issue Price, Purchase Price, Issue Price, per share purchase price.
- If OIP is not stated but conversion price equals OIP based on an explicit formula, use it and set source_basis to Calculated from source values or Inferred from source clause.
- Do not use par value, common stock price, option strike price, warrant exercise price, FMV, or public offering price as OIP unless expressly tied to the share issuance price.

Critical seniority checks:
- Search explicit ranking language and the liquidation waterfall.
- Convert payment order into numeric/liquidation ranking logic in the seniority field when possible.
- First paid = most senior. Second paid = next rank. Thereafter/residual common = lower rank.
- Pari passu securities share the same rank.
- If Series J is paid first and Series A-I are paid second, Series J is senior to Series A-I and Series A-I are pari passu with each other.
- Common Stock should be junior only when supported by the liquidation waterfall or prior/preference language.

Validation requirements:
- Return the full corrected JSON using the same schema, not just patches.
- Preserve all correct values from the prior extraction.
- Replace unsupported OIP or seniority values with Not stated or Needs review.
- Add Review Flags for any OIP or Seniority value still missing, ambiguous, inferred, calculated, or based on OCR-quality text.
- Every corrected OIP and seniority value must include source_text/source_section support.
""").strip()

SENIORITY_WATERFALL_INSTRUCTIONS = """
You are performing a seniority-only legal extraction from Articles / Charter documents.

Your only task is to determine liquidation preference payment order and return exact numeric ranks for each expected security.

Use the uploaded PDF/DOCX like a human reviewer would. If the PDF is scanned or image-based, read the visible page images. Focus on the liquidation preference / liquidation, dissolution or winding up / deemed liquidation event waterfall.

Return strict JSON matching the schema only.

Critical output rule:
- Fill security_seniority_ranks with one item for EVERY security in the provided expected security list.
- Use the EXACT security_name from the provided list. Do not invent or shorten names.
- Also fill liquidation_seniority_tiers as a backup explanation.

Ranking rules:
1. Rank 1 = most senior liquidation preference tier.
2. Same-tier / pari passu securities must share the same rank.
3. Later-paid securities get larger rank numbers.
4. Common Stock should be ranked after the preferred liquidation preference tiers when it receives residual/remaining assets after preferred liquidation preferences.
5. Do not flatten all Preferred Stock into one rank if individual series are paid in different waterfall steps.
6. Treat split series separately, e.g. Series A-1, Series A-2, Series A-3 may have different ranks.
7. Use payment order language over generic ranking language.
8. Priority source language includes: first, second, third, fourth, fifth, thereafter, after payment in full, prior and in preference, before any distribution, rank senior, rank junior, pari passu, on parity.
9. If the document contains a waterfall like Series D first, Series C second, Series B third, Series A-3 fourth, Series A-1/A-2 fifth, return exactly:
   Series D rank 1; Series C rank 2; Series B rank 3; Series A-3 rank 4; Series A-1 rank 5; Series A-2 rank 5; Common Stock rank 6.
10. If a security is not part of liquidation seniority, use rank 999 and source_basis "Needs review".

Important name-handling rules:
- Do not return combined securities such as "Series A-1 and Series A-2 Preferred Stock" in security_seniority_ranks. Return two items using exact names from the expected list.
- Do not return aggregate names such as "Series A Preferred Stock" when the expected list contains Series A-1, Series A-2, and Series A-3. Use exact split series names from the expected list.
- If the liquidation clause says "Series A-1 and Series A-2" and the expected list contains "Series A-1 Preferred Stock" and "Series A-2 Preferred Stock", assign the same rank to both exact names.
""".strip()

# ─────────────────────────────────────────────────────────────────────────────
# JSON schemas
# ─────────────────────────────────────────────────────────────────────────────

EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name":  {"type": "string"},
        "document_name": {"type": "string"},
        "securities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "security_name":            {"type": "string"},
                    "security_type":            {"type": "string"},
                    "authorized_count":         {"type": "string"},
                    "oip_original_issue_price": {"type": "string"},
                    "liquidation_preference":   {"type": "string"},
                    "dividend_paying":          {"type": "string"},
                    "dividend_type":            {"type": "string"},
                    "dividend_rate":            {"type": "string"},
                    "conversion_ratio":         {"type": "string"},
                    "conversion_price":         {"type": "string"},
                    "participation_rights":     {"type": "string"},
                    "participation_cap":        {"type": "string"},
                    "seniority":                {"type": "string"},
                    "senior_to":                {"type": "string"},
                    "pari_passu_with":          {"type": "string"},
                    "junior_to":                {"type": "string"},
                    "applies_to":               {"type": "string"},
                    "source_basis":             {"type": "string"},
                    "confidence":               {"type": "string"},
                    "source_section":           {"type": "string"},
                    "source_text":              {"type": "string"},
                    "review_flags":             {"type": "string"},
                },
                "required": [
                    "security_name","security_type","authorized_count",
                    "oip_original_issue_price","liquidation_preference",
                    "dividend_paying","dividend_type","dividend_rate",
                    "conversion_ratio","conversion_price","participation_rights",
                    "participation_cap","seniority","senior_to","pari_passu_with",
                    "junior_to","applies_to","source_basis","confidence",
                    "source_section","source_text","review_flags",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company_name","document_name","securities"],
    "additionalProperties": False,
}


SENIORITY_TIER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_name": {"type": "string"},
        "security_seniority_ranks": {
            "type": "array",
            "description": "One row per expected security from the provided list. Use exact security names from that list. Rank 1 is most senior; same-tier securities share the same rank; use 999 if not determinable.",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "rank": {"type": "integer"},
                    "source_basis": {"type": "string"},
                    "source_text": {"type": "string"},
                },
                "required": ["security_name", "rank", "source_basis", "source_text"],
                "additionalProperties": False,
            },
        },
        "liquidation_seniority_tiers": {
            "type": "array",
            "description": "Optional tier backup. Ordered liquidation preference tiers. Rank 1 is most senior. Same-tier securities share the same rank.",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "securities": {"type": "array", "items": {"type": "string"}},
                    "source_basis": {"type": "string"},
                    "source_text": {"type": "string"},
                },
                "required": ["rank", "securities", "source_basis", "source_text"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["company_name", "document_name", "security_seniority_ranks", "liquidation_seniority_tiers", "notes"],
    "additionalProperties": False,
}


OIP_PRICE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_name": {"type": "string"},
        "security_oip_prices": {
            "type": "array",
            "description": "Exact source-derived OIP / original issue price per expected security.",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "oip_original_issue_price": {"type": "string"},
                    "source_basis": {"type": "string"},
                    "source_text": {"type": "string"},
                },
                "required": ["security_name", "oip_original_issue_price", "source_basis", "source_text"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["company_name", "document_name", "security_oip_prices", "notes"],
    "additionalProperties": False,
}


def _oip_text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "exact_oip_source_prices",
            "schema": OIP_PRICE_SCHEMA,
            "strict": True,
        }
    }


OIP_PRICE_REVIEW_INSTRUCTIONS = """
You are performing an exact OIP / Original Issue Price validation from Articles / Charter documents.

Your only task is to read the uploaded source document and return the exact Original Issue Price / Original Issuance Price / Original Purchase Price / Issue Price for each expected security.

Return strict JSON matching the schema only.

Critical rules:
- Use the exact security names from the provided expected security list. Do not invent or shorten names.
- Focus on definitions and clauses containing Original Issue Price, Original Issuance Price, Original Purchase Price, Issue Price, Purchase Price, price per share, or initial conversion price if it explicitly equals OIP.
- Map each dollar amount to the correct series. Be extremely careful with decimals.
- If a clause says '$0.606174 per share for Series Seed-1 Preferred Stock and $0.484940 per share for Series Seed-2 Preferred Stock', return those exact values.
- Do not use par value, public offering price, option exercise price, warrant price, common stock price, or fair market value as OIP.
- If no OIP is stated for a security, return 'Not stated'.
- If the security is Common Stock, return 'N/A' unless the document clearly states an issue price.
- Preserve a short source_text excerpt containing the exact price and security name.
""".strip()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "articles_charter_terms_extraction",
            "schema": EXTRACTION_SCHEMA,
            "strict": True,
        }
    }


def _seniority_text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "liquidation_seniority_exact_ranks",
            "schema": SENIORITY_TIER_SCHEMA,
            "strict": True,
        }
    }


def _response_to_json(response: Any) -> Dict[str, Any]:
    raw = getattr(response, "output_text", None)
    if not raw:
        try:
            raw = response.output[0].content[0].text
        except Exception:
            raise RuntimeError("Could not read model output from OpenAI response.")
    return json.loads(raw)


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _safe_upload_filename(file_name: str) -> str:
    """The OpenAI file API can reject uppercase extensions like .PDF even
    though .pdf is supported. Lowercase just the extension, not the stem."""
    p = Path(file_name)
    suffix = p.suffix
    if suffix and suffix != suffix.lower():
        return p.stem + suffix.lower()
    return file_name


def _extract_pdf_text(file_bytes: bytes) -> str:
    from PyPDF2 import PdfReader
    import io
    reader = PdfReader(io.BytesIO(file_bytes))
    parts = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            parts.append(f"\n\n--- Page {i} ---\n{t}")
    return _normalize_whitespace("\n".join(parts))


def _extract_docx_text(file_bytes: bytes) -> str:
    import docx, io
    doc = docx.Document(io.BytesIO(file_bytes))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for idx, table in enumerate(doc.tables, 1):
        parts.append(f"\n--- Table {idx} ---")
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return _normalize_whitespace("\n".join(parts))


def _value_is_weak(value: Any) -> bool:
    t = str(value or "").strip().lower()
    return t in {"", "not stated", "n/a", "na", "none", "null", "needs review"} or "needs review" in t


def _needs_quality_review(data: Dict[str, Any]) -> bool:
    for sec in (data.get("securities") or []):
        name = str(sec.get("security_name", "")).lower()
        is_pref = "preferred" in name or re.search(r"series\s+[a-z0-9]+", name)
        if is_pref and _value_is_weak(sec.get("oip_original_issue_price")):
            return True
        if _value_is_weak(sec.get("seniority")):
            return True
    return False

def _normalize_security_name_for_match(value: str) -> str:
    t = str(value or "").lower()
    t = re.sub(r"preferred\s+stock|preferred\s+shares|shares|stock|inc\.|inc|corp\.|corporation", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _match_tier_security_to_existing(tier_name: str, existing_names: List[str]) -> Optional[str]:
    """Match a tier security name returned by the model to an existing extracted row name."""
    tn = _normalize_security_name_for_match(tier_name)
    if not tn:
        return None
    best = None
    best_score = 0
    for existing in existing_names:
        en = _normalize_security_name_for_match(existing)
        if not en:
            continue
        score = 0
        if tn == en:
            score = 100
        elif tn in en or en in tn:
            score = 80
        else:
            tset = set(tn.split())
            eset = set(en.split())
            if tset and eset:
                score = int(100 * len(tset & eset) / max(len(tset), len(eset)))
        if score > best_score:
            best_score = score
            best = existing
    return best if best_score >= 60 else None


def _series_token_from_name(value: str) -> str:
    """Return compact series token like seriesa1, seriesd, common from a security name."""
    v = str(value or '').lower()
    if 'common' in v and 'preferred' not in v:
        return 'common'
    m = re.search(r'\bseries\s+([a-z]+)(?:\s*[-–—]?\s*(\d+))?', v)
    if not m:
        return ''
    letter = m.group(1)
    num = m.group(2) or ''
    return f'series{letter}{num}'


def _match_tier_security_to_all_existing(tier_name: str, existing_names: List[str]) -> List[str]:
    """Return all existing row names represented by a tier string.

    Handles combined names such as:
    - "Series A-1 and Series A-2 Preferred Stock"
    - "Series A-1/A-2 Preferred Stock"
    - "Series A-1 through Series A-3 Preferred Stock"
    A single best-row match would miss same-tier securities in these cases.
    """
    raw_name = str(tier_name or '')
    low = raw_name.lower()
    existing_by_token = {_series_token_from_name(n): n for n in existing_names if _series_token_from_name(n)}
    found: List[str] = []

    def add_by_token(tok: str):
        if tok in existing_by_token and existing_by_token[tok] not in found:
            found.append(existing_by_token[tok])

    # Common stock.
    if 'common' in low:
        add_by_token('common')

    # Exact Series X or Series X-1 mentions.
    for m in re.finditer(r'\bseries\s+([a-z]+)(?:\s*[-–—]?\s*(\d+))?', low):
        add_by_token(f"series{m.group(1)}{m.group(2) or ''}")

    # Shorthand continuation: "Series A-1 and A-2" / "Series A-1/A-2".
    m = re.search(r'\bseries\s+([a-z]+)\s*[-–—]?\s*(\d+)', low)
    if m:
        letter = m.group(1)
        first_num = int(m.group(2))
        add_by_token(f'series{letter}{first_num}')
        for nm in re.finditer(r'(?:and|or|/|,)\s*([a-z])?\s*[-–—]?\s*(\d+)\b', low):
            next_letter = nm.group(1) or letter
            add_by_token(f"series{next_letter}{nm.group(2)}")
        # Ranges: Series A-1 through Series A-3.
        rng = re.search(r'\bseries\s+([a-z]+)\s*[-–—]?\s*(\d+)\s*(?:through|thru|to|-)\s*(?:series\s+)?(?:[a-z]+\s*[-–—]?\s*)?(\d+)\b', low)
        if rng:
            rletter = rng.group(1)
            start = int(rng.group(2)); end = int(rng.group(3))
            if start <= end and end - start <= 20:
                for i in range(start, end + 1):
                    add_by_token(f'series{rletter}{i}')

    # Ranges: Series A through Series I.
    rng2 = re.search(r'\bseries\s+([a-z])\s*(?:through|thru|to)\s*(?:series\s+)?([a-z])\b', low)
    if rng2:
        a = ord(rng2.group(1)); b = ord(rng2.group(2))
        if a <= b and b - a <= 26:
            for c in range(a, b + 1):
                add_by_token(f'series{chr(c)}')

    # If the model returned an aggregate name like "Series A Preferred Stock", map only if there is
    # a single matching Series A row. If A-1/A-2/A-3 rows exist, do not flatten them.
    if not found:
        best = _match_tier_security_to_existing(raw_name, existing_names)
        if best:
            btok = _series_token_from_name(best)
            # Avoid using aggregate Series A for multiple split series.
            if re.search(r'\bseries\s+[a-z]+\b', low) and not re.search(r'\d', low):
                prefix = btok.rstrip('0123456789')
                split_matches = [n for tok, n in existing_by_token.items() if tok.startswith(prefix) and tok != prefix]
                if len(split_matches) > 1:
                    return []
            found.append(best)

    return found

def _apply_oip_prices_to_data(data: Dict[str, Any], oip_json: Dict[str, Any]) -> Dict[str, Any]:
    """Apply exact OIP prices returned by the dedicated source-document OIP pass."""
    if not data or not oip_json:
        return data
    data["_oip_review_json"] = oip_json
    securities = data.get("securities", []) or []
    existing_names = [str(sec.get("security_name", "")) for sec in securities]

    price_map: Dict[str, Dict[str, str]] = {}
    for row in oip_json.get("security_oip_prices", []) or []:
        sec_name = str(row.get("security_name", "")).strip()
        price_text = str(row.get("oip_original_issue_price", "")).strip()
        if not sec_name or not price_text:
            continue
        matched_names = _match_tier_security_to_all_existing(sec_name, existing_names) or [sec_name]
        for matched in matched_names:
            price_map[matched] = {
                "price": price_text,
                "basis": str(row.get("source_basis", "Dedicated exact OIP extraction")),
                "source_text": str(row.get("source_text", "")),
            }

    applied = 0
    for sec in securities:
        name = str(sec.get("security_name", "")).strip()
        if not name or "preferred" not in name.lower():
            continue
        info = price_map.get(name)
        if not info:
            continue
        price_text = info.get("price", "")
        price_num = _money_to_float(price_text)
        if price_num is None:
            continue
        corrected = _fmt_money_num(price_num)
        current = _money_to_float(sec.get("oip_original_issue_price"))
        if current is None or abs(current - price_num) > max(0.0000005, abs(price_num) * 0.0005):
            sec["oip_original_issue_price"] = corrected
            conv = _money_to_float(sec.get("conversion_price"))
            if conv is None or current is None or abs(conv - current) <= max(0.0000005, abs(current or 0) * 0.0005):
                sec["conversion_price"] = corrected
            old_source = str(sec.get("source_text", ""))
            if info.get("source_text") and info["source_text"] not in old_source:
                sec["source_text"] = (old_source + "\n\nOIP EXACT-PRICE SOURCE: " + info["source_text"]).strip()
            flags = str(sec.get("review_flags", ""))
            note = "OIP corrected by dedicated source-document price validation"
            if note not in flags:
                sec["review_flags"] = (flags + "; " + note).strip("; ").strip()
            applied += 1
    data["_oip_review_applied_count"] = applied
    return data


def _apply_seniority_tiers_to_data(data: Dict[str, Any], tiers_json: Dict[str, Any]) -> Dict[str, Any]:
    """Overwrite extracted seniority fields using a dedicated seniority exact-rank JSON result.

    Prioritizes `security_seniority_ranks`, which is keyed to the exact securities
    already extracted. Falls back to `liquidation_seniority_tiers` + fuzzy name
    matching only when exact ranks are not present.
    """
    if not data or not tiers_json:
        return data

    # Keep the raw seniority response inside data so it can be inspected/debugged later.
    data["_seniority_review_json"] = tiers_json

    securities = data.get("securities", []) or []
    existing_names = [str(sec.get("security_name", "")) for sec in securities]

    exact_rank_map: Dict[str, Dict[str, str]] = {}

    # 1. Preferred path: exact security rank rows from seniority-only extraction.
    for row in tiers_json.get("security_seniority_ranks", []) or []:
        sec_name = str(row.get("security_name", "")).strip()
        if not sec_name:
            continue
        try:
            rank_int = int(row.get("rank"))
        except Exception:
            continue
        exact_rank_map[sec_name] = {
            "rank": str(rank_int),
            "basis": str(row.get("source_basis", "Dedicated exact seniority extraction")),
            "source_text": str(row.get("source_text", "")),
        }

    # 2. Fallback path: map tier names to existing names if exact ranks are not present.
    if not exact_rank_map:
        for tier in tiers_json.get("liquidation_seniority_tiers", []) or []:
            rank = tier.get("rank")
            basis = str(tier.get("source_basis", "Dedicated seniority waterfall extraction"))
            source_text = str(tier.get("source_text", ""))
            try:
                rank_int = int(rank)
            except Exception:
                continue
            for tier_sec in tier.get("securities", []) or []:
                matched_list = _match_tier_security_to_all_existing(str(tier_sec), existing_names)
                for matched in matched_list:
                    exact_rank_map[matched] = {"rank": str(rank_int), "basis": basis, "source_text": source_text}

    # 3. Apply exact ranks. Use fuzzy fallback only when model name is close but not exact.
    applied = 0
    for sec in securities:
        name = str(sec.get("security_name", "")).strip()
        info = exact_rank_map.get(name)
        if not info:
            # Very conservative fallback: map the rank row to exact existing name if it clearly matches.
            for candidate, cinfo in exact_rank_map.items():
                matches = _match_tier_security_to_all_existing(candidate, existing_names)
                if name in matches:
                    info = cinfo
                    break
        if not info:
            continue
        rank = info.get("rank", "")
        if rank and rank != "999":
            sec["seniority"] = f"Rank {rank}"
        else:
            sec["seniority"] = "Needs review"
        sec["source_basis"] = "Dedicated exact seniority extraction"
        if info.get("source_text"):
            old_source = str(sec.get("source_text", ""))
            if info["source_text"] not in old_source:
                sec["source_text"] = (old_source + "\n\nSENIORITY EXACT-RANK SOURCE: " + info["source_text"]).strip()
        flags = str(sec.get("review_flags", ""))
        marker = "Seniority verified by dedicated exact-rank pass"
        if marker not in flags:
            sec["review_flags"] = (flags + "; " + marker).strip("; ").strip()
        applied += 1

    data["_seniority_review_applied_count"] = applied
    return data


def _first_money(text: Any) -> str:
    text = "" if text is None else str(text).strip()
    m = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", text)
    if m:
        return re.sub(r"\$\s+", "$", m.group(0))
    if re.search(r"\b(per share|issue price|purchase price|strike price|conversion price)\b", text, re.I):
        m = re.search(r"\b\d[\d,]*(?:\.\d+)?\b", text)
        if m:
            return "$" + m.group(0)
    return ""


def _money_num(value: Any) -> Optional[float]:
    money = _first_money(value)
    if not money:
        return None
    try:
        return float(money.replace("$", "").replace(",", ""))
    except Exception:
        return None


def _compute_conversion_ratio(conversion_ratio_text: Any, conversion_price: Any, oip: Any) -> str:
    text = "" if conversion_ratio_text is None else str(conversion_ratio_text).strip()
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\b", text)
    if m:
        left = float(m.group(1))
        right = float(m.group(2))
 
        if right != 0 and left != 1.0:
            n_common = left / right
        else:
            n_common = right
        # Format cleanly.
        if abs(n_common - round(n_common)) < 0.01:
            return f"1:{int(round(n_common))}"
        return f"1:{n_common:.4f}".rstrip("0").rstrip(".")
    cp = _money_num(conversion_price)
    o = _money_num(oip)
    if cp and o and abs(cp - o) < 1e-9:
        return "1:1"
    if cp and o and cp != 0:
        ratio = o / cp
        if abs(ratio - round(ratio)) < 0.01:
            return f"1:{int(round(ratio))}"
        return f"1:{ratio:.4f}".rstrip("0").rstrip(".")
    return text

def _money_to_float(value: Any) -> Optional[float]:
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", str(value or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _fmt_money_num(value: float, decimals: int = 9) -> str:
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "$" + (text if text else "0")


def _series_markers(name: Any) -> List[str]:
    t = str(name or "").lower()
    t = re.sub(r"preferred\s+stock|preferred\s+shares|common\s+stock|shares|stock", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    markers = []
    for m in re.finditer(r"\bseries\s+[a-z0-9]+(?:\s+[a-z0-9]+)?\b", t):
        markers.append(m.group(0))
    if "seed series" in t:
        markers.append("seed series")
    if t and t not in markers:
        markers.append(t)
    return [m for m in markers if len(m) >= 3]


def _normalize_source_text_for_match(text: Any) -> str:
    t = str(text or "").lower()
    t = t.replace("–", "-").replace("—", "-")
    t = re.sub(r"[^a-z0-9.$%]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _nearest_oip_from_source(security_name: Any, text_blob: str) -> Optional[float]:
    low = _normalize_source_text_for_match(text_blob)
    if not low:
        return None
    markers = _series_markers(security_name)
    best: Optional[Tuple[int, float]] = None
    for marker in markers:
        marker = _normalize_source_text_for_match(marker)
        if not marker:
            continue
        for m in re.finditer(re.escape(marker), low):
            start = max(0, m.start() - 420)
            end = min(len(low), m.end() + 420)
            window = low[start:end]
            if not re.search(r"original\s+(issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|price\s+per\s+share|per\s+share", window, re.I):
                continue
            for money in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", window):
                try:
                    value = float(money.group(1).replace(",", ""))
                except Exception:
                    continue
                marker_pos = m.start() - start
                dist = abs(money.start() - marker_pos)
                if best is None or dist < best[0]:
                    best = (dist, value)
    return None if best is None else best[1]

def _normalize_authorized_count(value: Any, missing_as: str = "NA") -> str:
    """Return only the authorized share count number, without words like 'shares'."""
    text = "" if value is None else str(value).strip()
    if not text:
        return missing_as
    if text.lower() in {"not stated", "n/a", "na", "none", "null", "needs review", "series-specific"}:
        return missing_as
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:authorized\s+)?shares?\b", text, re.I)
    if not m:
        m = re.search(r"\bshares?\b\D{0,40}(\d[\d,]*(?:\.\d+)?)", text, re.I)
    if not m:
        m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return text
    num = m.group(1) if m.lastindex else m.group(0)
    try:
        if "." in num:
            f = float(num.replace(",", ""))
            if abs(f - round(f)) < 1e-9:
                return f"{int(round(f)):,}"
            return f"{f:,.2f}".rstrip("0").rstrip(".")
        return f"{int(num.replace(',', '')):,}"
    except Exception:
        return num.replace(" shares", "").replace(" share", "").strip()
    
def _normalize_dividend_rate_percent(value: Any, context: Any = "", missing_as: str = "") -> str:
    """Return dividend rate as a percentage only where possible, e.g. 8%."""
    text = "" if value is None else str(value).strip()
    ctx = "" if context is None else str(context).strip()
    combined = f"{text} {ctx}".strip()
    if not combined:
        return missing_as
    if combined.lower() in {"not stated", "n/a", "na", "none", "null", "needs review"}:
        return missing_as
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:percent|per\s*cent|%)", combined, re.I)
    if m:
        n = float(m.group(1))
        if abs(n - round(n)) < 1e-9:
            return f"{int(round(n))}%"
        return f"{n:g}%"
    # Handle common wording: "at the rate of 8 per annum" only if context is clearly dividend-related.
    if re.search(r"dividend|dividends", combined, re.I):
        m = re.search(r"(?:rate\s+of|equal\s+to)\s+(\d+(?:\.\d+)?)\s*(?:per\s+annum|annually|annual)", combined, re.I)
        if m:
            n = float(m.group(1))
            return f"{int(n) if abs(n-round(n))<1e-9 else n:g}%"
    return "Not stated" if text.lower() == "not stated" else missing_as

def _normalize_dividend_type(value: Any, context: Any = "", missing_as: str = "NA") -> str:
    """Classify dividend type conservatively for valuation output.

    Key rules:
    - Negative wording is checked first: non-cumulative before cumulative,
      non-compounded before compounded.
    - "When declared" only, without explicit accrual/cumulative wording, is
      classified as Non-cumulative per Knowcraft presentation convention.
    """
    text = "" if value is None else str(value).strip()
    ctx = "" if context is None else str(context).strip()
    combined = f"{text} {ctx}".lower()
    if not combined.strip():
        return missing_as

    null_tokens = {"not stated", "n/a", "na", "none", "null", "needs review", ""}
    if text.lower() in null_tokens and not ctx.strip():
        return missing_as

    # Non-cumulative / no accrual signals must be first.
    non_cum_patterns = [
        r"\bnon[-\s]?cumulative\b",
        r"\bnot\s+cumulative\b",
        r"\bshall\s+not\s+be\s+cumulative\b",
        r"\bno\s+right\s+to\s+dividends?\s+shall\s+accrue\b",
        r"\bshall\s+not\s+accrue\b",
        r"\bno\s+dividends?\s+shall\s+accumulate\b",
        r"\bno\s+right\s+to\s+receive\s+dividends?\b.*\bnot\s+declared\b",
    ]
    if any(re.search(p, combined, re.I) for p in non_cum_patterns):
        return "Non-cumulative"

    has_non_compounded = bool(re.search(r"\bnon[-\s]?compounded\b|\bnot\s+compounded\b|\bnon[-\s]?compounding\b", combined, re.I))
    has_compounded = bool(re.search(r"\bcompounded\b|\bcompounding\b|\bcompound\b", combined, re.I)) and not has_non_compounded
    has_cumulative = bool(re.search(r"\bcumulative\b|\bshall\s+accrue\b|\baccrued\s+(?:and\s+)?unpaid\b|\baccumulated\s+(?:and\s+)?unpaid\b|\bdividends?\s+shall\s+accrue\b|\bwhether\s+or\s+not\s+declared\b", combined, re.I))

    if has_cumulative and has_non_compounded:
        return "Cumulative, non-compounded"
    if has_cumulative and has_compounded:
        return "Cumulative, compounded"
    if has_cumulative:
        return "Cumulative"
    if has_non_compounded:
        return "Cumulative, non-compounded"
    if has_compounded:
        return "Cumulative, compounded"
    if re.search(r"\bsimple\b", combined, re.I):
        return "Simple"

    if re.search(r"when,?\s+as\s+and\s+if\s+declared|if\s+and\s+when\s+declared|when\s+declared|when\s+and\s+if\s+declared", combined, re.I):
        return "Non-cumulative"

    if text.lower() in null_tokens:
        return missing_as
    return text
 
 
def _build_cap_table(securities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a cap-table-style summary list from already-extracted securities.
 
    Mirrors extract_articles.py's build_cap_table_summary() / "Cap Table Summary"
    sheet, but returns `List[Dict]` (for data["cap_table"]) instead of a
    pandas DataFrame written to Excel.
 
    Presentation rules (unchanged from the original script):
    - Remove the aggregate "Preferred Stock" row when series-level rows are present.
    - Common Stock: blank out preference/participation/dividend/cap-dividend
      fields; it is assigned the next liquidation rank after Preferred.
    - Seniority is numeric (1, 2, 3...) rather than a long ranking sentence.
    - Conversion Ratio is concise (e.g., 1:1, 2:1).
    - Dividend Rate is shown as a percentage where it can be computed.
    """
    if not securities:
        return []
 
    missing_tokens = {"", "nan", "not stated", "n/a", "na", "none", "null", "needs review", "series-specific"}
 
    def raw(value: Any) -> str:
        return "" if value is None else str(value).strip()
 
    def is_missing(value: Any) -> bool:
        return raw(value).lower() in missing_tokens
 
    def clean(value: Any, na: str = "NA") -> str:
        text = raw(value)
        return na if text.lower() in missing_tokens else text
 
    def price_from(value: Any) -> str:
        money = _first_money(value)
        return money if money else clean(value)
    
    def liquidation_multiplier(text: Any) -> Optional[float]:
        t = raw(text).lower().replace("–", "-").replace("—", "-")
        if not t:
            return None
        m = re.search(r"\b(\d+(?:\.\d+)?)\s*x\b", t)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        phrase_map = {
            "one and one fourth": 1.25, "one and one quarter": 1.25, "one and a quarter": 1.25,
            "one and one half": 1.5, "one and a half": 1.5,
            "two times": 2.0, "twice": 2.0, "one times": 1.0, "one time": 1.0,
        }
        for phrase, value in phrase_map.items():
            if phrase in t and re.search(r"original\s+(issue|issuance|purchase)\s+price|\bOIP\b", t, re.I):
                return value
        return None

    def liq_pref_per_share(row: Dict[str, Any]) -> str:
        name = raw(row.get("security_name"))
        if "common" in name.lower() and "preferred" not in name.lower():
            return ""
        liq = raw(row.get("liquidation_preference"))
        source = raw(row.get("source_text"))
        oip = raw(row.get("oip_original_issue_price"))
        combined = f"{liq} {source}"

        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined, re.I):
            return "Qualified Capital Balance"

        base = _money_num(oip)
        mult = liquidation_multiplier(combined)
        if base is not None and mult is not None:
            return _fmt_money_num(base * mult)
        if base is not None and re.search(r"original\s+(issue|issuance|purchase)\s+price|\bOIP\b|applicable\s+original|greater\s+of|liquidation\s+amount|liquidation\s+preference", combined, re.I):
            return _fmt_money_num(base)

        money = _first_money(liq)
        if money:
            return money
        if re.search(r"\b(1\s*x|one\s+times|100%)\b", liq, re.I) and not is_missing(oip):
            return price_from(oip)
        if re.search(r"original\s+(issue|issuance|purchase)\s+price|\bOIP\b", liq, re.I) and not is_missing(oip):
            return price_from(oip)
        return clean(liq)
 
    def participation_summary(row: Dict[str, Any]) -> str:
        """Normalize participation for the Cap Table.
 
        Check non-participating before participating, since the word
        "participating" appears inside "Non-participating". Also classify
        greater-of / deemed-conversion preferred as non-participating where
        the remaining assets go only to Common Stock.
        """
        text = raw(row.get("participation_rights"))
        combined = " ".join([
            text,
            raw(row.get("liquidation_preference")),
            raw(row.get("conversion_ratio")),
            raw(row.get("conversion_price")),
            raw(row.get("source_text")),
        ])
        low = combined.lower()
        if is_missing(text):
            return "NA"
        if any(x in low for x in ["non-participating", "non participating", "nonparticipating", "non-participating preferred"]):
            if any(x in low for x in ["greater", "deemed conversion", "as-converted", "as converted"]):
                return "Non-participating"
            return "Non-participating"
        if any(x in low for x in ["deemed conversion", "as-converted", "as converted", "greater of"]):
            if re.search(r"remaining\s+(assets|proceeds|funds).{0,120}common", low, re.I):
                return "Non-participating "
        if "capped" in low or "participation cap" in low:
            return "Capped Participation"
        if "full participation" in low:
            return "Participating"
        if re.search(r"\bparticipat(?:e|es|ing|ion)\b", low) and not re.search(r"\bnon[-\s]?participat", low):
            return "Participating"
        return clean(text)
 
    def participation_cap_value(value: Any) -> str:
        text = raw(value)
        if is_missing(text):
            return "NA"
        money = _first_money(text)
        if money:
            return money
        m = re.search(r"\b\d+(?:\.\d+)?\s*x\b", text, re.I)
        if m:
            return m.group(0)
        return clean(text)
 
    def seniority_rank_from_row(row: Dict[str, Any], fallback: Any = "NA") -> Any:
        """Use explicit Rank X already in the seniority field before recalculating."""
        text = raw(row.get("seniority"))
        m = re.search(r"\brank\s*[:#=\-]?\s*(\d{1,3})\b", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\bseniority\s*[:#=\-]?\s*(\d{1,3})\b", text, re.I)
        if m:
            return int(m.group(1))
        return fallback
 
    def dividend_payable(row: Dict[str, Any]) -> str:
        dtype = _normalize_dividend_type(
            row.get("dividend_type"),
            " ".join([raw(row.get("dividend_paying")), raw(row.get("dividend_rate")), raw(row.get("source_text"))]),
            missing_as="NA",
        )
        if dtype.startswith("Cumulative"):
            return "Yes"
        if dtype == "Non-cumulative":
            return "No"
        if dtype in {"NA", "N/A", "Not stated", ""}:
            return "NA"
        return "NA"
 
    def dividend_rate_pct(row: Dict[str, Any]) -> str:
        text = raw(row.get("dividend_rate"))
        context = " ".join([raw(row.get("dividend_paying")), raw(row.get("dividend_type")), raw(row.get("liquidation_preference")), raw(row.get("source_text"))])
        if is_missing(text):
            return _normalize_dividend_rate_percent("", context, missing_as="")
        pct = _normalize_dividend_rate_percent(text, context, missing_as="")
        if pct:
            return pct
        amount = _money_num(text)
        if amount is None:
            return clean(text)
        # Prefer conversion price first; if unavailable, use issue/OIP price.
        denominators: List[float] = []
        cp = _money_num(row.get("conversion_price"))
        oip = _money_num(row.get("oip_original_issue_price"))
        if cp:
            denominators.append(cp)
        if oip and (not cp or abs(oip - cp) > 1e-9):
            denominators.append(oip)
        for denom in denominators:
            if denom:
                pct = amount / denom * 100
                if 0 < pct < 100:
                    rounded = round(pct)
                    if abs(pct - rounded) <= 0.55:
                        return f"{int(rounded)}%"
                    return f"{pct:.1f}%".rstrip("0").rstrip(".") + "%"
        return clean(text)
 
    def conversion_ratio_for_cap_table(row: Dict[str, Any]) -> str:
        text = raw(row.get("conversion_ratio"))
        m = re.search(r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b", text)
        if m:
            return re.sub(r"\s+", "", m.group(0))
        cp = _money_num(row.get("conversion_price"))
        oip = _money_num(row.get("oip_original_issue_price"))
        if cp and oip and abs(cp - oip) < 1e-9:
            return "1:1"
        if cp and oip and cp != 0:
            ratio = oip / cp
            if abs(ratio - round(ratio)) < 0.01:
                return f"{int(round(ratio))}:1"
            return f"{ratio:.2f}:1"
        return clean(text)
 
    def issue_strike_price(row: Dict[str, Any]) -> str:
        """Preferred issue/strike price for the cap table. Prefer explicit OIP; then safe fallbacks."""
        combined_source = " ".join([raw(row.get("oip_original_issue_price")), raw(row.get("liquidation_preference")), raw(row.get("source_text"))])
        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined_source, re.I):
            return "Not fixed / contribution-based"
        oip_value = price_from(row.get("oip_original_issue_price"))
        if oip_value not in {"", "NA", "N/A", "Not stated", "Needs review"}:
            return oip_value
 
        # Safe fallback: liquidation preference per share when it is a dollar value and the
        # clause references OIP/original issue price.
        liq_text = raw(row.get("liquidation_preference"))
        liq_value = liq_pref_per_share(row)
        if liq_value and liq_value not in {"NA", "N/A", "Not stated", "Needs review"}:
            if re.search(r"original\s+(issue|issuance|purchase)\s+price|\bOIP\b|1\s*x|one\s+times", liq_text, re.I):
                if _first_money(liq_value):
                    return _first_money(liq_value)
 
        # Last fallback: initial conversion price, but only if identified as initial/original.
        conv_text = f"{raw(row.get('conversion_price'))} {raw(row.get('conversion_ratio'))}"
        conv_value = price_from(row.get("conversion_price"))
        if conv_value not in {"", "NA", "N/A", "Not stated", "Needs review"}:
            if re.search(r"initial|original\s+conversion\s+price|initially", conv_text, re.I):
                return conv_value
        return ""

    def cap_price_includes_dividends(row: Dict[str, Any]) -> str:
        cap_value = participation_cap_value(row.get("participation_cap"))
        if raw(cap_value).lower() in {"", "na", "n/a", "not stated", "needs review", "none", "null"}:
            return ""

        combined = " ".join([
            raw(row.get("participation_cap")),
            raw(row.get("source_text")),
        ]).lower()

        if any(x in combined for x in [
            "excluding dividends", "excludes dividends", "does not include dividends",
            "without dividends", "exclusive of dividends",
        ]):
            return "No"

        if any(x in combined for x in [
            "including dividends", "includes dividends", "inclusive of dividends",
            "declared but unpaid", "declared and unpaid",
            "accrued but unpaid", "accrued and unpaid",
            "accumulated but unpaid", "accumulated and unpaid",
        ]):
            return "Yes"

        return "No"
 
    def assign_liquidation_ranks(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, int], int]:
        """Assign numeric liquidation seniority ranks deterministically.
 
        - Highest priority: explicit row-specific rank/ordinal in the seniority field
          such as "Rank 5", "Fifth liquidation preference tier", "Second payment tier".
        - Do not let Senior To / Junior To relationship logic overwrite explicit ranks.
        - Use Junior To / Senior To only as fallback for rows without explicit ranks.
        - Common Stock is ranked after the highest preferred rank.
        """
        preferred_rows = [
            r for r in rows
            if "preferred" in raw(r.get("security_name")).lower()
            and raw(r.get("security_name")).lower() != "preferred stock"
        ]
        names = [raw(r.get("security_name")) for r in preferred_rows]
        ranks: Dict[str, Optional[int]] = {name: None for name in names}
 
        ordinal_map = {
            "first": 1, "1st": 1,
            "second": 2, "2nd": 2,
            "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5,
            "sixth": 6, "6th": 6,
            "seventh": 7, "7th": 7,
            "eighth": 8, "8th": 8,
            "ninth": 9, "9th": 9,
            "tenth": 10, "10th": 10,
            "eleventh": 11, "11th": 11,
            "twelfth": 12, "12th": 12,
        }
 
        def normalize_series_token(text: str) -> str:
            t = raw(text).lower()
            t = re.sub(r"[^a-z0-9]+", " ", t)
            t = re.sub(r"\s+", " ", t).strip()
            return t
 
        def series_tokens(text: str) -> List[str]:
            t = normalize_series_token(text)
            tokens: List[str] = []
            for m in re.finditer(r"\bseries\s+[a-z]+(?:\s+\d+)?\b", t):
                tokens.append(m.group(0))
            if re.search(r"\bcommon\b", t) and "preferred" not in t:
                tokens.append("common")
            return tokens
 
        name_tokens: Dict[str, List[str]] = {name: series_tokens(name) for name in names}
 
        def mentioned_names(text: str) -> List[str]:
            low_tokens = set(series_tokens(text))
            low = normalize_series_token(text)
            found: List[str] = []
            for n in names:
                nts = name_tokens.get(n) or []
                if any(tok in low_tokens for tok in nts) or normalize_series_token(n) in low:
                    found.append(n)
            out: List[str] = []
            for n in found:
                if n not in out:
                    out.append(n)
            return out
 
        def explicit_rank(text: str) -> Optional[int]:
            """Parse row-specific rank/tier wording."""
            t = raw(text).lower()
            if not t:
                return None
            m = re.search(r"\b(?:seniority|rank|tier|priority)\b\s*[:=\-]?\s*(\d{1,2})\b", t)
            if m:
                return int(m.group(1))
            for word, num in ordinal_map.items():
                patterns = [
                    rf"\b{word}\b\s+(?:liquidation\s+)?(?:preference\s+)?(?:tier|rank|priority|payment|waterfall)",
                    rf"\b{word}\b\s+(?:payment|distribution)\s+(?:tier|priority)",
                    rf"(?:tier|rank|priority|payment)\s+\b{word}\b",
                    rf"\b{word}\b.*\b(?:liquidation\s+preference\s+tier|payment\s+tier|distribution\s+tier)",
                ]
                if any(re.search(p, t) for p in patterns):
                    return num
            return None
 
        # 1. Explicit row-specific ranks from the seniority field: never overwrite these later.
        for r in preferred_rows:
            name = raw(r.get("security_name"))
            rank = explicit_rank(raw(r.get("seniority")))
            if rank is not None:
                ranks[name] = rank
 
        # 2. Source Text fallback: only if row has no explicit rank and is near an ordinal.
        def current_security_near_ordinal(source_text: str, name: str) -> Optional[int]:
            t = normalize_series_token(source_text)
            nts = name_tokens.get(name) or []
            if not t or not nts:
                return None
            for word, num in ordinal_map.items():
                for m in re.finditer(rf"\b{word}\b", t):
                    start_i = max(0, m.start() - 220)
                    end_i = min(len(t), m.end() + 220)
                    window = t[start_i:end_i]
                    if any(tok in window for tok in nts) and re.search(r"liquidation|preference|payment|distribution|tier|prior", window):
                        return num
            return None
 
        for r in preferred_rows:
            name = raw(r.get("security_name"))
            if ranks[name] is None:
                rank = current_security_near_ordinal(raw(r.get("source_text")), name)
                if rank is not None:
                    ranks[name] = rank
 
        # 3. Junior To fallback: use only when no rank exists. Iteratively set rank to
        # max(rank of senior securities) + 1 if senior ranks are known.
        changed = True
        while changed:
            changed = False
            for r in preferred_rows:
                name = raw(r.get("security_name"))
                if ranks[name] is not None:
                    continue
                senior_names = [n for n in mentioned_names(" ".join([raw(r.get("junior_to")), raw(r.get("seniority"))])) if n != name]
                known = [ranks[n] for n in senior_names if ranks.get(n) is not None]
                if known:
                    ranks[name] = max(int(x) for x in known if x is not None) + 1
                    changed = True
 
        # 4. Senior To fallback: current row is above another known row.
        changed = True
        while changed:
            changed = False
            for r in preferred_rows:
                name = raw(r.get("security_name"))
                if ranks[name] is not None:
                    continue
                junior_names = [n for n in mentioned_names(raw(r.get("senior_to"))) if n != name]
                known = [ranks[n] for n in junior_names if ranks.get(n) is not None]
                if known:
                    candidate = min(int(x) for x in known if x is not None) - 1
                    if candidate >= 1:
                        ranks[name] = candidate
                        changed = True
 
        # 5. Last fallback: count explicitly mentioned senior securities in Junior To text.
        for r in preferred_rows:
            name = raw(r.get("security_name"))
            if ranks[name] is None:
                senior_names = [n for n in mentioned_names(raw(r.get("junior_to"))) if n != name]
                if senior_names:
                    ranks[name] = len(set(senior_names)) + 1
 
        # 6. Unresolved preferred rows go after known preferred ranks.
        max_known = max([int(v) for v in ranks.values() if v is not None], default=0)
        final_ranks: Dict[str, int] = {}
        for name in names:
            if ranks[name] is None:
                final_ranks[name] = max_known + 1 if max_known else 1
            else:
                final_ranks[name] = int(ranks[name])
 
        # Preserve explicit ranks and same-tier relationships, but close gaps if present.
        unique = sorted(set(final_ranks.values()))
        remap = {old: i + 1 for i, old in enumerate(unique)}
        final_ranks = {n: remap[v] for n, v in final_ranks.items()}
        common_rank = max(final_ranks.values(), default=0) + 1
        return final_ranks, common_rank

    def conversion_price_summary(row: Dict[str, Any]) -> str:
        combined_source = " ".join([raw(row.get("oip_original_issue_price")), raw(row.get("liquidation_preference")), raw(row.get("conversion_price")), raw(row.get("source_text"))])
        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined_source, re.I):
            return "Not stated / automatic conversion trigger"
        return price_from(row.get("conversion_price"))
 
    # Remove aggregate Preferred Stock row where detailed series rows exist.

    all_security_names = [raw(sec.get("security_name")) for sec in securities if raw(sec.get("security_name"))]
    detailed_preferred_names = [
        n for n in all_security_names
        if "preferred" in n.lower()
        and n.lower() not in {"preferred stock", "series preferred stock", "series seed preferred stock"}
    ]
    has_split_seed_series = any(re.search(r"\bseries\s+seed\s*[-–—]?\s*\d+", n, re.I) for n in all_security_names)
    has_any_detailed_preferred = len(detailed_preferred_names) > 0

    source_rows: List[Dict[str, Any]] = []
    for sec in securities:
        name = raw(sec.get("security_name"))
        if not name:
            continue
        low_name = name.lower()
        if low_name == "preferred stock" and has_any_detailed_preferred:
            continue
        if low_name == "series preferred stock" and has_any_detailed_preferred:
            continue
        if low_name == "series seed preferred stock" and has_split_seed_series:
            continue
        if "blank check" in low_name or "undesignated preferred" in low_name or "initially undesignated" in low_name:
            continue
        source_rows.append(sec)
 
    ranks, common_rank = assign_liquidation_ranks(source_rows)
    cap_table: List[Dict[str, Any]] = []
    for row in source_rows:
        name = raw(row.get("security_name"))
        is_common = "common" in name.lower() and "preferred" not in name.lower()
        is_preferred = "preferred" in name.lower()
        cap_table.append({
            "series_type": "" if is_common else ("Preferred Share" if is_preferred else clean(row.get("security_type"))),
            "series_name": clean(name),
            "authorized_count": _normalize_authorized_count(row.get("authorized_count")),
            "issue_strike_price": "" if is_common else issue_strike_price(row),
            "liq_pref_per_share": "" if is_common else liq_pref_per_share(row),
            "seniority_in_liq_pref": seniority_rank_from_row(row, common_rank if is_common else ranks.get(name, "NA")),
            "participation_rights": "" if is_common else participation_summary(row),
            "participation_cap_per_share": "" if is_common else participation_cap_value(row.get("participation_cap")),
            "conversion_price": "" if is_common else conversion_price_summary(row),
            "conversion_ratio": "" if is_common else conversion_ratio_for_cap_table(row),
            "dividend_payable": "" if is_common else dividend_payable(row),
            "dividend_rate": "" if is_common else dividend_rate_pct(row),
            "dividend_type": "" if is_common else _normalize_dividend_type(
                row.get("dividend_type"),
                " ".join([raw(row.get("dividend_paying")), raw(row.get("dividend_rate")), raw(row.get("source_text"))]),
            ),
            "cap_price_includes_dividends": "" if is_common else cap_price_includes_dividends(row),
        })
    def _sort_key(r: Dict[str, Any]) -> Tuple[int, int, str]:
        name = raw(r.get("series_name")).lower()
        is_common = "common" in name and "preferred" not in name
        is_pref = "preferred" in name
        try:
            rank = int(float(str(r.get("seniority_in_liq_pref", 999)).strip()))
        except Exception:
            rank = 999
        group = 0 if is_pref else (2 if is_common else 1)
        return (group, rank, name)

    cap_table.sort(key=_sort_key)
    return cap_table

    
 
    # return cap_table

def _validate_securities_against_source_text(
    securities: List[Dict[str, Any]], source_document_text: str
) -> List[Dict[str, Any]]:
    """JSON-list equivalent of validate_terms_dataframe() in extract_articles.py.
    Corrects OIP / conversion_price using deterministic source-text matching
    (catches OCR/decimal misreads even when the model's own source_text is wrong).
    """
    if not securities:
        return securities
    text_blob = "\n".join(
        str(sec.get(k, "")) for sec in securities
        for k in ("source_text", "liquidation_preference", "oip_original_issue_price", "conversion_price")
    )
    for sec in securities:
        name = str(sec.get("security_name", ""))
        if "preferred" not in name.lower():
            continue
        if "blank check" in name.lower() or "undesignated" in name.lower():
            continue
        row_context = f"{sec.get('source_text','')} {sec.get('liquidation_preference','')} {sec.get('oip_original_issue_price','')}"
        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", row_context, re.I):
            continue

        source_price = None
        if source_document_text:
            source_price = _nearest_oip_from_source(name, source_document_text)
        if source_price is None:
            source_price = _nearest_oip_from_source(name, f"{sec.get('source_text','')}\n{text_blob}")
        if source_price is None:
            continue

        current = _money_to_float(sec.get("oip_original_issue_price"))
        if current is None or abs(current - source_price) > max(0.0000005, abs(source_price) * 0.0005):
            corrected = _fmt_money_num(source_price)
            sec["oip_original_issue_price"] = corrected
            conv = _money_to_float(sec.get("conversion_price"))
            if conv is None or current is None or abs(conv - current) <= max(0.0000005, abs(current or 0) * 0.0005):
                sec["conversion_price"] = corrected
            flags = str(sec.get("review_flags", "") or "").strip()
            note = "OIP corrected by source-price validation"
            if note not in flags:
                sec["review_flags"] = (flags + "; " + note).strip("; ").strip()
    return securities

def _apply_conversion_ratio_normalization(data: Dict[str, Any]) -> Dict[str, Any]:
    """Overwrite each security's conversion_ratio with the computed numeric
    form where the underlying conversion_price and oip_original_issue_price
    support a calculation. Leaves the model's text untouched (e.g. "Formula
    stated...", "N/A", "Not stated") when no ratio can be calculated.
    """
    for sec in (data.get("securities") or []):
        sec["conversion_ratio"] = _compute_conversion_ratio(
            sec.get("conversion_ratio"),
            sec.get("conversion_price"),
            sec.get("oip_original_issue_price"),
        )
    return data

def _load_dictionary_phrases(dictionary_path: Path) -> Dict[str, List[str]]:
    """Load phrase dictionary from Excel. Sheet name = category."""
    if not dictionary_path.exists():
        raise FileNotFoundError(f"Dictionary file not found: {dictionary_path}")

    import pandas as pd
    xls = pd.ExcelFile(dictionary_path)
    phrases_by_category: Dict[str, List[str]] = {}
    candidate_column_words = ("phrase", "pattern", "keyword", "search")

    for sheet_name in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet_name, dtype=str).fillna("")
        except Exception:
            continue
        if df.empty:
            continue

        candidate_cols = [
            col for col in df.columns
            if any(word in str(col).strip().lower() for word in candidate_column_words)
        ]
        if not candidate_cols:
            continue

        category = str(sheet_name).strip() or "Dictionary"
        phrases: List[str] = []
        for col in candidate_cols:
            for raw in df[col].astype(str).tolist():
                phrase = raw.strip()
                if not phrase or len(phrase) < 4 or len(phrase) > 220:
                    continue
                if phrase.lower() in {"nan", "none", "n/a", "not stated"}:
                    continue
                if len(re.sub(r"[^A-Za-z]", "", phrase)) < 3:
                    continue
                phrases.append(phrase)

        seen, unique_phrases = set(), []
        for phrase in phrases:
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                unique_phrases.append(phrase)
        if unique_phrases:
            phrases_by_category[category] = unique_phrases

    return phrases_by_category

@lru_cache(maxsize=1)
def _get_dictionary_phrases() -> Dict[str, List[str]]:
    path = Path(getattr(
        django_settings, "ARTICLES_DICTIONARY_PATH",
        Path(__file__).resolve().parent / "Dictionary" / "articles_charter_extraction_dictionary_with_scenario_universe.xlsx",
    ))
    try:
        return _load_dictionary_phrases(path)
    except FileNotFoundError:
        return {}  # extraction still works, just falls back to plain truncation
    

def _build_dictionary_evidence(
    text: str,
    phrases_by_category: Dict[str, List[str]],
    max_chars: int,
    window_chars: int = 1400,
    max_matches_per_category: int = 35,
) -> str:
    text = _normalize_whitespace(text)
    if len(text) <= max_chars:
        return text
    if not phrases_by_category:
        return text[:max_chars]

    lower = text.lower()
    windows: List[Tuple[int, int, str]] = []
    for category, phrases in phrases_by_category.items():
        matches_for_category = 0
        for phrase in phrases:
            if matches_for_category >= max_matches_per_category:
                break
            phrase_l = phrase.lower()
            start_search = 0
            while matches_for_category < max_matches_per_category:
                idx = lower.find(phrase_l, start_search)
                if idx == -1:
                    break
                start = max(0, idx - window_chars)
                end = min(len(text), idx + len(phrase) + window_chars)
                windows.append((start, end, f"{category}: {phrase}"))
                matches_for_category += 1
                start_search = idx + len(phrase_l)

    if not windows:
        return text[:max_chars]

    front_matter = text[: min(25000, max_chars // 4)]
    windows.sort(key=lambda item: item[0])
    merged: List[Tuple[int, int, List[str]]] = []
    for start, end, label in windows:
        if not merged or start > merged[-1][1] + 300:
            merged.append((start, end, [label]))
        else:
            old_start, old_end, labels = merged[-1]
            labels.append(label)
            merged[-1] = (old_start, max(old_end, end), labels)

    output_parts = ["--- DOCUMENT FRONT MATTER ---", front_matter, "\n--- DICTIONARY MATCHED EVIDENCE WINDOWS ---"]
    used = sum(len(p) for p in output_parts)
    for i, (start, end, labels) in enumerate(merged, start=1):
        chunk = text[start:end]
        header = f"\n--- Evidence Window {i} | chars {start}-{end} | matches: {', '.join(labels[:8])} ---\n"
        if used + len(header) + len(chunk) > max_chars:
            break
        output_parts.append(header)
        output_parts.append(chunk)
        used += len(header) + len(chunk)

    return "\n".join(output_parts)[:max_chars]  


def _extract_via_local_text(
    client, model: str, file_name: str, text: str, max_chars: int,
    *, reasoning_effort: str = "medium", max_output_tokens: int = 8000,
) -> Dict[str, Any]:
    phrases_by_category = _get_dictionary_phrases()
    evidence = _build_dictionary_evidence(text, phrases_by_category, max_chars=max_chars)  # was: text[:max_chars]
    user_text = (
        f"Document name: {file_name}\n"
        f"Extract the securities terms from the document text below.\n"
        f"Use one row/object per security/class/series.\n\n"
        f"DOCUMENT TEXT:\n{evidence}"
    )
    kwargs: Dict[str, Any] = dict(
        model=model,
        instructions=INSTRUCTIONS,
        input=[{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
        text=_text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        max_output_tokens=max_output_tokens,
    )
    response = client.responses.create(**kwargs)
    return _response_to_json(response)

def _extract_via_file_api(
    client,
    model: str,
    file_name: str,
    file_bytes: bytes,
    reasoning_effort: str = "medium",
    max_output_tokens: int = 8000,
) -> Dict[str, Any]:
    import io as _io
    uploaded = client.files.create(
        file=(_safe_upload_filename(file_name), _io.BytesIO(file_bytes)),
        purpose="user_data",
    )
    user_text = (
        f"Document name: {file_name}\n"
        "Extract the securities terms from the uploaded document.\n"
        "Use one row/object per security/class/series.\n"
        "If the file is a scanned PDF, use the visible page images and text."
    )
    try:
        kwargs: Dict[str, Any] = dict(
            model=model,
            instructions=INSTRUCTIONS,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": uploaded.id},
                    {"type": "input_text", "text": user_text},
                ],
            }],
            text=_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            max_output_tokens=max_output_tokens,
        )
        response = client.responses.create(**kwargs)
        return _response_to_json(response)
    finally:
        try:
            client.files.delete(uploaded.id)
        except Exception:
            pass


def _quality_review_via_file_api(
    client,
    model: str,
    file_name: str,
    file_bytes: bytes,
    current_data: Dict[str, Any],
    reasoning_effort: str = "medium",
    max_output_tokens: int = 8000,
) -> Dict[str, Any]:
    import io as _io
    uploaded = client.files.create(
        file=(_safe_upload_filename(file_name), _io.BytesIO(file_bytes)),
        purpose="user_data",
    )
    user_text = (
        f"Document name: {file_name}\n\n"
        f"Prior extraction JSON:\n{json.dumps(current_data, ensure_ascii=False)}\n\n"
        "Perform a targeted OIP and seniority quality review from the uploaded source file. "
        "If the PDF is scanned, use visible page images. Return the full corrected JSON."
    )
    try:
        kwargs: Dict[str, Any] = dict(
            model=model,
            instructions=QUALITY_REVIEW_INSTRUCTIONS,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": uploaded.id},
                    {"type": "input_text", "text": user_text},
                ],
            }],
            text=_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            max_output_tokens=max_output_tokens,
        )
        response = client.responses.create(**kwargs)
        return _response_to_json(response)
    finally:
        try:
            client.files.delete(uploaded.id)
        except Exception:
            pass


def _oip_price_review_via_file_api(
    client, model: str, file_name: str, file_bytes: bytes, current_data: Dict[str, Any],
    reasoning_effort: str = "medium", max_output_tokens: int = 8000,
) -> Dict[str, Any]:
    """Dedicated source-document OIP validation pass, ported from
    oip_price_review_with_file_api() in extract_articles.py."""
    securities = current_data.get("securities", []) or []
    security_list = [str(sec.get("security_name", "")) for sec in securities if str(sec.get("security_name", "")).strip()]
    if not security_list:
        return current_data

    import io as _io
    uploaded = client.files.create(
        file=(_safe_upload_filename(file_name), _io.BytesIO(file_bytes)),
        purpose="user_data",
    )
    user_text = (
        f"Document name: {file_name}\n\n"
        f"Expected securities/classes/series from prior extraction:\n"
        f"{json.dumps(security_list, ensure_ascii=False, indent=2)}\n\n"
        f"Prior extraction JSON for context:\n{json.dumps(current_data, ensure_ascii=False)}\n\n"
        "Extract exact OIP / Original Issue Price values only from the uploaded source file. "
        "Use page images if the PDF is scanned. Return strict JSON only."
    )
    try:
        kwargs: Dict[str, Any] = dict(
            model=model,
            instructions=OIP_PRICE_REVIEW_INSTRUCTIONS,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": uploaded.id},
                    {"type": "input_text", "text": user_text},
                ],
            }],
            text=_oip_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            max_output_tokens=max_output_tokens,
        )
        response = client.responses.create(**kwargs)
        oip_json = _response_to_json(response)
        return _apply_oip_prices_to_data(current_data, oip_json)
    finally:
        try:
            client.files.delete(uploaded.id)
        except Exception:
            pass

def _seniority_waterfall_review_via_file_api(
    client,
    model: str,
    file_name: str,
    file_bytes: bytes,
    current_data: Dict[str, Any],
    reasoning_effort: str = "medium",
    max_output_tokens: int = 8000,
) -> Dict[str, Any]:
    """Dedicated seniority-only pass using the full uploaded source file, then apply
    the returned ranks onto current_data via _apply_seniority_tiers_to_data.

    Ported from extract_articles.py's seniority_waterfall_review_with_file_api().
    Always uses the file-api (full document upload), same as the original script —
    this pass is explicitly meant to read the document "like a human reviewer would",
    including scanned/image-based PDFs, so local extracted text is not used here.
    """

    current_data = _oip_price_review_via_file_api(
        client, model, file_name, file_bytes, current_data,
        reasoning_effort=reasoning_effort, max_output_tokens=max_output_tokens,
    )

    securities = current_data.get("securities", []) or []
    security_list = [str(sec.get("security_name", "")) for sec in securities if str(sec.get("security_name", "")).strip()]
    if not security_list:
        return current_data

    import io as _io
    uploaded = client.files.create(
        file=(_safe_upload_filename(file_name), _io.BytesIO(file_bytes)),
        purpose="user_data",
    )
    user_text = (
        f"Document name: {file_name}\n\n"
        f"Expected securities/classes/series from prior extraction:\n"
        f"{json.dumps(security_list, ensure_ascii=False, indent=2)}\n\n"
        f"Prior extraction JSON for context:\n{json.dumps(current_data, ensure_ascii=False)}\n\n"
        "Extract liquidation seniority tiers only from the uploaded source file. "
        "Use page images if the PDF is scanned. Return strict JSON only."
    )
    try:
        kwargs: Dict[str, Any] = dict(
            model=model,
            instructions=SENIORITY_WATERFALL_INSTRUCTIONS,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": uploaded.id},
                    {"type": "input_text", "text": user_text},
                ],
            }],
            text=_seniority_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
            max_output_tokens=max_output_tokens,
        )
        response = client.responses.create(**kwargs)
        tiers_json = _response_to_json(response)
        return _apply_seniority_tiers_to_data(current_data, tiers_json)
    finally:
        try:
            client.files.delete(uploaded.id)
        except Exception:
            pass

def run_extraction(
    file_name: str,
    file_bytes: bytes,
    *,
    model: str,
    api_key: str,
    max_chars: int = 120_000,
    reasoning_effort: str = "medium",
    quality_reasoning_effort: str = "high",
    max_output_tokens: int = 8000,
    quality_pass: bool = True,
    seniority_pass: bool = True,
) -> Dict[str, Any]:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    suffix = Path(file_name).suffix.lower()
    LOCAL_TEXT_EXTS = {".pdf", ".docx", ".txt", ".md"}
    FILE_API_EXTS   = {".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".md"}

    print(
        f"[ArticleExtractor] File: {file_name!r} | Model: {model!r} | "
        f"reasoning_effort: {reasoning_effort!r} | max_output_tokens: {max_output_tokens} | "
        f"quality_pass: {quality_pass} | seniority_pass: {seniority_pass}"
    )

    data: Optional[Dict[str, Any]] = None

    if suffix in LOCAL_TEXT_EXTS:
        try:
            if suffix == ".pdf":
                text = _extract_pdf_text(file_bytes)
            elif suffix == ".docx":
                text = _extract_docx_text(file_bytes)
            else:
                text = file_bytes.decode("utf-8", errors="replace")
            text = _normalize_whitespace(text)
            if len(text.strip()) >= 1500:
                print(f"[ArticleExtractor] Pass 1/main — local text extraction | model: {model!r} | reasoning_effort: {reasoning_effort!r}")
                data = _extract_via_local_text(
                    client, model, file_name, text, max_chars,
                    reasoning_effort=reasoning_effort,
                    max_output_tokens=max_output_tokens,
                )
                if not (data.get("securities") or []):
                    print("[ArticleExtractor] Local text extraction returned no securities — falling back to file API.")
                    data = None
        except Exception as exc:
            print(f"[ArticleExtractor] Local text extraction failed ({exc!r}) — falling back to file API.")
            data = None

    if data is None:
        if suffix not in FILE_API_EXTS:
            raise ValueError(f"Unsupported file type: {suffix}")
        print(f"[ArticleExtractor] Pass 1/main — file API extraction | model: {model!r} | reasoning_effort: {reasoning_effort!r}")
        data = _extract_via_file_api(
            client, model, file_name, file_bytes,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )

    if quality_pass and _needs_quality_review(data) and suffix in FILE_API_EXTS:
        print(f"[ArticleExtractor] Pass 2/quality review — file API | model: {model!r} | quality_reasoning_effort: {quality_reasoning_effort!r}")
        data = _quality_review_via_file_api(
            client, model, file_name, file_bytes, data,
            reasoning_effort=quality_reasoning_effort,
            max_output_tokens=max_output_tokens,
        )

    if seniority_pass and suffix in FILE_API_EXTS:
        print(f"[ArticleExtractor] Pass 3/seniority waterfall — file API | model: {model!r} | quality_reasoning_effort: {quality_reasoning_effort!r}")
        data = _seniority_waterfall_review_via_file_api(
            client, model, file_name, file_bytes, data,
            reasoning_effort=quality_reasoning_effort,
            max_output_tokens=max_output_tokens,
        )
    source_document_text = ""
    try:
        if suffix == ".pdf":
            source_document_text = _extract_pdf_text(file_bytes)
        elif suffix == ".docx":
            source_document_text = _extract_docx_text(file_bytes)
        elif suffix in {".txt", ".md"}:
            source_document_text = _normalize_whitespace(file_bytes.decode("utf-8", errors="replace"))
    except Exception:
        source_document_text = ""

    data["securities"] = _validate_securities_against_source_text(
        data.get("securities") or [], source_document_text
    )

    for sec in (data.get("securities") or []):
        sec["authorized_count"] = _normalize_authorized_count(
            sec.get("authorized_count", "Not stated"), missing_as="Not stated"
        )
        dividend_context = " ".join([
            str(sec.get("dividend_paying", "")),
            str(sec.get("dividend_rate", "")),
            str(sec.get("source_text", "")),
        ])
        sec["dividend_type"] = _normalize_dividend_type(
            sec.get("dividend_type", "Not stated"), dividend_context, missing_as="Not stated"
        )
        rate_context = " ".join([
            str(sec.get("dividend_paying", "")),
            str(sec.get("dividend_type", "")),
            str(sec.get("source_text", "")),
        ])
        sec["dividend_rate"] = _normalize_dividend_rate_percent(
            sec.get("dividend_rate", "Not stated"), rate_context, missing_as="Not stated"
        )

    print(f"[ArticleExtractor] Extraction complete. Securities found: {len(data.get('securities') or [])}")
    # data = _apply_conversion_ratio_normalization(data)
    data["cap_table"] = _build_cap_table(data.get("securities") or [])
    return data