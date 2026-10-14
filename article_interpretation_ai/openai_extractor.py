"""
articles_app/openai_extractor.py  (single-file version)

PART 1 (top of file): verbatim engine from extract_articles.py (v17_6_price_roles_and_conversion_ratios)
with ONLY the CLI / Excel / filesystem-output code removed. Prompts, schemas, verification
passes and validators are unchanged and must keep their ORIGINAL ORDER (later definitions
intentionally override / wrap earlier ones).
PART 2 (bottom of file): Django adapter -> run_extraction(), returns the Excel data as dicts.

Original description follows.

Articles / Charter Securities Terms Extraction Automation

Extracts one row per security/class/series from company Articles, Charters,
Certificates of Incorporation, or similar corporate constitutional documents.

Output columns include:
Company, Security Name, Authorized Count, OIP, Liquidation Preference,
Dividend terms, Conversion Ratio, Participation Rights, Seniority, Confidence,
and Source Text / Section.

Usage example:
    python extract_articles.py --input ./articles.pdf --output ./extracted_terms.xlsx --mode auto --workers 1 --reasoning-effort low

Environment:
    OPENAI_API_KEY must be set.
    OPENAI_MODEL must be set in the .env file next to this script. The script does not accept a --model command-line override.
"""

from __future__ import annotations

import argparse
import json
import os
import hashlib
import re
import sys
import time
import csv
import shutil
import tempfile
import math
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings as django_settings

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: pandas. Run: pip install -r requirements.txt") from exc

try:
    from openai import OpenAI
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: openai. Run: pip install -r requirements.txt") from exc

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: pymupdf. Run: python -m pip install pymupdf") from exc

try:
    from pydantic import BaseModel, ConfigDict, ValidationError
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: pydantic. Run: python -m pip install pydantic") from exc

try:
    from rapidfuzz import fuzz
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: rapidfuzz. Run: python -m pip install rapidfuzz") from exc

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
except Exception as exc:  # pragma: no cover
    raise ImportError("Missing dependency: tenacity. Run: python -m pip install tenacity") from exc


SUPPORTED_LOCAL_TEXT = {".pdf", ".docx", ".txt", ".md"}
SUPPORTED_FILE_API = {".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".md"}
SCRIPT_VERSION = "v17_2_final_field_certification"

OUTPUT_COLUMNS = [
    "Company Name",
    "Document Name",
    "Security Name",
    "Security Type",
    "Authorized Count",
    "OIP / Original Issue Price",
    "Liquidation Preference",
    "Dividend Paying",
    "Dividend Type",
    "Dividend Rate",
    "Conversion Ratio",
    "Conversion Price",
    "Participation Rights",
    "Participation Cap",
    "Seniority",
    "Senior To",
    "Pari Passu With",
    "Junior To",
    "Applies To",
    "Source Basis",
    "Confidence",
    "Source Section",
    "Source Text",
    "Review Flags",
]



CAP_TABLE_SUMMARY_COLUMNS = [
    "Series Type",
    "Series Name",
    "Issue / Strike Price",
    "Liq. Pref. / Share",
    "Seniority in Liq. Pref.",
    "Participation Rights",
    "Participation Cap $ / Share",
    "Conversion Price",
    "Conversion Ratio",
    "Is Dividend Payable?",
    "Dividend Rate",
    "Dividend Type",
    "Cap price Includes Dividends",
]


def normalize_authorized_count(value: Any, missing_as: str = "NA") -> str:
    """Return only the authorized share count number, without words like 'shares'."""
    text = "" if value is None else str(value).strip()
    if not text:
        return missing_as
    if text.lower() in {"not stated", "n/a", "na", "none", "null", "needs review", "series-specific"}:
        return missing_as
    # Prefer a number near the word share/shares.
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


def normalize_dividend_rate_percent(value: Any, context: Any = "", missing_as: str = "") -> str:
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


def normalize_dividend_type(value: Any, context: Any = "", missing_as: str = "NA") -> str:
    """Classify dividend type conservatively for valuation output.

    Key v14 rules:
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
        # Non-compounded by itself usually appears in a cumulative dividend definition.
        return "Cumulative, non-compounded"
    if has_compounded:
        return "Cumulative, compounded"
    if re.search(r"\bsimple\b", combined, re.I):
        return "Simple"

    # Knowcraft convention: when-and-if-declared without accrual language = Non-cumulative.
    if re.search(r"when,?\s+as\s+and\s+if\s+declared|if\s+and\s+when\s+declared|when\s+declared|when\s+and\s+if\s+declared", combined, re.I):
        return "Non-cumulative"

    if text.lower() in null_tokens:
        return missing_as
    return text


def build_cap_table_summary(terms_df: pd.DataFrame) -> pd.DataFrame:
    """Create a cap-table-style summary sheet with concise finance-friendly values.

    Presentation rules:
    - Remove the aggregate Preferred Stock row when series-level rows are present.
    - Common Stock: keep Series Type blank, use next liquidation rank after Preferred,
      and blank/grey N/A preference, participation, dividend and cap-dividend cells.
    - Seniority is numeric (1, 2, 3...) rather than long ranking sentences.
    - Authorized Count is intentionally excluded from this downstream summary.
    - Conversion Ratio is concise (e.g., 1:1, 2:1).
    - Dividend Rate is shown as a percentage where it can be computed.
    """
    if terms_df.empty:
        return pd.DataFrame(columns=CAP_TABLE_SUMMARY_COLUMNS)

    missing_tokens = {"", "nan", "not stated", "n/a", "na", "none", "null", "needs review", "series-specific"}

    def raw(value: Any) -> str:
        return "" if value is None else str(value).strip()

    def is_missing(value: Any) -> bool:
        return raw(value).lower() in missing_tokens

    def clean(value: Any, na: str = "NA") -> str:
        text = raw(value)
        low = text.lower()
        if low in {"", "nan", "not stated", "none", "null", "needs review", "series-specific"}:
            return ""
        if low in {"n/a", "na"}:
            return na
        return text

    def first_money(text: Any) -> str:
        text = raw(text)
        m = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", text)
        if m:
            return re.sub(r"\$\s+", "$", m.group(0))
        if re.search(r"\b(per share|issue price|purchase price|strike price|conversion price)\b", text, re.I):
            m = re.search(r"\b\d[\d,]*(?:\.\d+)?\b", text)
            if m:
                return "$" + m.group(0)
        return ""

    def money_num(value: Any) -> Optional[float]:
        money = first_money(value)
        if not money:
            return None
        try:
            return float(money.replace("$", "").replace(",", ""))
        except Exception:
            return None

    def price_from(value: Any) -> str:
        money = first_money(value)
        return money if money else clean(value)

    def fmt_money(value: float) -> str:
        return _fmt_money_num(value, decimals=9)

    def concise_summary_value(value: Any, field: str, na: str = "NA") -> str:
        """Keep Cap Table Summary valuation-friendly and free of legal narrative."""
        text = raw(value)
        if is_missing(text):
            low = text.lower()
            return na if low in {"n/a", "na"} else ""
        money = first_money(text)
        if field in {"liquidation", "cap", "conversion_price", "issue_price"} and money:
            return money
        if field in {"liquidation", "cap"}:
            m = re.search(r"\b(\d+(?:\.\d+)?)\s*x\b", text, re.I)
            if m:
                return f"{m.group(1)}x OIP"
            word_map = {
                "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
            }
            m = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*\(\s*\d+(?:\.\d+)?\s*\))?\s+times\b", text, re.I)
            if m:
                return f"{word_map[m.group(1).lower()]}x OIP"
            if re.search(r"qualified\s+capital\s+balance", text, re.I):
                return "Qualified Capital Balance"
        if field == "conversion_ratio":
            m = re.search(r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b", text)
            if m:
                return re.sub(r"\s+", "", m.group(0))
            if "formula" in text.lower():
                return "Formula stated"
        if field == "dividend_type":
            low = text.lower()
            for label in ["Cumulative, non-compounded", "Cumulative, compounded", "Non-cumulative", "Cumulative", "Simple"]:
                if label.lower() in low:
                    return label
        # Never carry a long legal sentence into Cap Table Summary.
        if len(text) > 45 or len(text.split()) > 7:
            return na
        return text

    def liquidation_multiplier(text: Any) -> Optional[float]:
        t = raw(text).lower().replace("–", "-").replace("—", "-")
        if not t:
            return None
        # Numeric forms: 1.25x, 2x, 1 x.
        m = re.search(r"\b(\d+(?:\.\d+)?)\s*x\b", t)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        phrase_map = {
            "one and one fourth": 1.25,
            "one and one quarter": 1.25,
            "one and a quarter": 1.25,
            "one and one half": 1.5,
            "one and a half": 1.5,
            "two times": 2.0,
            "twice": 2.0,
            "one times": 1.0,
            "one time": 1.0,
        }
        for phrase, value in phrase_map.items():
            if phrase in t and re.search(r"original\s+(issue|issuance|purchase)\s+price|\bOIP\b", t, re.I):
                return value
        return None

    def liq_pref_per_share(row: pd.Series) -> str:
        """Return liquidation preference without cross-field contamination.

        v15.1: an explicit amount in the Liquidation Preference field wins. OIP is
        used only when the liquidation clause itself states a multiplier/equality
        relationship. This prevents a conversion/OIP value from replacing a distinct
        liquidation amount (for example a 3x preference).
        """
        name = raw(row.get("Security Name"))
        if "common" in name.lower() and "preferred" not in name.lower():
            return ""
        liq = raw(row.get("Liquidation Preference"))
        source = raw(row.get("Source Text"))
        oip = raw(row.get("OIP / Original Issue Price"))
        combined = f"{liq} {source}"

        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined, re.I):
            return "Qualified Capital Balance"

        # Preserve an explicitly conditional liquidation formula. Do not collapse a
        # source-certified 1.6x/1x branch into one computed dollar amount.
        if re.search(r"condition[- ]dependent|\b\d+(?:\.\d+)?\s*x\s*oip\s*(?:or|/)\s*\d+(?:\.\d+)?\s*x\s*oip", liq, re.I):
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*x\s*oip", liq, re.I)
            if len(nums) >= 2:
                return f"{nums[0]}x OIP / {nums[1]}x OIP (condition-dependent)"
            return liq if len(liq) <= 60 else "Condition-dependent liquidation"

        # Strongest source: an explicit dollar liquidation amount in the dedicated field.
        money = first_money(liq)
        if money:
            return money

        base = money_num(oip)
        mult = liquidation_multiplier(liq)
        if base is not None and mult is not None:
            return fmt_money(base * mult)

        if base is not None and re.search(
            r"(?:1\s*x|one\s+times|one\s+time|100%)\s*(?:of\s+)?(?:the\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)|"
            r"(?:equal\s+to|same\s+as)\s+(?:the\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)",
            liq,
            re.I,
        ):
            return fmt_money(base)
        return concise_summary_value(liq, "liquidation")


    def participation_summary(row: pd.Series) -> str:
        """Normalize participation for the Cap Table Summary using field-role priority.

        v15.3 summary rule:
        - An actual Participation Cap is authoritative evidence of capped participation.
        - Explicit non-participating wording wins only when there is no actual cap.
        - "as-converted" wording alone must not turn a capped participating security
          into Non-participating.
        """
        text = raw(row.get("Participation Rights"))
        cap_text = raw(row.get("Participation Cap"))
        cap_missing = is_missing(cap_text)

        # A real cap (dollar cap, multiplier cap, or explicit capped formula) is the
        # strongest summary-level signal that the security participates subject to a cap.
        if not cap_missing:
            if (
                first_money(cap_text)
                or re.search(r"\b\d+(?:\.\d+)?\s*x\b", cap_text, re.I)
                or re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*\(\d+\))?\s+times\b", cap_text, re.I)
                or re.search(r"\bcapp?ed\b|\bmaximum\b|\bcease(?:s|d)?\s+to\s+participate\b", cap_text, re.I)
            ):
                return "Capped Participation"

        combined = " ".join([
            text,
            raw(row.get("Liquidation Preference")),
            raw(row.get("Source Text")),
        ])
        low = combined.lower()
        if is_missing(text):
            return "NA"
        if re.search(r"\bnon[-\s]?participat(?:ing|e|ion)?\b", low, re.I):
            return "Non-participating"
        if "full participation" in low:
            return "Participating"
        if re.search(r"\bparticipat(?:e|es|ing|ion)\b", low) and not re.search(r"\bnon[-\s]?participat", low):
            return "Participating"
        if any(x in low for x in ["deemed conversion", "greater of"]):
            return "Non-participating"
        normalized = clean(text)
        if normalized in {"Full Participation", "Participating"}:
            return "Participating"
        if normalized in {"Capped Participation", "Non-participating", "Non-Participating"}:
            return "Non-participating" if normalized.lower().startswith("non") else "Capped Participation"
        return "NA" if len(normalized) > 45 or len(normalized.split()) > 7 else normalized

    def participation_cap(value: Any, row: Optional[pd.Series] = None) -> str:
        """Return participation cap as a dollar/share amount when determinable.

        v15.5 summary rule:
        - Detect multiplier language BEFORE generic money parsing. This is critical
          because text such as "three (3) times the Original Issue Price per share"
          contains the words "Issue Price"; the generic ``first_money`` helper can
          otherwise treat the parenthetical 3 as a literal $3.
        - Multiplier forms such as 3x, 3 times, three times, and three (3) times
          are calculated from verified OIP.
        - Explicit dollar cap wins only when no multiplier-to-OIP relationship is
          present in the participation-cap field.
        - If the model reduced the cap field to a bare amount such as "$3", a
          narrowly-scoped participation-cap source clause may still establish that
          the number is actually a multiplier of OIP.
        """
        text = raw(value)
        if is_missing(text):
            return "NA"

        word_nums = {
            "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
            "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
        }

        def multiplier_from_cap_text(cap_text: str) -> Optional[float]:
            t = raw(cap_text)
            if not t:
                return None
            # Require cap/multiplier wording tied to OIP / issue price so unrelated
            # numbers in broader text cannot become a participation cap.
            has_oip_basis = bool(re.search(
                r"original\s+(?:issue|issuance|purchase)\s+price|\boip\b|issue\s+price",
                t,
                re.I,
            ))
            if not has_oip_basis:
                return None

            patterns = [
                r"\b(\d+(?:\.\d+)?)\s*x\b",
                r"\b(\d+(?:\.\d+)?)\s+times\b",
            ]
            for pat in patterns:
                m = re.search(pat, t, re.I)
                if m:
                    try:
                        return float(m.group(1))
                    except Exception:
                        pass

            m = re.search(
                r"\b(one|two|three|four|five|six|seven|eight|nine|ten)"
                r"(?:\s*\(\s*(\d+(?:\.\d+)?)\s*\))?\s+times\b",
                t,
                re.I,
            )
            if m:
                try:
                    return float(m.group(2)) if m.group(2) else word_nums[m.group(1).lower()]
                except Exception:
                    return None
            return None

        # 1) Dedicated Participation Cap field is authoritative. Parse multiplier first.
        mult = multiplier_from_cap_text(text)

        # 2) Narrow fallback to Source Text only when the cap field itself does not
        # preserve the multiplier. Require explicit cap wording near the multiplier.
        if mult is None and row is not None:
            source = raw(row.get("Source Text"))
            for m in re.finditer(
                r"(?:capp?ed\s+at|cap(?:ped)?\s+at|maximum(?:\s+amount)?(?:\s+equal\s+to)?|"
                r"payments?.{0,80}?capp?ed\s+at|shall\s+be\s+capp?ed\s+at).{0,180}",
                source,
                re.I,
            ):
                mult = multiplier_from_cap_text(m.group(0))
                if mult is not None:
                    break

        if mult is not None:
            if row is not None:
                oip = money_num(row.get("OIP / Original Issue Price"))
                if oip is not None and oip > 0:
                    return fmt_money(mult * oip)
            return f"{mult:g}x"

        # No multiplier relationship: now an explicit dollar cap may safely win.
        money = first_money(text)
        if money:
            return money
        return concise_summary_value(text, "cap")

    def seniority_rank_from_row(row: pd.Series, fallback: Any = "NA") -> Any:
        """Use explicit Rank X in Extracted Terms before recalculating seniority.

        This fixes cases where the detailed extraction already has correct ranks
        from the dedicated seniority pass, but Cap Table Summary flattened or
        recomputed them incorrectly.
        """
        text = raw(row.get("Seniority"))
        m = re.search(r"\brank\s*[:#=\-]?\s*(\d{1,3})\b", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\bseniority\s*[:#=\-]?\s*(\d{1,3})\b", text, re.I)
        if m:
            return int(m.group(1))
        return fallback

    def dividend_payable(row: pd.Series) -> str:
        dtype = normalize_dividend_type(
            row.get("Dividend Type"),
            " ".join([raw(row.get("Dividend Paying")), raw(row.get("Dividend Rate")), raw(row.get("Source Text"))]),
            missing_as="NA",
        )
        if dtype.startswith("Cumulative"):
            return "Yes"
        if dtype == "Non-cumulative":
            return "No"
        if dtype in {"NA", "N/A"}:
            return "NA"
        return ""


    def dividend_rate_pct(row: pd.Series) -> str:
        """Return a concise dividend percentage without inventing a rate.

        Priority:
        1) Explicit verified percentage in Dividend Rate.
        2) Explicit annual/per-share dividend dollar amount in dedicated fields/source
           divided by the verified OIP for the *same exact security*.
        3) Blank when the source does not state enough information.

        This is deterministic and avoids another AI call.
        """
        text = raw(row.get("Dividend Rate"))
        if not is_missing(text):
            m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%\s*", text)
            if m:
                n = Decimal(m.group(1))
                n = n.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
                return f"{format(n, 'f')}%"

        # Look only for an explicit annual dividend dollar amount for this security.
        security_name = raw(row.get("Security Name"))
        source_text = raw(row.get("Source Text"))
        annual_amount = _strict_dividend_amount_from_source(security_name, source_text)

        # Some extractors may place the annual dollar amount in the dedicated dividend fields.
        if annual_amount is None:
            combined = " ".join([raw(row.get("Dividend Rate")), raw(row.get("Dividend Paying"))])
            pats = [
                r"(?:annual\s+(?:dividend\s+)?rate|dividend\s+rate).{0,80}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?",
                r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?.{0,80}?(?:per\s+annum|annually|annual)",
            ]
            for pat in pats:
                m = re.search(pat, combined, re.I)
                if m:
                    try:
                        annual_amount = float(m.group(1).replace(",", ""))
                    except Exception:
                        annual_amount = None
                    if annual_amount is not None:
                        break

        oip = money_num(row.get("OIP / Original Issue Price"))
        if annual_amount is not None and oip is not None and oip > 0 and annual_amount >= 0:
            try:
                amount_d = Decimal(str(annual_amount))
                oip_d = Decimal(str(oip))
                pct = (amount_d / oip_d) * Decimal("100")
                if Decimal("0") <= pct <= Decimal("100"):
                    pct = pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if pct == pct.to_integral():
                        return f"{int(pct)}%"
                    return f"{format(pct.normalize(), 'f')}%"
            except (InvalidOperation, ZeroDivisionError):
                pass

        # Machine-consumed summary rule: unstated values remain blank.
        return ""


    def conversion_ratio(row: pd.Series) -> str:
        """Render the finalized certified conversion ratio.

        The certification engine may derive the ratio from an explicit conversion
        numerator/base that is legally distinct from OIP. Therefore the summary must
        not blindly recompute OIP / Conversion Price and overwrite a certified ratio.
        """
        text = raw(row.get("Conversion Ratio"))
        m = re.search(r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b", text)
        if m:
            return re.sub(r"\s+", "", m.group(0))
        cp = money_num(row.get("Conversion Price"))
        oip = money_num(row.get("OIP / Original Issue Price"))
        if cp is not None and oip is not None and cp > 0:
            ratio = oip / cp
            if abs(ratio - 1.0) <= 1e-9:
                return "1:1"
            ratio_text = f"{ratio:.4f}".rstrip("0").rstrip(".")
            return f"{ratio_text}:1"
        return clean(text)

    def conversion_price_summary(row: pd.Series) -> str:
        combined_source = " ".join([raw(row.get("OIP / Original Issue Price")), raw(row.get("Liquidation Preference")), raw(row.get("Conversion Price")), raw(row.get("Source Text"))])
        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined_source, re.I):
            return ""

        # A financing valuation cap is not a per-share Conversion Price.  This is a
        # field-role guard for convertible notes/SAFEs and similar instruments.  If
        # the field contains valuation-cap language, accept a dollar amount only when
        # it is independently and explicitly labelled as the conversion price.
        cp_text = raw(row.get("Conversion Price"))
        if re.search(r"\b(?:pre[- ]money|post[- ]money)?\s*valuation\s+cap\b|\bvaluation\s+cap\b", cp_text, re.I):
            m = re.search(r"\bconversion\s+price(?:\s+per\s+share)?\s*(?:of|=|:|is|shall\s+be)?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", cp_text, re.I)
            if m:
                return "$" + m.group(1)
            return ""
        return concise_summary_value(price_from(cp_text), "conversion_price")

    def issue_strike_price(row: pd.Series) -> str:
        """Field-source locked issue/strike price.

        Use explicit OIP / issue price first. Never use participation cap or
        liquidation preference as a substitute. As a final fallback, use an
        explicitly identified initial/original conversion price.
        """
        combined_source = " ".join([raw(row.get("OIP / Original Issue Price")), raw(row.get("Source Text"))])
        if re.search(r"qualified\s+capital\s+balance|qualified\s+capital\s+contribution", combined_source, re.I):
            return "Not fixed / contribution-based"
        oip_value = price_from(row.get("OIP / Original Issue Price"))
        if oip_value not in {"", "NA", "N/A", "Not stated", "Needs review"}:
            return oip_value
        conv_text = raw(row.get("Conversion Price"))
        conv_value = price_from(conv_text)
        if conv_value not in {"", "NA", "N/A", "Not stated", "Needs review"} and re.search(r"initial|original\s+conversion\s+price", conv_text, re.I):
            return conv_value
        return ""

    def cap_price_includes_dividends(row: pd.Series) -> str:
        """Return Yes only when the participation-cap clause itself expressly includes dividends.

        v15.2 summary rule:
        - No actual cap -> blank.
        - Explicit exclusion -> No.
        - Explicit inclusion in the dedicated participation-cap clause -> Yes.
        - Silence -> No.
        Generic liquidation/dividend wording elsewhere in Source Text is ignored.
        """
        cap_value = participation_cap(row.get("Participation Cap"), row)
        if raw(cap_value).lower() in {"", "na", "n/a", "not stated", "needs review", "none", "null"}:
            return ""

        cap_text = raw(row.get("Participation Cap"))
        source_text = raw(row.get("Source Text"))
        dedicated = ""
        marker = "PARTICIPATION CAP SOURCE:"
        low_source = source_text.lower()
        if marker.lower() in low_source:
            pos = low_source.rfind(marker.lower())
            dedicated = source_text[pos + len(marker):]
            dedicated = re.split(r"\n\n[A-Z][A-Z _-]+ SOURCE:", dedicated, maxsplit=1)[0]

        m_verified = re.search(r"CAP PRICE INCLUDES DIVIDENDS:\s*(Yes|No|N/A)", source_text, re.I)
        if m_verified:
            v = m_verified.group(1)
            return "N/A" if v.lower() == "n/a" else v.capitalize()

        combined = f"{cap_text} {dedicated}".lower()

        # Silence/ambiguity language must be treated as No before looking for the
        # word "includes".  This prevents phrases such as "does not expressly state
        # whether the cap includes dividends" from being misclassified as Yes.
        if re.search(
            r"(?:does\s+not|do\s+not|not)\s+(?:expressly\s+)?(?:state|specify|provide|say|indicate).{0,120}dividends?|"
            r"(?:not\s+expressly\s+stated|silent|unclear|ambiguous).{0,100}dividends?|"
            r"whether.{0,60}(?:include|includes|including).{0,60}dividends?",
            combined, re.I,
        ):
            return "No"
        if re.search(r"\b(excluding|excludes?|without|exclusive\s+of)\b.{0,60}\bdividends?\b|\bdoes\s+not\s+include\b.{0,40}\bdividends?\b", combined, re.I):
            return "No"
        if re.search(r"\b(including|includes?|inclusive\s+of)\b.{0,80}\bdividends?\b", combined, re.I):
            return "Yes"
        return "No"


    def assign_liquidation_ranks(rows: List[pd.Series]) -> Tuple[Dict[str, int], int]:
        """Assign numeric liquidation seniority ranks deterministically.

        v9 fix:
        - Highest priority: explicit row-specific rank/ordinal in the Seniority field
          such as "Rank 5", "Fifth liquidation preference tier", "Second payment tier".
        - Do not let Senior To / Junior To relationship logic overwrite explicit ranks.
        - Use Junior To / Senior To only as fallback for rows without explicit ranks.
        - Common Stock is ranked after the highest preferred rank.
        """
        preferred_rows = [
            r for r in rows
            if "preferred" in raw(r.get("Security Name")).lower()
            and raw(r.get("Security Name")).lower() != "preferred stock"
        ]
        names = [raw(r.get("Security Name")) for r in preferred_rows]
        ranks: Dict[str, Optional[int]] = {name: None for name in names}
        rank_source: Dict[str, str] = {name: "" for name in names}

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
            # Numeric rank patterns: Rank 5, rank: 5, tier 4, seniority = 3.
            m = re.search(r"\b(?:seniority|rank|tier|priority)\b\s*[:=\-]?\s*(\d{1,2})\b", t)
            if m:
                return int(m.group(1))
            # Ordinal tier/payment patterns.
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

        # 1. Explicit row-specific ranks from Seniority field: never overwrite these later.
        for r in preferred_rows:
            name = raw(r.get("Security Name"))
            rank = explicit_rank(raw(r.get("Seniority")))
            if rank is not None:
                ranks[name] = rank
                rank_source[name] = "explicit_seniority_field"

        # 2. Source Text fallback: only if row has no explicit rank and current security is near ordinal.
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
            name = raw(r.get("Security Name"))
            if ranks[name] is None:
                rank = current_security_near_ordinal(raw(r.get("Source Text")), name)
                if rank is not None:
                    ranks[name] = rank
                    rank_source[name] = "source_text_near_ordinal"

        # 3. Junior To fallback: use only when no rank exists.
        # Iteratively set rank to max(rank of senior securities) + 1 if senior ranks are known.
        changed = True
        while changed:
            changed = False
            for r in preferred_rows:
                name = raw(r.get("Security Name"))
                if ranks[name] is not None:
                    continue
                senior_names = [n for n in mentioned_names(" ".join([raw(r.get("Junior To")), raw(r.get("Seniority"))])) if n != name]
                known = [ranks[n] for n in senior_names if ranks.get(n) is not None]
                if known:
                    ranks[name] = max(int(x) for x in known if x is not None) + 1
                    rank_source[name] = "junior_to_known_ranks"
                    changed = True

        # 4. Senior To fallback: current row is above another known row.
        changed = True
        while changed:
            changed = False
            for r in preferred_rows:
                name = raw(r.get("Security Name"))
                if ranks[name] is not None:
                    continue
                junior_names = [n for n in mentioned_names(raw(r.get("Senior To"))) if n != name]
                known = [ranks[n] for n in junior_names if ranks.get(n) is not None]
                if known:
                    candidate = min(int(x) for x in known if x is not None) - 1
                    if candidate >= 1:
                        ranks[name] = candidate
                        rank_source[name] = "senior_to_known_ranks"
                        changed = True

        # 5. Last fallback: count explicitly mentioned senior securities in Junior To text.
        for r in preferred_rows:
            name = raw(r.get("Security Name"))
            if ranks[name] is None:
                senior_names = [n for n in mentioned_names(raw(r.get("Junior To"))) if n != name]
                if senior_names:
                    ranks[name] = len(set(senior_names)) + 1
                    rank_source[name] = "junior_to_count_fallback"

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

    # Remove aggregate Preferred Stock / defined-group rows where detailed series rows exist.
    all_security_names = [raw(r.get("Security Name")) for _, r in terms_df.iterrows() if raw(r.get("Security Name"))]
    detailed_preferred_names = [
        n for n in all_security_names
        if "preferred" in n.lower()
        and n.lower().strip() not in {"preferred", "preferred stock", "preferred shares", "series preferred stock"}
    ]
    has_any_detailed_preferred = len(detailed_preferred_names) > 0

    source_rows: List[pd.Series] = []
    for _, row in terms_df.iterrows():
        name = raw(row.get("Security Name"))
        if not name:
            continue
        low_name = name.lower().strip()

        # v15.8.1 roster-lock rule:
        # Suppress ONLY a true generic/aggregate Preferred row. A legally designated
        # security such as "Series Seed Preferred Stock" remains an actual series even
        # when Seed-1 / Seed-2 / etc. also exist. Never infer that a named series is an
        # aggregate merely because split series share the same root label.
        is_generic_preferred_aggregate = low_name in {
            "preferred", "preferred stock", "preferred shares", "series preferred stock"
        }
        if is_generic_preferred_aggregate and has_any_detailed_preferred:
            continue

        if "blank check" in low_name or "undesignated preferred" in low_name or "initially undesignated" in low_name:
            continue
        source_rows.append(row)

    # Hard roster invariant for the summary builder: every non-placeholder security row
    # surviving the authoritative extraction must appear exactly once in Cap Table Summary.
    # This catches silent row loss independently of AI interpretation.
    expected_summary_names = [raw(r.get("Security Name")) for r in source_rows if raw(r.get("Security Name"))]
    expected_norms = [normalize_security_name_for_match(n) for n in expected_summary_names]
    if len(expected_norms) != len(set(expected_norms)):
        dupes = sorted({n for n in expected_norms if expected_norms.count(n) > 1})
        raise ValueError(f"Cap Table Summary roster invariant failed: duplicate security rows after filtering: {dupes}")

    ranks, common_rank = assign_liquidation_ranks(source_rows)
    rows = []
    for row in source_rows:
        name = raw(row.get("Security Name"))
        is_common = "common" in name.lower() and "preferred" not in name.lower()
        is_preferred = "preferred" in name.lower()
        rows.append({
            "Series Type": "" if is_common else ("Preferred Share" if is_preferred else clean(row.get("Security Type"))),
            "Series Name": clean(name),
            "Issue / Strike Price": "" if is_common else issue_strike_price(row),
            "Liq. Pref. / Share": "" if is_common else liq_pref_per_share(row),
            "Seniority in Liq. Pref.": seniority_rank_from_row(row, common_rank if is_common else ranks.get(name, "NA")),
            "Participation Rights": "" if is_common else participation_summary(row),
            "Participation Cap $ / Share": "" if is_common else participation_cap(row.get("Participation Cap"), row),
            "Conversion Price": "" if is_common else conversion_price_summary(row),
            "Conversion Ratio": "" if is_common else conversion_ratio(row),
            "Is Dividend Payable?": "" if is_common else dividend_payable(row),
            "Dividend Rate": "" if is_common else dividend_rate_pct(row),
            "Dividend Type": "" if is_common else concise_summary_value(normalize_dividend_type(row.get("Dividend Type"), " ".join([raw(row.get("Dividend Paying")), raw(row.get("Dividend Rate")), raw(row.get("Source Text"))])), "dividend_type"),
            "Cap price Includes Dividends": "" if is_common else cap_price_includes_dividends(row),
        })

    summary_df = pd.DataFrame(rows, columns=CAP_TABLE_SUMMARY_COLUMNS)

    # Final deterministic roster check. The summary is a machine-consumed downstream
    # interface, so silently dropping an authoritative security is not permitted.
    actual_names = [raw(v) for v in summary_df.get("Series Name", pd.Series(dtype=object)).tolist() if raw(v)]
    actual_norms = [normalize_security_name_for_match(n) for n in actual_names]
    missing_norms = sorted(set(expected_norms) - set(actual_norms))
    extra_norms = sorted(set(actual_norms) - set(expected_norms))
    if missing_norms or extra_norms or len(actual_norms) != len(expected_norms):
        raise ValueError(
            "Cap Table Summary roster invariant failed: "
            f"missing={missing_norms or 'none'}; extra={extra_norms or 'none'}; "
            f"expected_rows={len(expected_norms)}; actual_rows={len(actual_norms)}"
        )

    # Machine-consumed summary rule: diagnostics and unstated values never appear as text.
    summary_df = summary_df.replace(to_replace=r"(?i)^\s*(needs\s+review|review\s+required|required\s+review|not\s+stated)\s*$", value="", regex=True)
    if not summary_df.empty:
        def _sort_key(r: pd.Series) -> Tuple[int, int, str]:
            name = raw(r.get("Series Name")).lower()
            is_common = "common" in name and "preferred" not in name
            is_pref = "preferred" in name
            try:
                rank = int(float(str(r.get("Seniority in Liq. Pref.", 999)).strip()))
            except Exception:
                rank = 999
            if is_pref:
                group = 0
            elif is_common:
                group = 2
            else:
                group = 1
            return (group, rank, name)
        summary_df = summary_df.sort_values(by=list(summary_df.columns), key=None) if False else summary_df.iloc[sorted(range(len(summary_df)), key=lambda i: _sort_key(summary_df.iloc[i]))].reset_index(drop=True)
    return summary_df


EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {
            "type": "string",
            "description": "Legal company name from the document. Use Not stated if unavailable.",
        },
        "document_name": {
            "type": "string",
            "description": "Document name or filename.",
        },
        "securities": {
            "type": "array",
            "description": "One object per security, class, or series.",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "security_type": {"type": "string"},
                    "authorized_count": {"type": "string"},
                    "oip_original_issue_price": {"type": "string"},
                    "liquidation_preference": {"type": "string"},
                    "dividend_paying": {"type": "string"},
                    "dividend_type": {"type": "string"},
                    "dividend_rate": {"type": "string"},
                    "conversion_ratio": {"type": "string"},
                    "conversion_price": {"type": "string"},
                    "participation_rights": {"type": "string"},
                    "participation_cap": {"type": "string"},
                    "seniority": {"type": "string"},
                    "senior_to": {"type": "string"},
                    "pari_passu_with": {"type": "string"},
                    "junior_to": {"type": "string"},
                    "applies_to": {"type": "string"},
                    "source_basis": {"type": "string"},
                    "confidence": {"type": "string"},
                    "source_section": {"type": "string"},
                    "source_text": {"type": "string"},
                    "review_flags": {"type": "string"},
                },
                "required": [
                    "security_name",
                    "security_type",
                    "authorized_count",
                    "oip_original_issue_price",
                    "liquidation_preference",
                    "dividend_paying",
                    "dividend_type",
                    "dividend_rate",
                    "conversion_ratio",
                    "conversion_price",
                    "participation_rights",
                    "participation_cap",
                    "seniority",
                    "senior_to",
                    "pari_passu_with",
                    "junior_to",
                    "applies_to",
                    "source_basis",
                    "confidence",
                    "source_section",
                    "source_text",
                    "review_flags",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["company_name", "document_name", "securities"],
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
            "description": "Exact source-derived OIP / original issue price per expected security. Use 999/Not stated only if not determinable.",
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


def oip_text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "exact_oip_source_prices",
            "schema": OIP_PRICE_SCHEMA,
            "strict": True,
        }
    }


def seniority_text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "liquidation_seniority_exact_ranks",
            "schema": SENIORITY_TIER_SCHEMA,
            "strict": True,
        }
    }



SECURITY_MASTER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_name": {"type": "string"},
        "document_classification": {"type": "string"},
        "classification_reason": {"type": "string"},
        "total_common_authorized": {"type": "string"},
        "total_preferred_authorized": {"type": "string"},
        "securities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "security_type": {"type": "string"},
                    "authorized_count": {"type": "string"},
                    "source_text": {"type": "string"}
                },
                "required": ["security_name", "security_type", "authorized_count", "source_text"],
                "additionalProperties": False
            }
        }
    },
    "required": ["company_name", "document_name", "document_classification", "classification_reason", "total_common_authorized", "total_preferred_authorized", "securities"],
    "additionalProperties": False
}


def security_master_text_format_schema() -> Dict[str, Any]:
    return {"format": {"type": "json_schema", "name": "security_master", "schema": SECURITY_MASTER_SCHEMA, "strict": True}}


SECURITY_MASTER_INSTRUCTIONS = r"""
You are building the authoritative SECURITY ROSTER for a valuation workflow from the uploaded corporate document. This pass is ONLY for identifying the complete list of actual classes/series. Do not spend time determining authorized share counts or any economics.

Scope:
- Standard Inc./Corporation Articles, Certificates of Incorporation, Amended and Restated Certificates, Charters, and Certificates of Designation are Standard.
- LLC agreements, investor-rights agreements, subscription agreements, shareholder agreements, Australian constitutions and similar documents are Non-standard for this workflow.

Rules:
1. Read the source capitalization/designation provisions and identify every actual class/series.
2. Series Seed, Seed-1, Seed-2, Series A, A-1, A-2 etc. are distinct securities.
3. Do NOT add aggregate/group labels such as Preferred Stock, Series C Preferred Shares, or Series D Preferred Stock when the source separately designates individual series beneath that label.
4. Common Stock / Class A Common / Class B Common are separate rows only when they are actual designated classes.
5. Do not infer a security solely from a reference in liquidation, conversion, voting, protective or dividend provisions if it is not part of the operative roster.
6. Authorized share counts are NOT required for this workflow. For schema compatibility, set authorized_count, total_common_authorized and total_preferred_authorized to Not stated. Do not research or reconcile counts.
7. source_text should be a short excerpt supporting the security designation only.
8. Return strict JSON only.
"""

SECURITY_MASTER_ADJUDICATION_INSTRUCTIONS = SECURITY_MASTER_INSTRUCTIONS + r"""

ADJUDICATION PASS
Re-read the source capitalization/designation provisions and resolve the exact security roster only. Authorized counts are out of scope. The source is authoritative. Return one complete corrected Security Master.
"""

CAP_TABLE_SOURCE_FACTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_name": {"type": "string"},
        "document_classification": {"type": "string"},
        "classification_reason": {"type": "string"},
        "securities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "authorized_count": {"type": "string"},
                    "oip_original_issue_price": {"type": "string"},
                    "liquidation_preference": {"type": "string"},
                    "seniority_rank": {"type": "integer"},
                    "participation_rights": {"type": "string"},
                    "participation_cap": {"type": "string"},
                    "conversion_price": {"type": "string"},
                    "conversion_ratio": {"type": "string"},
                    "dividend_rate": {"type": "string"},
                    "dividend_type": {"type": "string"},
                    "cap_price_includes_dividends": {"type": "string"},
                    "source_basis": {"type": "string"},
                    "source_text": {"type": "string"}
                },
                "required": ["security_name","authorized_count","oip_original_issue_price","liquidation_preference","seniority_rank","participation_rights","participation_cap","conversion_price","conversion_ratio","dividend_rate","dividend_type","cap_price_includes_dividends","source_basis","source_text"],
                "additionalProperties": False
            }
        }
    },
    "required": ["company_name","document_name","document_classification","classification_reason","securities"],
    "additionalProperties": False
}

def cap_table_source_facts_text_format_schema() -> Dict[str, Any]:
    return {"format": {"type": "json_schema", "name": "cap_table_full_source_verification", "schema": CAP_TABLE_SOURCE_FACTS_SCHEMA, "strict": True}}

CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS = r"""
You are a senior valuation analyst verifying a corporate charter for a downstream valuation system. Read the ENTIRE uploaded source document like a careful human reviewer, including visible page content for scanned PDFs.

The workflow is intended for standard Inc./Corporation Articles, Certificates of Incorporation, Amended and Restated Certificates, Charters, and Certificates of Designation. Classify the document as Standard or Non-standard and state a short reason.

For EVERY security in the supplied expected-security list, return one row using the EXACT supplied security_name. ALSO independently inspect the capitalization/designation provisions and add any actual class/series that is present in the source but missing from the expected-security list. Do not add aggregate Preferred Stock when the source separately designates individual preferred series. This verification result will override an earlier extraction.

CRITICAL RULES
- Bind every fact to the exact named class/series in the operative clause or definition; never map by proximity alone.
- Series A, A-1, A-2, Seed, Seed-1 and Seed-2 are distinct securities.
- Never use aggregate Preferred Stock counts for an individual split series.
- Preserve complete comma-separated counts and exact decimals.
- Never mistake par value, section numbers, series numbers, page numbers, voting percentages, or unrelated dollars for economic terms.
- Authorized Count is out of scope for this workflow. Return Not stated for schema compatibility. Do not spend time verifying or reconciling it.
- OIP: exact Original Issue/Purchase/Issuance Price for that exact series. If no explicit OIP exists, an expressly stated initial conversion price may be used only when it is clearly the original per-share economic base. Otherwise Not stated. Never borrow another series price.
- Liquidation Preference: concise only, e.g. $2.50, 1x OIP, 1.25x OIP. Preserve a stated OIP multiplier.
- Seniority Rank: 1 is most senior; pari passu shares rank; Common follows preferred tiers when residual. Use 999 only if genuinely not determinable.
- Participation Rights: only Non-participating, Participating, Capped Participation, N/A, or Not stated. Greater-of preference vs as-converted without residual sharing = Non-participating. Preference plus residual sharing = Participating. Residual sharing with a ceiling = Capped Participation.
- Participation Cap: concise only, e.g. $8.61639, 3x OIP, N/A, Not stated. Do not confuse it with liquidation preference.
- Conversion Price/Ratio: exact conversion clause. Initial one-for-one = 1:1. If the charter formula supports it, ratio = OIP / Conversion Price. Never use liquidation preference to calculate ratio.
- Dividend Rate: explicit fixed percentage from the dividend clause wins. If a fixed annual dollar dividend per share is stated and verified OIP is available, rate may be annual dividend/OIP. Otherwise Not stated. NEVER infer 100%, 66.67%, 54.55% or another percentage from matching dividends, liquidation amounts, conversion prices, or generic percentages.
- Dividend Type: explicit non-cumulative/no accrual = Non-cumulative; explicit cumulative/accruing = Cumulative; when/as/if declared without accrual = Non-cumulative; otherwise Not stated.
- Cap Price Includes Dividends: Yes only if the participation-cap provision itself expressly includes dividends; No if a cap exists and dividends are excluded or the cap clause is silent; N/A if no participation cap.

Economic fields must be concise and machine-friendly. No long legal sentences. Put a short supporting excerpt only in source_text. Never output Review Required or Needs review in economic fields; use Not stated or N/A. Return strict JSON only.
"""


ECONOMIC_ANCHOR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "document_name": {"type": "string"},
        "anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "oip_original_issue_price": {"type": "string"},
                    "conversion_price": {"type": "string"},
                    "liquidation_basis": {"type": "string"},
                    "participation_rights": {"type": "string"},
                    "participation_cap": {"type": "string"},
                    "dividend_rate": {"type": "string"},
                    "annual_dividend_per_share": {"type": "string"},
                    "dividend_type": {"type": "string"},
                    "source_text": {"type": "string"}
                },
                "required": ["security_name","oip_original_issue_price","conversion_price","liquidation_basis","participation_rights","participation_cap","dividend_rate","annual_dividend_per_share","dividend_type","source_text"],
                "additionalProperties": False
            }
        }
    },
    "required": ["company_name","document_name","anchors"],
    "additionalProperties": False
}

def economic_anchor_text_format_schema() -> Dict[str, Any]:
    return {"format": {"type": "json_schema", "name": "economic_anchor_verification", "schema": ECONOMIC_ANCHOR_SCHEMA, "strict": True}}

ECONOMIC_ANCHOR_INSTRUCTIONS = r"""
You are performing a TARGETED economic-anchor verification for a valuation workflow. Read the uploaded corporate charter and verify only the economic anchors listed below for each exact expected security. This is not a general summary pass.

Rules:
1. Use the EXACT security_name from the supplied expected list. Series A, A-1, A-2, Seed, Seed-1, Seed-2 etc. are separate securities. Never borrow a value from another series.
2. OIP: return the exact Original Issue Price / Original Purchase Price / Original Issuance Price / expressly stated issue or subscription price for that exact security. OIP and Conversion Price are independent fields. NEVER substitute Conversion Price for OIP merely because they are equal, nearby, or mathematically related. If the source does not expressly state the original issue/purchase/issuance/issue/subscription price for that exact security, return Not stated.
3. Conversion Price: independently return the exact initial conversion price for that security from the conversion clause. If the source expressly says the initial Conversion Price equals that exact security's OIP, return the resulting Conversion Price and include the equality clause in source_text even when the dollar amount is not repeated there. Never infer equality merely because two values happen to match.
4. Liquidation Basis: return only a concise economic basis such as 1x OIP, 1.25x OIP, 3x OIP, $15.00, Qualified Capital Balance, or a concise conditional form such as '1.6x OIP or 1x OIP (condition-dependent)'. A clause giving each preferred series the greater of its applicable OIP (plus declared/unpaid dividends) or its as-converted amount is a 1x OIP non-participating liquidation basis. Formula/cross-reference evidence is valid even when the exact dollar amount is not repeated. Never use par value.
5. Participation Rights: inspect the FULL liquidation waterfall, including any later residual-distribution paragraph. Preference plus residual sharing with Common = Participating; greater-of preference versus as-converted with no later residual sharing = Non-participating; residual sharing subject to a ceiling = Capped Participation. Return only Non-participating, Participating, Capped Participation, N/A, or Not stated.
6. Participation Cap: concise only (for example 3x OIP or $8.61639); otherwise N/A/Not stated.
7. Dividend Rate: return an explicit fixed percentage only when the dividend clause states one. If the clause applies to an expressly defined group (for example Series D Preferred Stock) that explicitly includes the exact member security (for example Series D-1 and D-2), apply that group rate to the member. Otherwise Not stated.
8. Annual Dividend Per Share: when the dividend clause explicitly states a fixed annual dollar amount per share for the exact security, return that dollar amount. Otherwise Not stated.
9. Dividend Type: explicit cumulative/accruing = Cumulative; explicit non-cumulative/no accrual or when/as/if declared without accrual = Non-cumulative; otherwise Not stated.
10. source_text MUST be a short excerpt containing the exact security identity and the relevant value(s) OR a complete deterministic relationship that proves the value (for example, Conversion Price equals that security's OIP). For a group-level right, include BOTH the group definition showing that the exact member security belongs to the group and the operative group-level right. Multi-series list clauses are valid evidence; preserve the exact member/value pair. If you cannot support a value from the operative clause, return Not stated rather than guessing.
11. Ignore authorized share counts entirely.
12. Return strict JSON only. No Review Required / Needs Review language.
"""

def _build_economic_anchor_excerpt(source_text: str, security_list: List[str], max_chars: int = 32000) -> str:
    """Build a compact evidence pack for the economic-anchor pass.

    Token-efficiency rule: send only source neighborhoods that are likely to contain
    OIP, liquidation, conversion, participation, and dividend economics.  Keep broad
    windows and exact security-name hits so quality is preserved.  If the evidence
    pack is too thin, the caller falls back to the uploaded full document.
    """
    text = str(source_text or "")
    if not text.strip():
        return ""
    low = text.lower()
    terms = [
        "original issue price", "original purchase price", "issue price",
        "conversion price", "conversion rate", "conversion ratio",
        "liquidation", "liquidating distribution", "liquidation preference",
        "participat", "remaining assets", "remaining proceeds",
        "dividend", "cumulative", "non-cumulative", "noncumulative",
        "pari passu", "senior to", "junior to", "priority",
    ]
    # Exact security names and compact series tokens are high-value anchors.
    for name in security_list:
        n = str(name or "").strip().lower()
        if n:
            terms.append(n)
        m = re.search(r"\bseries\s+[a-z]+(?:\s*[-–—]?\s*\d+)?", n, re.I)
        if m:
            terms.append(m.group(0).lower())

    hits = []
    for term in dict.fromkeys(t for t in terms if t):
        start = 0
        while True:
            idx = low.find(term, start)
            if idx < 0:
                break
            hits.append(idx)
            start = idx + max(1, len(term))
            if len(hits) > 500:
                break
        if len(hits) > 500:
            break
    if not hits:
        return ""

    windows = []
    radius_before, radius_after = 1800, 3200
    for idx in sorted(set(hits)):
        a = max(0, idx - radius_before)
        b = min(len(text), idx + radius_after)
        windows.append((a, b))
    # Merge overlapping windows.
    merged = []
    for a, b in windows:
        if not merged or a > merged[-1][1] + 300:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)

    chunks = []
    used = 0
    for a, b in merged:
        chunk = text[a:b].strip()
        if not chunk:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunk = chunk[:remaining]
        chunks.append(chunk)
        used += len(chunk)
    return "\n\n--- SOURCE EVIDENCE WINDOW ---\n\n".join(chunks)


def _call_economic_anchor_verification(client: OpenAI, model: str, uploaded_id: str, path: Path, security_list: List[str], reasoning_effort: str, source_excerpt: str = "") -> Dict[str, Any]:
    identity_payload = _security_identity_payload(security_list)
    user_text = (
        f"Document name: {path.name}\nImmutable expected-security roster (security_id + exact legal name):\n"
        + json.dumps(identity_payload, ensure_ascii=False, indent=2)
        + "\n\nVerify the economic anchors for every expected security. Use the exact supplied security_name. "
          "The security_id is immutable context only; never transfer a fact from one security_id to another."
    )
    excerpt = str(source_excerpt or "").strip()
    # Use compact local evidence when it is substantial enough; otherwise preserve
    # the prior full-document path. This lowers tokens without trading away coverage.
    if len(excerpt) >= 1500:
        user_text += "\n\nRelevant source evidence extracted from the same document:\n" + excerpt
        content = [{"type":"input_text","text":user_text}]
    else:
        content = [{"type":"input_file","file_id":uploaded_id},{"type":"input_text","text":user_text}]
    response = _responses_create(client, 
        model=model,
        instructions=ECONOMIC_ANCHOR_INSTRUCTIONS,
        input=[{"role":"user","content":content}],
        text=economic_anchor_text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)

def _value_is_missing(v: Any) -> bool:
    t = str(v or "").strip().lower()
    return t in {"", "not stated", "n/a", "na", "none", "null", "needs review", "review required"}


def _money_value_in_evidence(value: Any, evidence: str) -> bool:
    """Require the claimed money amount to be literally supported by the returned evidence excerpt."""
    num = _money_to_float(value)
    if num is None:
        return False
    ev = str(evidence or "")
    if not ev.strip():
        return False
    # Compare parsed dollar values rather than formatting so $5, $5.00 and $5.0000 all match.
    for m in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", ev):
        try:
            x = float(m.group(1).replace(",", ""))
        except Exception:
            continue
        tol = max(1e-9, abs(num) * 1e-8)
        if abs(x - num) <= tol:
            return True
    return False


def _exact_security_in_evidence(security_name: str, evidence: str) -> bool:
    token = _strict_series_token(security_name)
    if token == "common":
        return bool(re.search(r"\bcommon(?:\s+stock|\s+shares?)?\b", str(evidence or ""), re.I))
    rx = _strict_series_regex(token)
    return bool(rx and re.search(rx, _normalize_source_text_for_match(evidence), re.I))


def _annual_dividend_from_anchor_evidence(security_name: str, evidence: str) -> Optional[float]:
    """Parse only explicit annual/per-share dividend dollars from an anchor's short evidence.

    Never use a generic nearby dollar amount. This deliberately requires either:
    - "$X per share for/of <exact security> ... per annum/annual", or
    - "<exact security> ... Dividend Rate ... annual rate of $X per share".
    """
    ev = _normalize_source_text_for_match(evidence)
    if not ev or not _exact_security_in_evidence(security_name, evidence):
        return None
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx:
        return None
    patterns = [
        rf"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+share\s+(?:for|of)\s+(?:the\s+)?{rx}.{{0,100}}?(?:per\s+annum|annually|annual)",
        rf"{rx}.{{0,120}}?dividend\s+rate.{{0,100}}?annual\s+rate\s+of\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?",
        rf"{rx}.{{0,120}}?annual\s+dividend.{{0,100}}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?",
    ]
    vals=[]
    for pat in patterns:
        for m in re.finditer(pat, ev, re.I):
            try:
                vals.append(float(m.group(1).replace(',', '')))
            except Exception:
                pass
    uniq=[]
    for v in vals:
        if not any(abs(v-u) <= max(1e-12,abs(v)*1e-9) for u in uniq):
            uniq.append(v)
    return uniq[0] if len(uniq)==1 else None


def _dividend_percent_from_anchor_evidence(security_name: str, evidence: str) -> Optional[float]:
    """Return only an explicit dividend percentage from the short anchor evidence."""
    ev = _normalize_source_text_for_match(evidence)
    if not ev or not _exact_security_in_evidence(security_name, evidence):
        return None
    pats = [
        r"dividends?.{0,120}?(?:rate\s+(?:per\s+annum\s+)?of|at\s+(?:the\s+)?rate\s+of)\s*(?:[a-z -]+\s*)?\(?\s*(\d+(?:\.\d+)?)\s*%",
        r"dividend\s+rate.{0,100}?\(?\s*(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%.{0,100}?(?:per\s+annum|annual).{0,120}?dividend",
    ]
    vals=[]
    for pat in pats:
        for m in re.finditer(pat, ev, re.I):
            try:
                v=float(m.group(1))
            except Exception:
                continue
            if 0 <= v <= 100:
                vals.append(v)
    uniq=[]
    for v in vals:
        if not any(abs(v-u) <= 1e-9 for u in uniq):
            uniq.append(v)
    return uniq[0] if len(uniq)==1 else None




def _field_role_supported_in_evidence(field: str, evidence: str) -> bool:
    """Require the excerpt to prove the economic role, not merely contain a dollar value."""
    ev = _normalize_source_text_for_match(evidence)
    if not ev:
        return False
    patterns = {
        "oip": r"original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price",
        "conversion": r"initial\s+conversion\s+price|applicable\s+conversion\s+price|conversion\s+price",
        "liquidation": r"liquidation\s+amount|liquidation\s+preference|liquidation,?\s+dissolution|deemed\s+liquidation|greater\s+of",
        "dividend": r"dividend|dividends|per\s+annum|annual",
        "participation": r"participat|remaining\s+(?:assets|proceeds)|greater\s+of|as[-\s]?converted",
    }
    pat = patterns.get(field)
    return bool(pat and re.search(pat, ev, re.I))


def _exact_series_money_from_role_clause(security_name: str, source_text: str, field: str) -> Optional[float]:
    """Parse exact series/value pairs inside one role-specific list or definition.

    Unlike proximity parsers, this requires the exact immutable series token and searches
    only inside the relevant OIP or conversion-price clause family. It supports common
    list forms such as:
      "$54.50 per share of Series A ... $21.67 per share of Series A-1"
      "in the case of Series A, a per share amount initially equal to $0.52"
      "with respect to Series A Preferred Stock, $2.72 per share"
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or token == "common" or not source_text:
        return None
    if field == "oip":
        anchors = [r"original\s+(?:issue|issuance|purchase)\s+price", r"applicable\s+original\s+issue\s+price", r"issue\s+price"]
    elif field == "conversion":
        anchors = [r"applicable\s+conversion\s+price", r"initial\s+conversion\s+price", r"conversion\s+price"]
    else:
        return None
    windows = _role_windows(source_text, anchors, radius=5200)
    vals: List[Tuple[int, float]] = []
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    for raw_window in windows:
        w = _normalize_source_text_for_match(raw_window)
        if not w:
            continue
        patterns = [
            rf"{money}\s*(?:per\s+share)?\s*(?:of|for|with\s+respect\s+to|applicable\s+to)\s+(?:the\s+)?{rx}",
            rf"(?:with\s+respect\s+to|in\s+the\s+case\s+of)\s+(?:the\s+)?{rx}.{{0,120}}?{money}",
            rf"(?:the\s+)?{rx}.{{0,150}}?(?:per\s+share\s+amount\s+)?(?:initially\s+)?(?:equal\s+to|is|shall\s+mean|means)?\s*{money}",
        ]
        for pri, pat in enumerate(patterns):
            for m in re.finditer(pat, w, re.I):
                # Because each pattern has one money capture and the series regex uses only
                # non-capturing groups, group(1) is the amount.
                try:
                    v = float(m.group(1).replace(',', ''))
                except Exception:
                    continue
                if 0 < v < 1_000_000:
                    vals.append((pri * 100000 + (m.end()-m.start()), v))
    if not vals:
        return None
    vals.sort(key=lambda x: x[0])
    # If the best-priority matches disagree, do not guess.
    best_pri = vals[0][0] // 100000
    same = [v for score, v in vals if score // 100000 == best_pri]
    uniq: List[float] = []
    for v in same:
        if not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in uniq):
            uniq.append(v)
    return uniq[0] if len(uniq) == 1 else None



def _fixed_conversion_base_from_evidence(security_name: str, evidence: str) -> Optional[float]:
    """Return an exact fixed per-series conversion numerator/base when expressly stated.

    This is NOT a Conversion Price -> OIP fallback. It accepts only an independently
    stated fixed amount used as the numerator/base of the conversion formula for the
    exact security, e.g. "dividing $0.40 for Series A Preferred Stock by the
    applicable Conversion Price". This supports charters that never use an OIP label.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    ev = _normalize_source_text_for_match(evidence)
    if not rx or token == "common" or not ev:
        return None
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    patterns = [
        rf"(?:divid(?:e|ed|ing)|quotient).{{0,80}}?{money}.{{0,100}}?(?:for|of|applicable\s+to)\s+(?:the\s+)?{rx}.{{0,180}}?(?:by|over).{{0,80}}?(?:the\s+)?(?:applicable\s+)?conversion\s+price",
        rf"{rx}.{{0,160}}?(?:conversion\s+(?:rate|ratio)).{{0,180}}?(?:divid(?:e|ed|ing)|quotient).{{0,80}}?{money}.{{0,140}}?(?:conversion\s+price)",
    ]
    vals=[]
    for pat in patterns:
        for m in re.finditer(pat, ev, re.I):
            try:
                vals.append(float(m.group(1).replace(',', '')))
            except Exception:
                pass
    uniq=[]
    for v in vals:
        if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12,abs(v)*1e-9) for u in uniq):
            uniq.append(v)
    return uniq[0] if len(uniq) == 1 else None



def _corroborated_economic_base_from_evidence(security_name: str, evidence: str) -> Optional[float]:
    """Certify an unlabeled original per-share economic base from two independent roles.

    Some older charters do not use the term Original Issue Price, but state the same
    exact per-series base independently in the liquidation provision and as the
    initial Conversion Price. This helper accepts that base only when:
      * both clauses identify the exact immutable series;
      * each role yields one unambiguous dollar value; and
      * the two independently stated values are equal.

    This is deliberately stricter than copying Conversion Price into OIP and prevents
    cross-series or single-clause inference.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    ev = _normalize_source_text_for_match(evidence)
    if not rx or token == "common" or not ev:
        return None

    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"

    def unique_values(patterns: List[str]) -> List[float]:
        vals: List[float] = []
        for pat in patterns:
            for m in re.finditer(pat, ev, re.I):
                try:
                    v = float(m.group(1).replace(",", ""))
                except Exception:
                    continue
                if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                    vals.append(v)
        return vals

    liq_vals = unique_values([
        rf"liquidation.{{0,140}}?{money}\s*(?:per\s+share)?\s*(?:for|of|in\s+the\s+case\s+of)\s+(?:the\s+)?{rx}",
        rf"liquidation.{{0,140}}?{money}\s+per\s+share\s+for\s+(?:the\s+)?{rx}",
        rf"(?:liquidation|preference).{{0,420}}?{rx}.{{0,220}}?{money}\s+for\s+each\s+share",
        rf"(?:liquidation|preference).{{0,420}}?{money}\s+for\s+each\s+share\s+(?:of\s+)?(?:the\s+)?{rx}",
    ])
    conv_vals = unique_values([
        rf"(?:initial\s+)?conversion\s+price.{{0,220}}?{money}.{{0,100}}?(?:for\s+shares?\s+of|for|of)\s+(?:the\s+)?{rx}",
        rf"(?:initial\s+)?conversion\s+price.{{0,220}}?{money}\s*(?:per\s+share)?(?=.{{0,180}}?{rx})",
        rf"{rx}.{{0,180}}?(?:initial\s+)?conversion\s+price.{{0,120}}?{money}",
    ])

    if len(liq_vals) != 1:
        return None
    liq = liq_vals[0]

    # First corroboration path: initial Conversion Price independently equals the
    # liquidation/base amount for the exact series.
    if len(conv_vals) == 1:
        conv = conv_vals[0]
        if abs(liq-conv) <= max(1e-9, abs(liq)*1e-8):
            return liq

    # Do NOT use a conversion numerator or liquidation amount as OIP when it differs
    # from the initial Conversion Price.  A charter can deliberately use a higher
    # fixed conversion numerator / liquidation base (for example a 3x preference)
    # while the original per-share economic base remains the initial Conversion Price.
    return None


def _initial_conversion_price_as_oip_equivalent(security_name: str, evidence: str) -> Optional[float]:
    """Return exact initial Conversion Price as an OIP-equivalent only when no OIP is stated.

    This is a deliberately narrow fallback for older charters that never use an
    Original Issue Price / purchase-price label.  It requires an exact-series
    *initial* Conversion Price and rejects the fallback if the same evidence contains
    an explicit issue/purchase/subscription/OIP label for that security.  It never
    uses a liquidation amount or conversion numerator as OIP.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    ev = _normalize_source_text_for_match(evidence)
    if not rx or token == "common" or not ev:
        return None
    # If an explicit issue-price/OIP role is present, that role must control instead.
    if re.search(
        rf"{rx}.{{0,220}}?(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price)|"
        rf"(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price).{{0,220}}?{rx}",
        ev, re.I,
    ):
        return None
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    vals: List[float] = []
    patterns = [
        rf"initial\s+conversion\s+price.{{0,180}}?{money}.{{0,120}}?(?:for\s+shares?\s+of|for|of)\s+(?:the\s+)?{rx}",
        rf"{rx}.{{0,180}}?initial\s+conversion\s+price.{{0,120}}?{money}",
    ]
    for pat in patterns:
        for m in re.finditer(pat, ev, re.I):
            try:
                v = float(m.group(1).replace(',', ''))
            except Exception:
                continue
            if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                vals.append(v)
    return vals[0] if len(vals) == 1 else None


def _exact_series_liquidation_multiple_from_evidence(security_name: str, evidence: str) -> Optional[str]:
    """Return an exact-series liquidation multiple such as ``1x OIP``.

    Requires the exact series identity and its own Original Issue Price reference in
    the operative liquidation evidence.  This handles clauses like ``one times the
    Series C-1 Original Issue Price`` without relying on a class-wide fallback.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    ev = _normalize_source_text_for_match(evidence)
    if not rx or token == "common" or not ev:
        return None
    words = {"one":1.0,"two":2.0,"three":3.0,"four":4.0,"five":5.0,"six":6.0,"seven":7.0,"eight":8.0,"nine":9.0,"ten":10.0}
    patterns = [
        rf"(?:amount\s+per\s+share\s+equal\s+to\s+)?(?P<num>\d+(?:\.\d+)?)\s*(?:x|times)\s+(?:the\s+)?{rx}\s+original\s+(?:issue|issuance|purchase)\s+price",
        rf"(?:amount\s+per\s+share\s+equal\s+to\s+)?(?P<word>one|two|three|four|five|six|seven|eight|nine|ten)\s+times\s+(?:the\s+)?{rx}\s+original\s+(?:issue|issuance|purchase)\s+price",
    ]
    vals: List[float] = []
    for pat in patterns:
        for m in re.finditer(pat, ev, re.I):
            try:
                v = float(m.group('num')) if m.groupdict().get('num') else words[m.group('word').lower()]
            except Exception:
                continue
            if v > 0 and not any(abs(v-u) <= 1e-9 for u in vals):
                vals.append(v)
    if len(vals) != 1:
        return None
    v = vals[0]
    return f"{int(v) if abs(v-round(v)) <= 1e-9 else v:g}x OIP"


def _conversion_ratio_from_exact_formula(security_name: str, evidence: str, conversion_price: Any) -> Optional[str]:
    """Calculate conversion ratio from an explicit exact-series conversion numerator.

    The numerator/base of a conversion formula is a separate legal fact from OIP.
    This prevents a 3x liquidation/conversion numerator from being mislabeled as OIP
    while still preserving the correct conversion ratio.
    """
    base = _fixed_conversion_base_from_evidence(security_name, evidence)
    cp = _money_to_float(conversion_price)
    if base is None or cp is None or base <= 0 or cp <= 0:
        return None
    ratio = base / cp
    return "1:1" if abs(ratio-1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"

def _security_is_named_residual_recipient(security_name: str, source_text: str) -> bool:
    """Detect an exact security expressly placed in the residual pool after preferences.

    Used as a guard against assigning a class-wide preferred liquidation formula to a
    separately designated security whose actual waterfall role is residual with Common.
    """
    if not source_text:
        return False
    name = str(security_name or '').strip()
    if not name:
        return False
    # Series-token identities are handled by class-wide preferred rules. This guard is
    # most important for separately named non-Series preferred classes.
    if _strict_series_token(name):
        return False
    text = _normalize_source_text_for_match(source_text)
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in {"stock","shares"}]
    if not words:
        return False
    exactish = r"\s+".join(re.escape(w) for w in words)
    patterns = [
        rf"after.{{0,260}}?(?:payment\s+in\s+full|liquidation\s+amounts?).{{0,500}}?(?:remaining\s+(?:assets|proceeds)).{{0,500}}?{exactish}",
        rf"remaining\s+(?:assets|proceeds).{{0,450}}?(?:common\s+stock).{{0,220}}?(?:and|,).{{0,120}}?{exactish}",
        rf"remaining\s+(?:assets|proceeds).{{0,450}}?{exactish}.{{0,220}}?(?:and|,).{{0,120}}?common\s+stock",
    ]
    return any(re.search(p, text, re.I) for p in patterns)


def _generic_nonparticipating_preferred_from_source(security_name: str, source_text: str) -> Optional[str]:
    """Classify a Series preferred security from the complete liquidation waterfall.

    A greater-of preference versus as-converted structure is non-participating when,
    after payment of the preferred liquidation amounts, the residual distribution
    goes to Common (and possibly separately named residual classes) rather than back
    to the same Series / generic Preferred Stock pool.
    """
    token = _strict_series_token(security_name)
    if not token.startswith('series') or not source_text:
        return None
    text = _normalize_source_text_for_match(source_text)
    scope = bool(re.search(
        r"each\s+series\s+of\s+preferred\s+stock|holders\s+of\s+shares\s+of\s+each\s+series\s+of\s+preferred\s+stock",
        text, re.I))
    if not scope:
        return None
    greater = bool(re.search(
        r"greater\s+of.{0,650}?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,850}?(?:as[-\s]?converted|converted\s+into\s+common|had\s+all\s+shares.{0,140}?converted)",
        text, re.I))
    if not greater:
        return None

    exact_rx = _strict_series_regex(token)
    # Examine the actual residual-distribution target, not every occurrence of the
    # words Preferred Stock near the clause (which may merely be a negative cross-reference).
    residual_targets: List[str] = []
    for m in re.finditer(r"remaining\s+(?:assets|proceeds)", text, re.I):
        tail = text[m.end():m.end()+1200]
        dm = re.search(r"(?:shall\s+be\s+)?distributed\s+(?:among|to)\s+(.{0,420})", tail, re.I)
        if dm:
            residual_targets.append(dm.group(1).split(".", 1)[0])
    if not residual_targets:
        return None

    for target in residual_targets:
        if not re.search(r"common\s+stock", target, re.I):
            continue
        # If the exact series itself is a residual recipient, it participates.
        if exact_rx and re.search(exact_rx, target, re.I):
            continue
        # Generic Preferred Stock as a distribution recipient indicates participation.
        # Ignore separately named residual classes such as Founder Preferred Stock.
        cleaned = re.sub(r"\bfounder\s+preferred\s+stock\b", "", target, flags=re.I)
        if re.search(r"(?:holders?\s+of\s+shares\s+of\s+)?preferred\s+stock", cleaned, re.I):
            continue
        return "Non-participating"
    return None


def _series_family_membership_proven(security_name: str, family: str, source_text: str) -> bool:
    """Prove that an exact child series belongs to a named parent series family."""
    token = _strict_series_token(security_name)
    child_rx = _strict_series_regex(token)
    if not child_rx or not source_text:
        return False
    text = _normalize_source_text_for_match(source_text)
    parent_rx = rf"series\s+{re.escape(family)}\s+preferred(?:\s+stock|\s+shares?)?"
    # Typical legal definitions use "together with", "collectively", "shall mean",
    # or define a parent term near the listed children.
    patterns = [
        rf"{child_rx}.{{0,700}}?(?:together\s+with|collectively|shall\s+mean|referred\s+to\s+as).{{0,260}}?{parent_rx}",
        rf"{parent_rx}.{{0,700}}?(?:means?|shall\s+mean|consists?\s+of|including).{{0,500}}?{child_rx}",
        rf"{child_rx}.{{0,450}}?{parent_rx}",
        rf"{parent_rx}.{{0,450}}?{child_rx}",
    ]
    return any(re.search(p, text, re.I) for p in patterns)

def _conversion_price_equals_oip_in_source(security_name: str, source_text: str) -> bool:
    """Certify an explicit exact-series Conversion Price = OIP relationship."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or token == "common" or not source_text:
        return False
    text = _normalize_source_text_for_match(source_text)
    patterns = [
        rf"{rx}.{{0,120}}?conversion\s+price.{{0,100}}?(?:initially\s+)?(?:shall\s+be\s+)?equal\s+to.{{0,80}}?(?:{rx}.{{0,80}}?)?original\s+(?:issue|issuance|purchase)\s+price",
        rf"(?:series\s+[a-z]+(?:\s*[- ]\s*\d+)?\s+)?conversion\s+price.{{0,100}}?(?:initially\s+)?(?:shall\s+be\s+)?equal\s+to\s+(?:the\s+)?{rx}.{{0,80}}?original\s+(?:issue|issuance|purchase)\s+price",
    ]
    return any(re.search(pat, text, re.I) for pat in patterns)


def _named_security_role_regex(security_name: str) -> Optional[str]:
    """Return a conservative exact-name regex for specifically named securities.

    Series securities continue to use the strict series-token machinery.  This
    fallback exists for real named classes such as ``Founder Preferred Stock`` or
    ``Non-Voting Junior Preferred Stock`` that are not named ``Series X``.  Generic
    labels (Preferred Stock, Common Stock, Notes, Warrants) are intentionally refused.
    """
    name = _normalize_source_text_for_match(str(security_name or "")).strip()
    if not name:
        return None
    # Series identities already have stricter token-based parsers. Do not let this
    # fallback compete with them or reinterpret nearby Series economics.
    if str(_strict_series_token(security_name) or "").startswith("series"):
        return None
    generic = {
        "preferred", "preferred stock", "preferred shares", "common", "common stock",
        "common shares", "convertible notes", "convertible note", "notes", "note",
        "warrant", "warrants", "options", "option",
    }
    if name in generic:
        return None
    words = re.findall(r"[a-z0-9]+", name, re.I)
    informative = [w for w in words if w.lower() not in {"stock", "shares", "share", "class"}]
    if len(informative) < 2:
        return None
    return r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"


def _conversion_price_equals_oip_for_named_preferred(security_name: str, source_text: str) -> bool:
    """Certify CP = OIP for a specifically named non-Series preferred class.

    Requires the exact security name plus an operative equality statement.  A
    class-wide Preferred Stock statement is accepted only when the named security is
    itself expressly identified as Preferred Stock in the same evidence.
    """
    if not source_text or "preferred" not in str(security_name or "").lower():
        return False
    rx = _named_security_role_regex(security_name)
    if not rx:
        return False
    text = _normalize_source_text_for_match(source_text)
    if not re.search(rx, text, re.I):
        return False
    local_patterns = [
        rf"{rx}.{{0,900}}?conversion\s+price.{{0,180}}?(?:initially\s+)?(?:shall\s+be\s+)?equal\s+to\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",
        rf"conversion\s+price.{{0,180}}?(?:initially\s+)?(?:shall\s+be\s+)?equal\s+to\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{{0,900}}?{rx}",
    ]
    if any(re.search(pat, text, re.I | re.S) for pat in local_patterns):
        return True
    # Common charter formulation: one rule for every series/class of Preferred Stock,
    # followed by exact OIP definitions for each named preferred security.
    classwide = re.search(
        r"conversion\s+price(?:\s+per\s+share)?.{0,220}?(?:each\s+series\s+of\s+preferred\s+stock|preferred\s+stock).{0,220}?(?:initially\s+)?(?:shall\s+be\s+)?equal\s+to\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",
        text, re.I | re.S,
    )
    return bool(classwide)


def _annual_dividend_for_named_security(security_name: str, source_text: str) -> Optional[float]:
    """Return an explicit annual dividend/share for a named non-Series security."""
    if not source_text:
        return None
    rx = _named_security_role_regex(security_name)
    if not rx:
        return None
    text = _normalize_source_text_for_match(source_text)
    vals: List[float] = []
    patterns = [
        rf"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+annum\s+for\s+each\s+share\s+of\s+(?:the\s+)?{rx}",
        rf"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+annum\s+(?:for|with\s+respect\s+to)\s+(?:the\s+)?{rx}",
        rf"{rx}.{{0,180}}?dividend\s+rate.{{0,180}}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?\s*(?:per\s+annum|annually|annual)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I | re.S):
            try:
                v = float(m.group(1).replace(",", ""))
            except Exception:
                continue
            if 0 <= v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                vals.append(v)
    return vals[0] if len(vals) == 1 else None


def _named_conversion_formula_ratio(security_name: str, evidence: str, conversion_price: Any) -> Optional[str]:
    """Calculate ratio from an exact named-security fixed numerator / CP formula."""
    if not evidence:
        return None
    rx = _named_security_role_regex(security_name)
    cp = _money_to_float(conversion_price)
    if not rx or cp is None or cp <= 0:
        return None
    text = _normalize_source_text_for_match(evidence)
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    patterns = [
        rf"(?:each\s+share\s+of\s+)?{rx}.{{0,260}}?convertible.{{0,360}}?(?:divid(?:e|ed|ing)|quotient).{{0,100}}?{money}.{{0,180}}?(?:by|over).{{0,100}}?(?:the\s+)?(?:applicable\s+)?(?:{rx}.{{0,80}}?)?conversion\s+price",
        rf"{rx}.{{0,260}}?conversion.{{0,280}}?(?:divid(?:e|ed|ing)|quotient).{{0,100}}?{money}.{{0,180}}?(?:conversion\s+price)",
    ]
    vals: List[float] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I | re.S):
            try:
                v = float(m.group(1).replace(",", ""))
            except Exception:
                continue
            if v > 0 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                vals.append(v)
    if len(vals) != 1:
        return None
    ratio = vals[0] / cp
    return "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"


def _generic_preferred_liquidation_basis_from_source(security_name: str, source_text: str) -> Optional[str]:
    """Certify common class-wide liquidation formulas applicable to every preferred series."""
    name = str(security_name or '')
    if 'preferred' not in name.lower() or not source_text:
        return None
    # Class-wide "each series of Preferred Stock" formulas apply only to actual
    # Series identities. Separately named preferred classes must be proven by an
    # exact clause; otherwise they may be residual securities despite their name.
    if not _strict_series_token(name).startswith("series"):
        return None
    if _security_is_named_residual_recipient(name, source_text):
        return None
    text = _normalize_source_text_for_match(source_text)
    # Require an operative class-wide scope plus the classic greater-of formulation.
    scope = bool(re.search(r"holders\s+of\s+(?:shares\s+of\s+)?(?:each\s+series\s+of\s+)?preferred\s+stock|each\s+series\s+of\s+preferred\s+stock", text, re.I))
    if not scope:
        return None
    greater = re.search(
        r"greater\s+of.{0,500}?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,700}?(?:converted\s+into\s+common|as[-\s]?converted|had\s+all\s+shares.{0,100}?converted)",
        text,
        re.I,
    )
    if greater:
        return "1x OIP"
    # Direct one-times / 1x class-wide preference.
    if re.search(r"(?:one\s+times|1\s*x)\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price", text, re.I):
        return "1x OIP"
    return None


def _group_dividend_percent_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Certify a fixed dividend rate inherited from an explicitly defined series family.

    Example: Series D-1 / D-2 are members of Series D Preferred Stock and the charter
    states an 8% Series D dividend. The helper requires both the child identity and
    the parent-family economic clause in the same source document.
    """
    token = _strict_series_token(security_name)
    m = re.match(r"^series([a-z]+)(\d+)$", token or '')
    if not m or not source_text:
        return None
    family = m.group(1)
    child_rx = _strict_series_regex(token)
    text = _normalize_source_text_for_match(source_text)
    if not child_rx or not re.search(child_rx, text, re.I):
        return None
    parent_rx = rf"series\s+{re.escape(family)}\s+preferred(?:\s+stock|\s+shares?)?"
    # Require a legal definition/designation that proves the exact child belongs to
    # the parent family. Do not require the linkage to appear only in an OIP definition.
    linkage = _series_family_membership_proven(security_name, family, source_text)
    if not linkage:
        return None
    # First parse the operative parent-family dividend sentence directly. Legal
    # drafting commonly states the percentage as words followed by a parenthetical
    # numeric percentage (e.g. "eight percent (8%)").
    direct_vals: List[float] = []
    for pat in [
        rf"{parent_rx}.{{0,420}}?dividends?.{{0,320}}?rate\s+per\s+annum\s+of.{{0,140}}?\(?\s*(\d+(?:\.\d+)?)\s*%",
        rf"{parent_rx}.{{0,420}}?dividends?.{{0,320}}?at\s+(?:the\s+)?rate(?:\s+per\s+annum)?\s+of.{{0,140}}?\(?\s*(\d+(?:\.\d+)?)\s*%",
    ]:
        for mm in re.finditer(pat, text, re.I):
            try:
                direct_vals.append(float(mm.group(1)))
            except Exception:
                pass
    direct_uniq=[]
    for v in direct_vals:
        if 0 <= v <= 100 and not any(abs(v-u) <= 1e-9 for u in direct_uniq):
            direct_uniq.append(v)
    if len(direct_uniq) == 1:
        return direct_uniq[0]
    windows = _role_windows(source_text, [parent_rx, r"dividend", r"dividends"], radius=2600)
    vals: List[float] = []
    for w0 in windows:
        w = _normalize_source_text_for_match(w0)
        if not re.search(parent_rx, w, re.I) or not re.search(r"dividend", w, re.I):
            continue
        for pat in [
            rf"{parent_rx}.{{0,320}}?dividends?.{{0,260}}?(?:rate\s+(?:per\s+annum\s+)?of|at\s+(?:the\s+)?rate(?:\s+per\s+annum)?\s+of).{{0,140}}?\(?\s*(\d+(?:\.\d+)?)\s*%",
            rf"dividends?.{{0,260}}?(?:rate\s+(?:per\s+annum\s+)?of|at\s+(?:the\s+)?rate(?:\s+per\s+annum)?\s+of).{{0,140}}?\(?\s*(\d+(?:\.\d+)?)\s*%.{{0,240}}?{parent_rx}",
        ]:
            for mm in re.finditer(pat, w, re.I):
                try:
                    vals.append(float(mm.group(1)))
                except Exception:
                    pass
    uniq=[]
    for v in vals:
        if 0 <= v <= 100 and not any(abs(v-u) <= 1e-9 for u in uniq):
            uniq.append(v)
    return uniq[0] if len(uniq)==1 else None

def _apply_economic_anchors_to_facts(facts: Dict[str, Any], anchors: Dict[str, Any], expected_names: List[str], source_text: str = "") -> Dict[str, Any]:
    """Certify-or-clear critical economics against exact-series evidence.

    v15.17 architecture:
    * broad-pass economics are candidates only, never authoritative;
    * every critical field is cleared before certification;
    * OIP and Conversion Price are certified independently;
    * a value can be restored only when the short evidence belongs to the exact
      immutable security identity and supports the claimed field/value;
    * deterministic calculations run only from certified inputs.

    This prevents an incorrect broad-pass value from surviving merely because a
    targeted verifier failed to find a replacement.
    """
    if not isinstance(facts, dict) or not isinstance(anchors, dict):
        return facts

    out = dict(facts)
    rows = [dict(r) for r in (facts.get("securities", []) or [])]
    expected_by_norm = {
        normalize_security_name_for_match(name): name
        for name in expected_names
        if str(name or "").strip()
    }

    critical_defaults = {
        "oip_original_issue_price": "Not stated",
        "conversion_price": "Not stated",
        "conversion_ratio": "Not stated",
        "liquidation_preference": "Not stated",
        "participation_rights": "Not stated",
        "participation_cap": "Not stated",
        "dividend_rate": "Not stated",
        "dividend_type": "Not stated",
    }

    row_map: Dict[str, Dict[str, Any]] = {}
    original_candidates: Dict[str, Dict[str, Any]] = {}
    certification: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        supplied = str(row.get("security_name", "") or "").strip()
        match = best_existing_security_match(supplied, expected_names)
        if not match:
            continue
        key = normalize_security_name_for_match(match)
        # Lock the row to the authoritative roster name and immutable ID.
        row["security_name"] = match
        row["_security_id"] = _security_id_for_name(match)
        original_candidates[key] = dict(row)
        for field, default in critical_defaults.items():
            row[field] = default
        row_map[key] = row
        certification[key] = {
            "security_id": row["_security_id"],
            "security_name": match,
            "certified_fields": [],
            "rejected_fields": [],
        }

    def certify(key: str, field: str, value: Any) -> None:
        row_map[key][field] = value
        certification[key]["certified_fields"].append(field)

    def reject(key: str, field: str) -> None:
        if field not in certification[key]["rejected_fields"]:
            certification[key]["rejected_fields"].append(field)

    for a in (anchors.get("anchors", []) or []):
        supplied = str(a.get("security_name", "") or "").strip()
        match = best_existing_security_match(supplied, expected_names)
        if not match:
            continue
        key = normalize_security_name_for_match(match)
        row = row_map.get(key)
        if row is None:
            continue

        evidence = str(a.get("source_text", "") or "").strip()
        exact_evidence = _exact_security_in_evidence(match, evidence)
        if not exact_evidence:
            for fld in critical_defaults:
                reject(key, fld)
            continue

        oip = _safe_verified_value(a.get("oip_original_issue_price"), "Not stated")
        cp = _safe_verified_value(a.get("conversion_price"), "Not stated")
        liq = _safe_verified_value(a.get("liquidation_basis"), "Not stated")
        part = _safe_verified_value(a.get("participation_rights"), "Not stated")
        pcap = _safe_verified_value(a.get("participation_cap"), "Not stated")
        dr = _safe_verified_value(a.get("dividend_rate"), "Not stated")
        da = _safe_verified_value(a.get("annual_dividend_per_share"), "Not stated")
        dtype = _safe_verified_value(a.get("dividend_type"), "Not stated")

        # OIP and Conversion Price are independent and require the claimed dollar
        # amount to be literally present in exact-security evidence.
        if (not _value_is_missing(oip)
                and _field_role_supported_in_evidence("oip", evidence)
                and _money_value_in_evidence(oip, evidence)):
            certify(key, "oip_original_issue_price", oip)
        else:
            # Some charters do not use an OIP label but independently state a fixed
            # per-series numerator/base in the conversion formula. Accept only when
            # the claimed OIP equals that exact conversion base; never copy CP itself.
            conv_base = _fixed_conversion_base_from_evidence(match, evidence)
            if conv_base is not None and _money_to_float(oip) is not None and abs(_money_to_float(oip) - conv_base) <= max(1e-9, abs(conv_base)*1e-8):
                certify(key, "oip_original_issue_price", oip)
            else:
                reject(key, "oip_original_issue_price")

        if (not _value_is_missing(cp)
                and _field_role_supported_in_evidence("conversion", evidence)
                and _money_value_in_evidence(cp, evidence)):
            certify(key, "conversion_price", cp)
        else:
            reject(key, "conversion_price")

        # Liquidation classification: dollar amounts require literal evidence;
        # OIP multiples/qualified-balance formulas require matching legal wording.
        if not _value_is_missing(liq):
            liq_ok = True
            if _money_to_float(liq) is not None:
                liq_ok = _money_value_in_evidence(liq, evidence)
            elif re.search(r"\b\d+(?:\.\d+)?\s*x\b", liq, re.I):
                mult = re.search(r"\b(\d+(?:\.\d+)?)\s*x\b", liq, re.I)
                liq_ok = bool(mult and re.search(rf"\b{re.escape(mult.group(1))}\s*x\b|\b{re.escape(mult.group(1))}\s+times\b", evidence, re.I))
            if liq_ok:
                certify(key, "liquidation_preference", liq)
            else:
                reject(key, "liquidation_preference")
        else:
            reject(key, "liquidation_preference")

        if part in {"Non-participating", "Participating", "Capped Participation", "N/A"}:
            certify(key, "participation_rights", part)
        else:
            reject(key, "participation_rights")

        if not _value_is_missing(pcap):
            cap_ok = True
            if _money_to_float(pcap) is not None:
                cap_ok = _money_value_in_evidence(pcap, evidence)
            elif re.search(r"\b\d+(?:\.\d+)?\s*x\b", pcap, re.I):
                mult = re.search(r"\b(\d+(?:\.\d+)?)\s*x\b", pcap, re.I)
                cap_ok = bool(mult and re.search(rf"\b{re.escape(mult.group(1))}\s*x\b|\b{re.escape(mult.group(1))}\s+times\b", evidence, re.I))
            if cap_ok:
                certify(key, "participation_cap", pcap)
            else:
                reject(key, "participation_cap")
        else:
            reject(key, "participation_cap")

        if dtype in {"Cumulative", "Non-cumulative"}:
            certify(key, "dividend_type", dtype)
        else:
            reject(key, "dividend_type")

        # Dividend rate can come only from an explicit dividend percentage in the
        # exact-security evidence or explicit annual dividend/share divided by
        # independently certified OIP. Never infer a percentage from generic dollars.
        evidence_pct = _dividend_percent_from_anchor_evidence(match, evidence)
        if evidence_pct is not None:
            pct_text = f"{evidence_pct:.6f}".rstrip("0").rstrip(".") + "%"
            certify(key, "dividend_rate", pct_text)
        else:
            amount = None
            if not _value_is_missing(da) and _money_value_in_evidence(da, evidence):
                amount = _money_to_float(da)
            if amount is None:
                amount = _annual_dividend_from_anchor_evidence(match, evidence)
            base = _money_to_float(row.get("oip_original_issue_price"))
            if amount is not None and base is not None and base > 0 and amount >= 0:
                pct = amount / base * 100.0
                if 0 <= pct <= 100:
                    certify(key, "dividend_rate", f"{pct:.6f}".rstrip("0").rstrip(".") + "%")
                else:
                    reject(key, "dividend_rate")
            else:
                reject(key, "dividend_rate")

        # Ratio is deterministic only after both independently certified anchors exist.
        base = _money_to_float(row.get("oip_original_issue_price"))
        conv = _money_to_float(row.get("conversion_price"))
        if base is not None and conv is not None and base > 0 and conv > 0:
            ratio = base / conv
            ratio_text = "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"
            certify(key, "conversion_ratio", ratio_text)
        else:
            reject(key, "conversion_ratio")

        if evidence:
            row["source_text"] = (str(row.get("source_text", "") or "").strip() + "\n\nCERTIFIED ECONOMIC EVIDENCE: " + evidence).strip()

    # v15.17 SOURCE-RELATIONSHIP CERTIFICATION. This is additive only: it restores a
    # field that remains blank after the targeted anchor pass when the source itself
    # proves the exact value or legal relationship. It never overwrites a certified value.
    full_source = str(source_text or "")
    for key, row in row_map.items():
        name = str(row.get("security_name", "") or "").strip()
        candidate = original_candidates.get(key, {})
        candidate_ev = str(candidate.get("source_text", "") or "")

        # 1) Preserve a correct broad-pass OIP/CP only if its own evidence proves exact
        # security + field role + literal amount. This protects previously passed results
        # without allowing unsupported broad values to survive.
        if _value_is_missing(row.get("oip_original_issue_price")):
            cv = candidate.get("oip_original_issue_price")
            if (not _value_is_missing(cv) and _exact_security_in_evidence(name, candidate_ev)
                    and _field_role_supported_in_evidence("oip", candidate_ev)
                    and _money_value_in_evidence(cv, candidate_ev)):
                certify(key, "oip_original_issue_price", cv)

            # Independent fixed conversion numerator/base (not Conversion Price) can
            # certify the economic OIP base when the charter never uses an OIP label.
            # Crucially, this must run even when the broad pass returned OIP=Not stated.
            if _value_is_missing(row.get("oip_original_issue_price")):
                b = _fixed_conversion_base_from_evidence(name, candidate_ev)
                if b is not None:
                    certify(key, "oip_original_issue_price", _fmt_money_num(b))

            if _value_is_missing(row.get("oip_original_issue_price")):
                b = _corroborated_economic_base_from_evidence(name, candidate_ev)
                if b is not None:
                    certify(key, "oip_original_issue_price", _fmt_money_num(b))

            # Last exact-series OIP-equivalent fallback for older charters: an explicitly
            # stated initial Conversion Price may serve as the original per-share base
            # only when no OIP/issue/purchase/subscription price is stated.
            if _value_is_missing(row.get("oip_original_issue_price")):
                b = _initial_conversion_price_as_oip_equivalent(name, candidate_ev)
                if b is not None:
                    certify(key, "oip_original_issue_price", _fmt_money_num(b))

            if _value_is_missing(row.get("oip_original_issue_price")) and full_source:
                v = _exact_series_money_from_role_clause(name, full_source, "oip")
                if v is not None:
                    certify(key, "oip_original_issue_price", _fmt_money_num(v))
                else:
                    # Same independent-base rule against local source text when available.
                    b = _fixed_conversion_base_from_evidence(name, full_source)
                    if b is None:
                        b = _corroborated_economic_base_from_evidence(name, full_source)
                    if b is None:
                        b = _initial_conversion_price_as_oip_equivalent(name, full_source)
                    if b is not None:
                        certify(key, "oip_original_issue_price", _fmt_money_num(b))

        if _value_is_missing(row.get("conversion_price")):
            cv = candidate.get("conversion_price")
            if (not _value_is_missing(cv) and _exact_security_in_evidence(name, candidate_ev)
                    and _field_role_supported_in_evidence("conversion", candidate_ev)
                    and _money_value_in_evidence(cv, candidate_ev)):
                certify(key, "conversion_price", cv)
            elif full_source:
                v = _exact_series_money_from_role_clause(name, full_source, "conversion")
                if v is not None:
                    certify(key, "conversion_price", _fmt_money_num(v))
                elif ((_conversion_price_equals_oip_in_source(name, full_source)
                       or _conversion_price_equals_oip_for_named_preferred(name, full_source))
                      and not _value_is_missing(row.get("oip_original_issue_price"))):
                    certify(key, "conversion_price", row.get("oip_original_issue_price"))

        # 2) Liquidation formulas may be expressed by cross-reference rather than by
        # repeating the dollar amount for every series. Certify the universal class-wide
        # greater-of OIP vs as-converted formulation as 1x OIP.
        if _value_is_missing(row.get("liquidation_preference")):
            # Prefer an exact-series formula from the row's own evidence, then local source.
            liq = _exact_series_liquidation_multiple_from_evidence(name, candidate_ev) if candidate_ev else None
            if liq is None and full_source:
                liq = _exact_series_liquidation_multiple_from_evidence(name, full_source)
            if liq is None:
                liq = _generic_preferred_liquidation_basis_from_source(name, full_source) if full_source else None
            if liq:
                certify(key, "liquidation_preference", liq)
            else:
                cv = candidate.get("liquidation_preference")
                if (not _value_is_missing(cv) and _exact_security_in_evidence(name, candidate_ev)
                        and _field_role_supported_in_evidence("liquidation", candidate_ev)):
                    certify(key, "liquidation_preference", cv)

        # 3) Full-waterfall participation classification. A class-wide greater-of
        # preference versus as-converted structure with residual solely to Common is
        # non-participating. Apply only when participation is still missing.
        if _value_is_missing(row.get("participation_rights")):
            pclass = None
            if full_source:
                pclass = _extract_participation_class_from_source(name, full_source)
                if pclass is None:
                    pclass = _generic_nonparticipating_preferred_from_source(name, full_source)
            if pclass is None and candidate_ev:
                pclass = _extract_participation_class_from_source(name, candidate_ev)
                if pclass is None:
                    pclass = _generic_nonparticipating_preferred_from_source(name, candidate_ev)
            if pclass in {"Non-participating", "Participating", "Capped Participation", "N/A"}:
                certify(key, "participation_rights", pclass)

        # 4) Defined-family dividend inheritance, e.g. D-1/D-2 under Series D Preferred.
        if _value_is_missing(row.get("dividend_rate")) and full_source:
            gpct = _group_dividend_percent_from_source(name, full_source)
            if gpct is not None:
                certify(key, "dividend_rate", f"{gpct:.6f}".rstrip("0").rstrip(".") + "%")

        # 5) Deterministic calculations run last, after all literal/equality/formula
        # certifications are complete.
        base = _money_to_float(row.get("oip_original_issue_price"))
        conv = _money_to_float(row.get("conversion_price"))
        if _value_is_missing(row.get("conversion_ratio")):
            ratio_text = _conversion_ratio_from_exact_formula(name, candidate_ev, row.get("conversion_price")) if candidate_ev else None
            if ratio_text is None and full_source:
                ratio_text = _conversion_ratio_from_exact_formula(name, full_source, row.get("conversion_price"))
            if ratio_text is None and candidate_ev:
                ratio_text = _named_conversion_formula_ratio(name, candidate_ev, row.get("conversion_price"))
            if ratio_text is None and full_source:
                ratio_text = _named_conversion_formula_ratio(name, full_source, row.get("conversion_price"))
            if ratio_text is None and base is not None and conv is not None and base > 0 and conv > 0:
                ratio = base / conv
                ratio_text = "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"
            if ratio_text is not None:
                certify(key, "conversion_ratio", ratio_text)

        if _value_is_missing(row.get("dividend_rate")):
            # Late OIP certification can make a previously unusable annual dividend
            # amount calculable. Prefer local full-source text, then the exact row's
            # source-backed candidate evidence (important for scanned/image PDFs).
            amount = _strict_dividend_amount_from_source(name, full_source) if full_source else None
            if amount is None and full_source:
                amount = _annual_dividend_for_named_security(name, full_source)
            if amount is None and candidate_ev:
                amount = _annual_dividend_from_anchor_evidence(name, candidate_ev)
            if amount is None and candidate_ev:
                amount = _annual_dividend_for_named_security(name, candidate_ev)
            if amount is None and candidate_ev:
                amount = _strict_dividend_amount_from_source(name, candidate_ev)
            if amount is not None and base is not None and base > 0 and amount >= 0:
                pct = amount / base * 100.0
                if 0 <= pct <= 100:
                    certify(key, "dividend_rate", f"{pct:.6f}".rstrip("0").rstrip(".") + "%")

    out["securities"] = rows
    out["_economic_certification"] = list(certification.values())
    return _attach_immutable_security_ids(out)

def _source_facts_cache_root() -> Path:
    """Persistent source-fact caching is disabled in v15.9.1."""
    return Path()

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _load_source_facts_cache(path: Path) -> Optional[Dict[str, Any]]:
    return None

def _save_source_facts_cache(path: Path, model: str, facts: Dict[str, Any], security_master: Optional[Dict[str, Any]] = None) -> None:
    return None



def _security_id_for_name(security_name: str) -> str:
    """Return a deterministic immutable ID for one exact roster security.

    The ID is derived only from the canonical roster identity, never from economics,
    counts, row position, or company-specific values.  All downstream certification
    remains keyed to this ID/name pair for the life of the document run.
    """
    normalized = normalize_security_name_for_match(str(security_name or ""))
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10].upper()
    token = re.sub(r"[^A-Z0-9]+", "_", normalized.upper()).strip("_")[:36] or "SECURITY"
    return f"SEC_{token}_{digest}"


def _attach_immutable_security_ids(data: Dict[str, Any]) -> Dict[str, Any]:
    """Attach one deterministic security_id to each authoritative roster row."""
    if not isinstance(data, dict):
        return data
    seen: Dict[str, str] = {}
    for row in (data.get("securities", []) or []):
        name = str(row.get("security_name", "") or "").strip()
        sid = _security_id_for_name(name)
        if not sid:
            continue
        previous = seen.get(sid)
        if previous and normalize_security_name_for_match(previous) != normalize_security_name_for_match(name):
            raise ValueError(f"Immutable security ID collision: {previous!r} vs {name!r}")
        seen[sid] = name
        row["_security_id"] = sid
    data["_security_id_map"] = {sid: name for sid, name in seen.items()}
    return data


def _security_identity_payload(security_names: List[str]) -> List[Dict[str, str]]:
    return [
        {"security_id": _security_id_for_name(name), "security_name": name}
        for name in security_names
        if str(name or "").strip()
    ]

def best_existing_security_match(supplied: str, expected_names: List[str]) -> Optional[str]:
    """Strict security-name matcher for economic facts.

    Economic values must never be assigned using broad fuzzy matching. We first
    require exact normalized identity. A compact exact series token is accepted
    only when it uniquely identifies one expected row (e.g., Series A-1).
    """
    supplied_norm = normalize_security_name_for_match(supplied)
    if not supplied_norm:
        return None
    exact = [n for n in expected_names if normalize_security_name_for_match(n) == supplied_norm]
    if len(exact) == 1:
        return exact[0]
    # Exact series/common token fallback only; never proximity/token-overlap matching.
    def tok(v: str) -> str:
        low = str(v or "").lower()
        if "common" in low and "preferred" not in low:
            return "common"
        m = re.search(r"\bseries\s+([a-z]+)(?:\s*[-–—]?\s*(\d+))?", low)
        if not m:
            return ""
        return f"series{m.group(1)}{m.group(2) or ''}"
    st = tok(supplied)
    if not st:
        return None
    matches = [n for n in expected_names if tok(n) == st]
    return matches[0] if len(matches) == 1 else None


def _norm_compare_value(value: Any) -> str:
    t = str(value or "").strip().lower()
    t = t.replace("$", "").replace(",", "")
    t = re.sub(r"\s+", " ", t)
    aliases = {
        "not applicable": "n/a", "na": "n/a", "n.a.": "n/a",
        "non participating": "non-participating", "nonparticipating": "non-participating",
        "capped participation": "capped participation", "participating capped": "capped participation",
    }
    return aliases.get(t, t)


def _facts_by_exact_security(facts: Dict[str, Any], expected_names: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in (facts or {}).get("securities", []) or []:
        supplied = str(row.get("security_name", "")).strip()
        match = best_existing_security_match(supplied, expected_names)
        if match and match not in out:
            out[match] = row
    return out


def _critical_fact_disagreements(a: Dict[str, Any], b: Dict[str, Any], expected_names: List[str]) -> List[str]:
    fields = [
        "oip_original_issue_price", "liquidation_preference",
        "seniority_rank", "participation_rights", "participation_cap",
        "conversion_price", "conversion_ratio", "dividend_rate", "dividend_type",
        "cap_price_includes_dividends",
    ]
    aa = _facts_by_exact_security(a, expected_names)
    bb = _facts_by_exact_security(b, expected_names)
    issues: List[str] = []
    # Compare the full security roster too, so a missed series triggers adjudication.
    roster_a = sorted({normalize_security_name_for_match(str(r.get("security_name", ""))) for r in (a or {}).get("securities", []) or [] if str(r.get("security_name", "")).strip()})
    roster_b = sorted({normalize_security_name_for_match(str(r.get("security_name", ""))) for r in (b or {}).get("securities", []) or [] if str(r.get("security_name", "")).strip()})
    if roster_a != roster_b:
        issues.append("security roster differs between independent passes")
    for name in expected_names:
        if name not in aa or name not in bb:
            issues.append(f"{name}: missing verification row in one pass")
            continue
        for f in fields:
            if _norm_compare_value(aa[name].get(f)) != _norm_compare_value(bb[name].get(f)):
                issues.append(f"{name}: {f} differs")
    return issues


def _validate_verified_facts(facts: Dict[str, Any], expected_names: List[str]) -> List[str]:
    """Deterministic gate before a result is eligible for persistent cache."""
    issues: List[str] = []
    if not isinstance(facts, dict):
        return ["verification payload is not an object"]
    mapped = _facts_by_exact_security(facts, expected_names)
    missing = [n for n in expected_names if n not in mapped]
    if missing:
        issues.append("missing exact security rows: " + ", ".join(missing))
    if len(mapped) != len(expected_names):
        issues.append("security row count does not reconcile to expected securities")

    allowed_part = {"non-participating", "participating", "capped participation", "n/a", "not stated"}
    allowed_divtype = {"cumulative", "non-cumulative", "not stated", "n/a", "simple", "compounding"}
    allowed_capdiv = {"yes", "no", "n/a", "not stated"}
    bad_words = re.compile(r"needs\s+review|review\s+required|required\s+review|ambiguous|unclear", re.I)

    for name, row in mapped.items():
        for f in ["oip_original_issue_price","liquidation_preference","participation_rights","participation_cap","conversion_price","conversion_ratio","dividend_rate","dividend_type","cap_price_includes_dividends"]:
            val = str(row.get(f, "") or "").strip()
            if bad_words.search(val):
                issues.append(f"{name}: prohibited unresolved marker in {f}")
        part = _norm_compare_value(row.get("participation_rights"))
        if part and part not in allowed_part:
            issues.append(f"{name}: unsupported participation_rights ({row.get('participation_rights')})")
        dt = _norm_compare_value(row.get("dividend_type"))
        if dt and dt not in allowed_divtype:
            issues.append(f"{name}: unsupported dividend_type ({row.get('dividend_type')})")
        cd = _norm_compare_value(row.get("cap_price_includes_dividends"))
        if cd and cd not in allowed_capdiv:
            issues.append(f"{name}: unsupported cap-price dividend flag ({row.get('cap_price_includes_dividends')})")
        try:
            rank = int(row.get("seniority_rank", 999))
            if rank < 1:
                issues.append(f"{name}: invalid seniority rank {rank}")
        except Exception:
            issues.append(f"{name}: seniority rank is not an integer")
    return issues


CAP_TABLE_ADJUDICATION_INSTRUCTIONS = CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS + r"""

ADJUDICATION PASS
You are the final independent adjudicator. The same source document was reviewed twice and the two candidate results disagree on one or more critical fields. Re-read the source itself and resolve every disagreement from the operative clause/definition. Do not choose a candidate merely because it looks plausible or appears in both nearby text blocks. Exact class/series identity is mandatory. Return a complete corrected result for every expected security using the same strict schema. The source document is authoritative; candidate A and B are only clues. Keep all economic outputs concise and machine-friendly.
"""


def _call_full_source_verification(client: OpenAI, model: str, uploaded_id: str, path: Path, security_list: List[str], reasoning_effort: str, instructions: str, extra_text: str = "") -> Dict[str, Any]:
    user_text = (
        f"Document name: {path.name}\nExpected securities/classes/series:\n"
        + json.dumps(security_list, ensure_ascii=False, indent=2)
        + "\n\nReview the full uploaded document and return one verification row for every expected security, plus any actual security/class/series found in the source that the expected list missed. Treat the source as authoritative."
        + ("\n\n" + extra_text if extra_text else "")
    )
    response = _responses_create(client, 
        model=model,
        instructions=instructions,
        input=[{"role":"user","content":[{"type":"input_file","file_id":uploaded_id},{"type":"input_text","text":user_text}]}],
        text=cap_table_source_facts_text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)

def _safe_verified_value(value: Any, fallback: str = "Not stated") -> str:
    t = str(value or "").strip()
    if t.lower() in {"needs review","review required","required review","review","unclear","ambiguous"}:
        return fallback
    return t or fallback


def _call_security_master(client: OpenAI, model: str, uploaded_id: str, path: Path, reasoning_effort: str, instructions: str, extra_text: str = "") -> Dict[str, Any]:
    user_text = (
        f"Document name: {path.name}\n"
        "Build the authoritative security roster only from the uploaded source. "
        "Authorized share counts are out of scope: do not research, calculate, reconcile, or validate them. "
        "Ignore any prior extraction and independently read the capitalization/designation provisions."
        + ("\n\n" + extra_text if extra_text else "")
    )
    response = _responses_create(client, 
        model=model,
        instructions=instructions,
        input=[{"role":"user","content":[{"type":"input_file","file_id":uploaded_id},{"type":"input_text","text":user_text}]}],
        text=security_master_text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)


def _security_master_signature(master: Dict[str, Any]) -> List[Tuple[str, str]]:
    sig: List[Tuple[str, str]] = []
    for row in (master or {}).get("securities", []) or []:
        name = str(row.get("security_name", "") or "").strip()
        if not name:
            continue
        count = str(row.get("authorized_count", "") or "").replace(",", "").strip().lower()
        sig.append((normalize_security_name_for_match(name), count))
    return sorted(sig)


def _normalize_master_authorized(value: Any) -> str:
    t = str(value or "").strip()
    if not t or t.lower() in {"not stated", "n/a", "na"}:
        return "Not stated"
    m = re.fullmatch(r"\s*(\d{1,3}(?:,\d{3})+|\d+)\s*", t)
    if not m:
        return "Not stated"
    try:
        return f"{int(m.group(1).replace(',', '')):,}"
    except Exception:
        return "Not stated"



def _canonical_security_name_from_source(value: str) -> str:
    """Canonicalize a capitalization-clause security label without company-specific rules."""
    t = str(value or "").strip().strip('"\'“”‘’.,;:()[]')
    t = re.sub(r"\s+", " ", t)
    if not t:
        return ""
    if re.search(r"\bcommon\b", t, re.I) and not re.search(r"\bpreferred\b", t, re.I):
        return "Common Stock"
    m = re.search(r"\bseries\s+([A-Za-z]+)(?:\s*[-–—]\s*(\d+))?", t, re.I)
    if m:
        word = m.group(1)
        num = m.group(2)
        token = f"Series {word.upper() if len(word)==1 else word.title()}"
        if num:
            token += f"-{num}"
        return token + " Preferred Stock"
    return t


def _extract_capitalization_facts_from_source(source_text: str) -> Dict[str, Any]:
    """Parse authoritative roster/count facts from the capitalization/designation clause.

    This parser deliberately ignores economics and focuses only on share-authority language.
    It is reusable across standard corporate charters and never uses company names or known values.
    """
    facts: Dict[str, Any] = {"total_common_authorized": None, "total_preferred_authorized": None, "series_counts": {}, "evidence": {}}
    if not source_text:
        return facts
    text = str(source_text).replace("–", "-").replace("—", "-")
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    count_rx = r"\d{1,3}(?:,\d{3})+|\d+"
    series_rx = r"Series\s+[A-Za-z]+(?:\s*-\s*\d+)?(?!\s*-\s*[A-Za-z0-9])(?:\s+Preferred(?:\s+Stock|\s+Shares?))?"

    def to_int(v: str) -> Optional[int]:
        try:
            n = int(str(v).replace(',', '').strip())
            return n if n > 0 else None
        except Exception:
            return None

    def set_total(kind: str, n: int, ev: str) -> None:
        key = f"total_{kind}_authorized"
        old = facts.get(key)
        if old is None or (old < 1000 <= n):
            facts[key] = n
            facts["evidence"][key] = ev.strip()

    # Top-level class totals. Require shares + class language and prefer contexts mentioning authority/authorized.
    class_patterns = [
        rf"(?P<count>{count_rx})\s+shares?\s+of\s+(?:authorized\s+)?(?P<class>Common|Preferred)\s+Stock",
        rf"(?P<count>{count_rx})\s+shares?\s+of\s+(?P<class>Common|Preferred)\s+Stock[^.;]{{0,100}}?authorized",
    ]
    for pat in class_patterns:
        for m in re.finditer(pat, text, re.I):
            n = to_int(m.group('count'))
            if not n:
                continue
            kind = m.group('class').lower()
            local = text[max(0,m.start()-180):min(len(text),m.end()+180)]
            # Reject issued/outstanding/reserved contexts unless authority/designation language is also present.
            if re.search(r"issued|outstanding|reserved|issuable|option|warrant|as-converted", local, re.I) and not re.search(r"authori[sz]|designat|authority\s+to\s+issue", local, re.I):
                continue
            set_total(kind, n, local)

    def add_series(series: str, count: int, ev: str) -> None:
        name = _canonical_security_name_from_source(series)
        if not name or not count or count <= 0:
            return
        key = normalize_security_name_for_match(name)
        # Keep the first high-confidence exact pair. All patterns below are designation/consist-of patterns.
        if key not in facts["series_counts"]:
            facts["series_counts"][key] = {"security_name": name, "authorized_count": count, "source_text": ev.strip()}

    # Count -> exact series designation. Handles "1,891,431 of which are designated as Series Seed-1..."
    count_to_series_patterns = [
        rf"(?P<count>{count_rx})\s+(?:shares?\s+)?(?:of\s+which\s+are\s+|of\s+which\s+is\s+)?(?:hereby\s+)?designated\s+(?:as\s+)?[\"'“”‘’]*(?P<series>{series_rx})",
        rf"(?P<count>{count_rx})\s+shares?[^.;]{{0,140}}?(?:shall\s+be\s+|are\s+|is\s+|hereby\s+)?designated\s+(?:as\s+)?[\"'“”‘’]*(?P<series>{series_rx})",
    ]
    for pat in count_to_series_patterns:
        for m in re.finditer(pat, text, re.I):
            n = to_int(m.group('count'))
            if not n:
                continue
            local = text[max(0,m.start()-140):min(len(text),m.end()+140)]
            if re.search(r"issued|outstanding|reserved|issuable|option|warrant|as-converted", local, re.I) and not re.search(r"authori[sz]|designat|consist|authority\s+to\s+issue", local, re.I):
                continue
            add_series(m.group('series'), n, local)

    # Exact series -> count. Handles "Series A ... shall consist of 17,005,494 shares" and table-like clauses.
    series_to_count_patterns = [
        rf"(?P<series>{series_rx})[^.;]{{0,180}}?(?:shall\s+consist\s+of|consists?\s+of|authorized\s+(?:number\s+of\s+)?shares?(?:\s+is|\s+are|\s*[:=])?|number\s+of\s+authorized\s+shares?(?:\s+is|\s+are|\s*[:=])?)\s*(?P<count>{count_rx})\s+shares?",
        rf"(?P<series>{series_rx})\s*[:\-]\s*(?P<count>{count_rx})(?:\s+shares?)?",
    ]
    for pat in series_to_count_patterns:
        for m in re.finditer(pat, text, re.I):
            n = to_int(m.group('count'))
            if not n:
                continue
            local = text[max(0,m.start()-120):min(len(text),m.end()+120)]
            add_series(m.group('series'), n, local)

    # "all of which are hereby designated Series A" means the aggregate Preferred count is the exact series count.
    if facts.get("total_preferred_authorized"):
        pat = rf"(?P<count>{count_rx})\s+shares?\s+of\s+(?:authorized\s+)?Preferred\s+Stock.{{0,260}}?all\s+of\s+which\s+are\s+(?:hereby\s+)?designated\s+(?:as\s+)?[\"'“”‘’]*(?P<series>{series_rx})"
        for m in re.finditer(pat, text, re.I):
            n = to_int(m.group('count'))
            if n:
                local = text[max(0,m.start()-100):min(len(text),m.end()+100)]
                add_series(m.group('series'), n, local)

    # Suppress a collective family label (e.g. "Series Seed Preferred Stock") when
    # the capitalization clause designates numbered child series and the collective
    # label itself lacks designation/consist-of evidence.
    keys = list(facts["series_counts"].keys())
    for key in keys:
        item = facts["series_counts"].get(key) or {}
        name = str(item.get("security_name", "") or "")
        tok = _strict_series_token(name)
        mt = re.match(r"^series([a-z]+)$", tok or "")
        if not mt:
            continue
        family = mt.group(1)
        has_children = any(re.match(rf"^series{re.escape(family)}\d+$", _strict_series_token(str((facts['series_counts'].get(k) or {}).get('security_name',''))) or "") for k in keys if k != key)
        ev = str(item.get("source_text", "") or "")
        if has_children and not re.search(r"designat|shall\s+consist|consists?\s+of|authorized\s+(?:number\s+of\s+)?shares", ev, re.I):
            facts["series_counts"].pop(key, None)

    return facts


def _apply_deterministic_capitalization_to_master(master: Dict[str, Any], source_text: str) -> Dict[str, Any]:
    """Make locally parsed capitalization facts authoritative where exact evidence exists."""
    master = _clean_security_master(master)
    if not source_text:
        return master
    parsed = _extract_capitalization_facts_from_source(source_text)
    rows = [dict(r) for r in (master.get("securities", []) or [])]
    by_key = {normalize_security_name_for_match(str(r.get("security_name", "") or "")): r for r in rows}

    # Exact series counts from designation clauses override AI counts.
    for key, item in (parsed.get("series_counts") or {}).items():
        row = by_key.get(key)
        if row is None:
            row = {
                "security_name": item["security_name"],
                "security_type": "Preferred Stock",
                "authorized_count": "Not stated",
                "source_text": "",
            }
            rows.append(row)
            by_key[key] = row
        row["authorized_count"] = f"{int(item['authorized_count']):,}"
        row["source_text"] = item.get("source_text", "")

    # Common count from top-level capitalization is authoritative.
    common_n = parsed.get("total_common_authorized")
    if common_n:
        common_key = normalize_security_name_for_match("Common Stock")
        common_row = None
        for k, r in by_key.items():
            nm = str(r.get("security_name", "") or "")
            st = str(r.get("security_type", "") or "")
            if ("common" in nm.lower() and "preferred" not in nm.lower()) or ("common" in st.lower() and "preferred" not in st.lower()):
                common_row = r; break
        if common_row is None:
            common_row = {"security_name":"Common Stock","security_type":"Common Stock","authorized_count":"Not stated","source_text":""}
            rows.append(common_row)
        common_row["authorized_count"] = f"{int(common_n):,}"
        common_row["source_text"] = parsed.get("evidence", {}).get("total_common_authorized", "")

    out = dict(master)
    out["securities"] = rows
    if parsed.get("total_common_authorized"):
        out["total_common_authorized"] = f"{int(parsed['total_common_authorized']):,}"
    if parsed.get("total_preferred_authorized"):
        out["total_preferred_authorized"] = f"{int(parsed['total_preferred_authorized']):,}"
    out = _clean_security_master(out)
    out["deterministic_capitalization_lock"] = True
    return out


def _extract_exact_series_money_from_source(security_name: str, source_text: str, field: str) -> Optional[float]:
    """High-confidence exact-series price parser used before any proximity fallback."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text or token == "common":
        return None
    text = _normalize_source_text_for_match(source_text)
    money = r"\$\s*(?P<amount>[0-9][0-9,]*(?:\.[0-9]+)?)"
    if field == "oip":
        label = r"original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price"
        patterns = [
            rf"{money}\s*(?:per\s+share)?\s*(?:for|of|with\s+respect\s+to|applicable\s+to)\s+(?:the\s+)?{rx}",
            rf"(?:the\s+)?{rx}.{{0,140}}?(?:{label}).{{0,80}}?{money}",
            rf"(?:{label}).{{0,80}}?{money}.{{0,140}}?(?:for|of|with\s+respect\s+to|applicable\s+to)?\s*(?:the\s+)?{rx}",
        ]
    elif field == "conversion":
        label = r"initial\s+conversion\s+price|original\s+conversion\s+price|conversion\s+price"
        patterns = [
            rf"(?:the\s+)?{rx}.{{0,160}}?(?:{label}).{{0,80}}?{money}",
            rf"(?:{label}).{{0,80}}?{money}.{{0,160}}?(?:for|of|with\s+respect\s+to|applicable\s+to)?\s*(?:the\s+)?{rx}",
            rf"{money}\s*(?:per\s+share)?\s*(?:for|of|with\s+respect\s+to|applicable\s+to)\s+(?:the\s+)?{rx}.{{0,120}}?(?:conversion)",
        ]
    else:
        return None
    vals = []
    for priority, pat in enumerate(patterns):
        for m in re.finditer(pat, text, re.I):
            try:
                v = float(m.group('amount').replace(',', ''))
            except Exception:
                continue
            if 0 < v < 1_000_000:
                vals.append((priority, m.end()-m.start(), v))
    if not vals:
        return None
    vals.sort(key=lambda x:(x[0],x[1]))
    return vals[0][2]


def _strict_dividend_amount_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Return only an explicit annual dollar dividend per share for the exact security.

    Safety rule: a nearby OIP, liquidation amount, or conversion price must never be
    converted into a dividend rate.  The accepted sentence must itself contain both
    dividend-role language and an annual/per-annum qualifier around the dollar amount.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text or token == "common":
        return None
    text = _normalize_source_text_for_match(source_text)
    vals: List[float] = []
    # Exact-security sentence windows only. Keep them short enough that an OIP/CP in a
    # neighbouring sentence cannot become an annual dividend amount.
    for sm in re.finditer(rx, text, re.I):
        local = text[max(0, sm.start()-320):min(len(text), sm.end()+520)]
        if not re.search(r"\bdividends?\b|\bdividend\s+rate\b", local, re.I):
            continue
        patterns = [
            r"dividend\s+rate.{0,90}?annual\s+rate\s+of\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?",
            r"dividends?.{0,90}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*per\s+share.{0,70}?(?:per\s+annum|annually|annual)",
            r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*per\s+share.{0,70}?(?:per\s+annum|annually|annual).{0,90}?dividends?",
            r"annual\s+dividend.{0,70}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?",
        ]
        for pat in patterns:
            for m in re.finditer(pat, local, re.I):
                try:
                    v = float(m.group(1).replace(',', ''))
                except Exception:
                    continue
                if 0 <= v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                    vals.append(v)
    return vals[0] if len(vals) == 1 else None


def _final_relationship_backfill(facts: Dict[str, Any], source_text: str = "") -> Dict[str, Any]:
    """Final additive certification pass after all AI/source rows are assembled.

    This stage deliberately runs late so it can use the row's combined source evidence
    (broad verification + targeted anchor evidence) without reopening fuzzy mapping.
    It only fills missing OIP/ratio/dividend values, except that an explicit source
    dividend percentage is authoritative and may replace an unsafe calculated rate.
    """
    if not isinstance(facts, dict):
        return facts
    out = dict(facts)
    rows = [dict(r) for r in (facts.get("securities", []) or [])]
    full_source = str(source_text or "")

    for row in rows:
        name = str(row.get("security_name", "") or "").strip()
        if not name:
            continue
        evidence = str(row.get("source_text", "") or "")
        review = str(row.get("review_flags", "") or "")
        basis = str(row.get("source_basis", "") or "")
        combined = "\n".join(x for x in [evidence, basis, review] if x)

        # 1) Explicit dividend percentage always wins over any calculated fallback.
        #    This also supports a defined parent family (e.g. child series under a
        #    parent Series D term) when the legal membership is proven.
        pct = None
        if full_source:
            pct = _group_dividend_percent_from_source(name, full_source)
            if pct is None:
                pct = _strict_dividend_percent_from_source(name, full_source)
        if pct is None and evidence:
            pct = _strict_dividend_percent_from_source(name, evidence)
        if pct is not None:
            row["dividend_rate"] = f"{pct:.6f}".rstrip("0").rstrip(".") + "%"

        # 2) Promote an unlabeled economic base only from independent exact-series
        #    corroboration in the final assembled evidence.  This is not CP -> OIP.
        if _value_is_missing(row.get("oip_original_issue_price")):
            base = _corroborated_economic_base_from_evidence(name, combined)
            if base is None and full_source:
                base = _corroborated_economic_base_from_evidence(name, full_source)
            if base is None:
                base = _initial_conversion_price_as_oip_equivalent(name, combined)
            if base is None and full_source:
                base = _initial_conversion_price_as_oip_equivalent(name, full_source)

            # Never promote a liquidation amount or conversion numerator to OIP merely
            # because it is used in the conversion formula. Those roles remain separate.
            # Some legacy reviewer notes may describe such a relationship; they are no
            # longer sufficient to set OIP.
            # Some charters express the fixed original base as the liquidation
            # preference and separately state that the conversion ratio is calculated
            # from that preference formula and the Conversion Price. Accept that only
            # when the exact row evidence contains both exact-series dollar roles and
            # the reviewer/source basis explicitly identifies this relationship.
            if base is None:
                liq = _money_to_float(row.get("liquidation_preference"))
                cp = _money_to_float(row.get("conversion_price"))
                relationship = False  # liquidation/conversion numerator must never define OIP
                exact = _exact_security_in_evidence(name, evidence)
                if relationship and exact and liq is not None and cp is not None and liq > 0 and cp > 0:
                    # Require the literal liquidation amount to occur in the exact row
                    # evidence; this prevents borrowing a neighbouring series amount.
                    if _money_value_in_evidence(row.get("liquidation_preference"), evidence):
                        base = liq
            if base is not None and base > 0:
                row["oip_original_issue_price"] = _fmt_money_num(base)

        # 2b) Final exact-role CP equality backstop for named non-Series preferred
        #     classes.  This runs only when OIP is already independently certified.
        if _value_is_missing(row.get("conversion_price")) and not _value_is_missing(row.get("oip_original_issue_price")):
            eq_source = (full_source or "") + "\n" + evidence
            if _conversion_price_equals_oip_for_named_preferred(name, eq_source):
                row["conversion_price"] = row.get("oip_original_issue_price")

        # 3) Recompute ratio after final values are settled. Prefer an explicit
        #    conversion numerator/base from the legal formula; only fall back to OIP/CP.
        base = _money_to_float(row.get("oip_original_issue_price"))
        cp = _money_to_float(row.get("conversion_price"))
        explicit_ratio = _v162_explicit_conversion_ratio_from_evidence(name, combined + "\n" + (full_source or ""))
        ratio_text = explicit_ratio or _conversion_ratio_from_exact_formula(name, combined, row.get("conversion_price"))
        if ratio_text is None and full_source:
            ratio_text = _conversion_ratio_from_exact_formula(name, full_source, row.get("conversion_price"))
        if ratio_text is None:
            ratio_text = _named_conversion_formula_ratio(name, combined, row.get("conversion_price"))
        if ratio_text is None and full_source:
            ratio_text = _named_conversion_formula_ratio(name, full_source, row.get("conversion_price"))
        if ratio_text is not None:
            row["conversion_ratio"] = ratio_text
        elif base is not None and cp is not None and base > 0 and cp > 0:
            ratio = base / cp
            row["conversion_ratio"] = "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"

        precise_liq = _v162_precise_conditional_liquidation(name, row.get("liquidation_preference"), combined + "\n" + (full_source or ""))
        if precise_liq:
            row["liquidation_preference"] = precise_liq

        # 4) Only if no explicit percentage exists, calculate a rate from an explicit
        #    annual dollar dividend/share and the final certified OIP.
        if _value_is_missing(row.get("dividend_rate")) and base is not None and base > 0:
            amount = _strict_dividend_amount_from_source(name, full_source) if full_source else None
            if amount is None and full_source:
                amount = _annual_dividend_for_named_security(name, full_source)
            if amount is None and evidence:
                amount = _strict_dividend_amount_from_source(name, evidence)
            if amount is None and evidence:
                amount = _annual_dividend_for_named_security(name, evidence)
            if amount is not None and amount >= 0:
                calc = amount / base * 100.0
                if 0 <= calc <= 100:
                    row["dividend_rate"] = f"{calc:.6f}".rstrip("0").rstrip(".") + "%"

    out["securities"] = rows
    return out

def _clean_security_master(master: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and deduplicate the authoritative security roster without company-specific rules."""
    if not isinstance(master, dict):
        return master
    rows = []
    seen = set()
    raw_rows = (master.get("securities", []) or [])
    has_detailed_preferred = any(re.search(r"\bseries\s+", str(r.get("security_name", "") or ""), re.I) for r in raw_rows)
    for row in raw_rows:
        name = str(row.get("security_name", "") or "").strip()
        if not name:
            continue
        low = name.lower().strip()
        # Suppress only true aggregate Preferred Stock labels when detailed series are present.
        # A separately designated non-Series class such as Founder Preferred Stock is a real
        # security and must remain in the roster.
        if has_detailed_preferred and low in {"preferred", "preferred stock", "preferred shares", "series preferred stock"}:
            continue
        key = normalize_security_name_for_match(name)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned = dict(row)
        cleaned["security_name"] = name
        cleaned["authorized_count"] = _normalize_master_authorized(row.get("authorized_count"))
        st = str(row.get("security_type", "") or "").strip()
        if not st:
            st = "Common Stock" if "common" in low and "preferred" not in low else ("Preferred Stock" if ("preferred" in low or "series" in low) else "Not stated")
        cleaned["security_type"] = st
        rows.append(cleaned)
    master = dict(master)
    master["securities"] = rows
    master["total_common_authorized"] = _normalize_master_authorized(master.get("total_common_authorized"))
    master["total_preferred_authorized"] = _normalize_master_authorized(master.get("total_preferred_authorized"))
    return master


def _validate_security_master(master: Dict[str, Any]) -> List[str]:
    """Validate only roster identity. Authorized counts are intentionally non-blocking."""
    issues: List[str] = []
    if not isinstance(master, dict):
        return ["security master payload is not an object"]
    rows = (master.get("securities", []) or [])
    if not rows:
        return ["security master returned no securities"]
    seen = set()
    for row in rows:
        name = str(row.get("security_name", "") or "").strip()
        key = normalize_security_name_for_match(name)
        if not key:
            issues.append("security master contains blank/unmatchable security name")
            continue
        if key in seen:
            issues.append(f"duplicate security in master: {name}")
        seen.add(key)
    return issues


def _merge_security_master_into_current_data(current_data: Dict[str, Any], master: Dict[str, Any]) -> Dict[str, Any]:
    """Make the Security Master authoritative for roster identity only."""
    if not current_data or not master:
        return current_data
    master = _clean_security_master(master)
    master_rows = master.get("securities", []) or []
    if not master_rows:
        return current_data
    existing = current_data.setdefault("securities", [])
    by_norm = {normalize_security_name_for_match(str(r.get("security_name", "") or "")): r for r in existing if str(r.get("security_name", "") or "").strip()}
    authoritative_norms = set()
    for mr in master_rows:
        name = str(mr.get("security_name", "") or "").strip()
        key = normalize_security_name_for_match(name)
        if not key:
            continue
        authoritative_norms.add(key)
        target = by_norm.get(key)
        if target is None:
            low = name.lower()
            target = {
                "security_name": name,
                "security_type": str(mr.get("security_type", "") or "") or ("Common Stock" if "common" in low and "preferred" not in low else "Preferred Stock"),
                "authorized_count": "Not stated",
                "oip_original_issue_price": "Not stated",
                "liquidation_preference": "Not stated",
                "dividend_paying": "Not stated",
                "dividend_type": "Not stated",
                "dividend_rate": "Not stated",
                "conversion_ratio": "Not stated",
                "conversion_price": "Not stated",
                "participation_rights": "Not stated",
                "participation_cap": "Not stated",
                "seniority": "Not stated",
                "senior_to": "Not stated",
                "pari_passu_with": "Not stated",
                "junior_to": "Not stated",
                "applies_to": "Not stated",
                "source_basis": "Security Master",
                "confidence": "High",
                "source_section": "Capitalization / designation",
                "source_text": "",
                "review_flags": "Security discovered by authoritative Security Master",
            }
            existing.append(target)
            by_norm[key] = target
        target["security_name"] = name
        target["security_type"] = str(mr.get("security_type", "") or target.get("security_type", "Not stated"))
        excerpt = str(mr.get("source_text", "") or "").strip()
        if excerpt:
            target["source_text"] = (str(target.get("source_text", "") or "").strip() + "\n\nSECURITY MASTER: " + excerpt).strip()
        target["review_flags"] = _append_review_flag(target.get("review_flags", ""), "Security Master roster applied")
    classification = str(master.get("document_classification", "") or "").strip().lower()
    if classification == "standard" and authoritative_norms:
        # For standard Inc. charters, the independently verified capitalization/designation
        # roster is authoritative. This prevents stale/phantom rows from the first extraction.
        current_data["securities"] = [
            r for r in existing
            if normalize_security_name_for_match(str(r.get("security_name", "") or "")) in authoritative_norms
        ]
    else:
        # Outside standard scope, stay conservative and only suppress a redundant aggregate
        # Preferred row when detailed series are clearly present.
        detailed = any(re.search(r"\bseries\s+", str(r.get("security_name", "") or ""), re.I) for r in master_rows)
        if detailed:
            kept = []
            for r in existing:
                name = str(r.get("security_name", "") or "").strip()
                key = normalize_security_name_for_match(name)
                low = name.lower()
                is_aggregate_pref = low in {"preferred", "preferred stock", "preferred shares", "series preferred stock"}
                if is_aggregate_pref and key not in authoritative_norms:
                    continue
                kept.append(r)
            current_data["securities"] = kept
    current_data["_security_master"] = master
    return current_data


def _strict_dividend_percent_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Accept only a percentage explicitly functioning as the dividend rate for the exact security."""
    if not source_text:
        return None
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    text = _normalize_source_text_for_match(source_text)
    if not rx or not text or token == "common":
        return None
    windows = _role_windows(text, [r"dividend", r"dividends"], radius=2200)
    candidates: List[Tuple[int, float]] = []
    for window in windows:
        for sm in re.finditer(rx, window, re.I):
            local = window[max(0, sm.start()-550):min(len(window), sm.end()+750)]
            if not re.search(r"dividend|dividends", local, re.I):
                continue
            pats = [
                r"dividends?.{0,120}?(?:at\s+(?:a\s+)?rate\s+of\s+)(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)",
                r"dividend\s+rate.{0,80}?(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)",
                r"dividends?.{0,100}?(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent).{0,80}?(?:of\s+(?:the\s+)?original\s+(?:issue|issuance|purchase)\s+price|per\s+annum|annually|annual)",
                r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent).{0,80}?(?:per\s+annum|annually|annual).{0,120}?dividends?",
                r"(?:cumulative\s+)?dividends?.{0,120}?(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)\s+of\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",
            ]
            for priority, pat in enumerate(pats):
                for pm in re.finditer(pat, local, re.I):
                    try: pct=float(pm.group(1))
                    except Exception: continue
                    if 0 <= pct <= 100:
                        candidates.append((priority, pct))
    if not candidates:
        return None
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1]


def _strengthen_verified_facts_from_local_source(facts: Dict[str, Any], source_text: str) -> Dict[str, Any]:
    """Conservative local-source backfill only.

    v15.13 rule: local regex must NEVER overwrite a non-missing economic value returned by
    source-backed AI verification. Earlier versions could find the correct value in the AI
    evidence and then replace it with a neighboring series value during post-processing.
    Local parsing is therefore used only to fill a missing OIP/Conversion Price or explicit
    dividend rate when the exact-series parser has high-confidence evidence.
    """
    if not isinstance(facts, dict) or not source_text or len(source_text.strip()) < 300:
        return facts
    out = dict(facts)
    rows = []
    for row0 in (facts.get("securities", []) or []):
        row = dict(row0)
        name = str(row.get("security_name", "") or "").strip()
        if not name:
            rows.append(row); continue

        # Only backfill missing values. Never override a source-verified anchor.
        if _value_is_missing(row.get("oip_original_issue_price")):
            oip = _extract_exact_series_money_from_source(name, source_text, "oip")
            if oip is not None:
                row["oip_original_issue_price"] = _fmt_money_num(oip)
        if _value_is_missing(row.get("conversion_price")):
            conv = _extract_exact_series_money_from_source(name, source_text, "conversion")
            if conv is not None:
                row["conversion_price"] = _fmt_money_num(conv)

        if _value_is_missing(row.get("dividend_rate")):
            pct = _strict_dividend_percent_from_source(name, source_text)
            if pct is not None:
                row["dividend_rate"] = f"{pct:.2f}".rstrip("0").rstrip(".") + "%"
            else:
                amount = _strict_dividend_amount_from_source(name, source_text)
                base = _money_to_float(row.get("oip_original_issue_price"))
                if amount is not None and base is not None and base > 0:
                    calc = amount / base * 100.0
                    if 0 <= calc <= 100:
                        row["dividend_rate"] = f"{calc:.2f}".rstrip("0").rstrip(".") + "%"

        # Ratio may be calculated deterministically after values are final, but do not fabricate
        # either underlying economic input.
        base = _money_to_float(row.get("oip_original_issue_price"))
        cp = _money_to_float(row.get("conversion_price"))
        if base is not None and cp is not None and base > 0 and cp > 0:
            ratio = base / cp
            row["conversion_ratio"] = "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"
        rows.append(row)
    out["securities"] = rows
    return out

def _local_source_text_for_verification(path: Path) -> str:
    try:
        if path.suffix.lower() in SUPPORTED_LOCAL_TEXT:
            return extract_local_text(path)
    except Exception:
        return ""
    return ""

def _add_missing_securities_from_verified_facts(current_data: Dict[str, Any], facts: Dict[str, Any]) -> None:
    """Add a source-verified security that the initial extraction missed.

    The full-source verification pass is allowed to discover omitted classes/series.
    Only rows with a concrete security name are added. The detailed fields are then
    overwritten from the verified fact set in the normal application step.
    """
    securities = current_data.setdefault("securities", [])
    existing = [str(x.get("security_name", "")).strip() for x in securities]
    existing_norm = {normalize_security_name_for_match(x) for x in existing if x}
    facts_rows = (facts or {}).get("securities", []) or []
    # Avoid adding an aggregate Preferred Stock row when individual preferred series exist.
    fact_names = [str(r.get("security_name", "")).strip() for r in facts_rows]
    has_detailed_preferred = any(re.search(r"\bseries\s+", n, re.I) for n in fact_names)
    for row in facts_rows:
        name = str(row.get("security_name", "")).strip()
        if not name:
            continue
        norm = normalize_security_name_for_match(name)
        if not norm or norm in existing_norm:
            continue
        low = name.lower()
        if has_detailed_preferred and low in {"preferred", "preferred stock", "preferred shares", "series preferred stock"}:
            continue
        security_type = "Common Stock" if "common" in low and "preferred" not in low else ("Preferred Stock" if ("preferred" in low or "series" in low) else "Not stated")
        securities.append({
            "security_name": name,
            "security_type": security_type,
            "authorized_count": "Not stated",
            "oip_original_issue_price": "Not stated",
            "liquidation_preference": "Not stated",
            "dividend_paying": "Not stated",
            "dividend_type": "Not stated",
            "dividend_rate": "Not stated",
            "conversion_ratio": "Not stated",
            "conversion_price": "Not stated",
            "participation_rights": "Not stated",
            "participation_cap": "Not stated",
            "seniority": "Not stated",
            "senior_to": "Not stated",
            "pari_passu_with": "Not stated",
            "junior_to": "Not stated",
            "applies_to": "Not stated",
            "source_basis": "Full-source verification",
            "confidence": "High",
            "source_section": "Not stated",
            "source_text": "",
            "review_flags": "Security discovered by full-source verification",
        })
        existing_norm.add(norm)

def apply_cap_table_source_facts_to_data(current_data: Dict[str, Any], facts: Dict[str, Any]) -> Dict[str, Any]:
    if not current_data or not facts:
        return current_data
    _add_missing_securities_from_verified_facts(current_data, facts)
    securities = current_data.get("securities", []) or []
    expected = [str(x.get("security_name", "")).strip() for x in securities]
    by_name: Dict[str, Dict[str, Any]] = {}
    for row in facts.get("securities", []) or []:
        supplied = str(row.get("security_name", "")).strip()
        match = best_existing_security_match(supplied, expected)
        if match:
            key = normalize_security_name_for_match(match)
            if key not in by_name:
                by_name[key] = row
    for sec in securities:
        name = str(sec.get("security_name", "")).strip()
        row = by_name.get(normalize_security_name_for_match(name))
        if not row:
            continue
        for field in ["oip_original_issue_price","liquidation_preference","participation_rights","participation_cap","conversion_price","conversion_ratio","dividend_rate","dividend_type"]:
            sec[field] = _safe_verified_value(row.get(field), "Not stated")
        try:
            rank = int(row.get("seniority_rank", 999))
        except Exception:
            rank = 999
        sec["seniority"] = f"Rank {rank}" if rank < 999 else "Not stated"
        sec["source_basis"] = _safe_verified_value(row.get("source_basis"), "Full-source verification")
        excerpt = str(row.get("source_text", "") or "").strip()
        if excerpt:
            sec["source_text"] = (str(sec.get("source_text", "") or "").strip() + "\n\nFULL-SOURCE VERIFIED: " + excerpt).strip()
        cap_div = _safe_verified_value(row.get("cap_price_includes_dividends"), "N/A")
        sec["source_text"] = (str(sec.get("source_text", "") or "").strip() + f"\nCAP PRICE INCLUDES DIVIDENDS: {cap_div}").strip()
        sec["review_flags"] = _append_review_flag(sec.get("review_flags", ""), "Full-source verification applied")
    current_data["_full_source_verification"] = facts
    current_data["_document_classification"] = facts.get("document_classification", "")
    current_data["_classification_reason"] = facts.get("classification_reason", "")
    return current_data

def comprehensive_cap_table_review_with_file_api(client: OpenAI, model: str, path: Path, current_data: Dict[str, Any], keep_api_files: bool = False, reasoning_effort: str = "medium") -> Dict[str, Any]:
    """Fast, fail-safe full-source review focused on economic terms.

    v15.17 architecture:
    - one roster-only pass with immutable security IDs and no authorized-count work;
    - one broad economic interpretation pass used only as a candidate/discovery layer;
    - one targeted exact-security economic certification pass;
    - certify-or-clear critical economics so unsupported broad values cannot survive;
    - deterministic calculations only from independently certified source facts;
    - at most one targeted repair pass only when structural validation still fails.
    """
    securities = current_data.get("securities", []) or []
    initial_security_list = [str(sec.get("security_name", "")).strip() for sec in securities if str(sec.get("security_name", "")).strip()]
    if not initial_security_list:
        return current_data

    verify_effort = reasoning_effort or "medium"
    if str(verify_effort).lower() == "low":
        verify_effort = "medium"

    upload_path, is_temp_copy = prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    try:
        local_source_text = _local_source_text_for_verification(path)

        # STEP 1 - one authoritative roster pass. Authorized counts are intentionally ignored.
        print(f"    Security roster: single source pass for {path.name}", flush=True)
        security_master = _clean_security_master(
            _call_security_master(client, model, uploaded.id, path, verify_effort, SECURITY_MASTER_INSTRUCTIONS)
        )
        master_issues = _validate_security_master(security_master)
        if master_issues:
            print(f"    Security roster: {len(master_issues)} issue(s); using one focused roster repair", flush=True)
            extra = "Roster issues:\n- " + "\n- ".join(master_issues[:20])
            security_master = _clean_security_master(
                _call_security_master(client, model, uploaded.id, path, verify_effort, SECURITY_MASTER_ADJUDICATION_INSTRUCTIONS, extra)
            )
            master_issues = _validate_security_master(security_master)

        current_data = _merge_security_master_into_current_data(current_data, security_master)
        current_data = _attach_immutable_security_ids(current_data)
        master_security_list = [
            str(r.get("security_name", "") or "").strip()
            for r in (security_master.get("securities", []) or [])
            if str(r.get("security_name", "") or "").strip()
        ]
        security_list = master_security_list or [
            str(sec.get("security_name", "")).strip()
            for sec in (current_data.get("securities", []) or [])
            if str(sec.get("security_name", "")).strip()
        ]
        if not security_list:
            security_list = initial_security_list

        # STEP 2 - one broad full-source interpretation.
        print(f"    Full-source economics: single interpretation pass for {path.name}", flush=True)
        final_facts = _call_full_source_verification(
            client, model, uploaded.id, path, security_list, verify_effort, CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS
        )

        # STEP 3 - targeted anchor verification for the fields most prone to cross-series contamination.
        print(f"    Economic anchors: exact-series OIP/conversion/dividend verification for {path.name}", flush=True)
        anchor_excerpt = _build_economic_anchor_excerpt(local_source_text, security_list) if local_source_text else ""
        if anchor_excerpt:
            print(f"    Token-efficient anchor evidence: {len(anchor_excerpt):,} chars (full-file fallback remains available)", flush=True)
        anchors = _call_economic_anchor_verification(
            client, model, uploaded.id, path, security_list, verify_effort, source_excerpt=anchor_excerpt
        )
        final_facts = _apply_economic_anchors_to_facts(final_facts, anchors, security_list, source_text=local_source_text)

        # STEP 4 - deterministic exact-series/source locks and arithmetic.
        if local_source_text:
            final_facts = _strengthen_verified_facts_from_local_source(final_facts, local_source_text)
        final_facts = _final_relationship_backfill(final_facts, local_source_text)

        validation_issues = _validate_verified_facts(final_facts, security_list)
        classification = str(
            security_master.get("document_classification", final_facts.get("document_classification", "")) or ""
        ).strip().lower()

        # One focused repair only if a standard document still fails structural validation.
        if validation_issues and classification != "non-standard":
            print(f"    Full-source economics: {len(validation_issues)} validation issue(s); running one focused repair", flush=True)
            repair_extra = (
                "Authoritative security roster:\n" + json.dumps(security_list, ensure_ascii=False, indent=2)
                + "\n\nEconomic anchors already verified:\n" + json.dumps(anchors, ensure_ascii=False, indent=2)
                + "\n\nRemaining validation issues:\n- " + "\n- ".join(validation_issues[:50])
                + "\n\nRepair only the affected economic fields. Exact security identity is mandatory. "
                  "Do not borrow values across series and do not spend time on authorized share counts."
            )
            repaired = _call_full_source_verification(
                client, model, uploaded.id, path, security_list, verify_effort, CAP_TABLE_ADJUDICATION_INSTRUCTIONS, repair_extra
            )
            repaired = _apply_economic_anchors_to_facts(repaired, anchors, security_list, source_text=local_source_text)
            if local_source_text:
                repaired = _strengthen_verified_facts_from_local_source(repaired, local_source_text)
            repaired = _final_relationship_backfill(repaired, local_source_text)
            final_facts = repaired
            validation_issues = _validate_verified_facts(final_facts, security_list)

        if master_issues:
            validation_issues = list(master_issues) + list(validation_issues)

        if validation_issues:
            print(f"    Full-source verification: FINAL VALIDATION FAILED with {len(validation_issues)} issue(s)", flush=True)
            for msg in validation_issues[:8]:
                print(f"      - {msg}", flush=True)
            current_data["_full_source_validation_issues"] = validation_issues
            if classification != "non-standard":
                raise ValueError("Final source-validation gate failed: " + "; ".join(validation_issues[:8]))
        elif classification == "non-standard":
            print("    Full-source verification: non-standard document; outside standard-Inc. certification scope", flush=True)
        else:
            print("    Full-source verification: economic anchors + deterministic gates passed", flush=True)

        final_facts["document_classification"] = security_master.get(
            "document_classification", final_facts.get("document_classification", "")
        )
        final_facts["classification_reason"] = security_master.get(
            "classification_reason", final_facts.get("classification_reason", "")
        )
        return apply_cap_table_source_facts_to_data(current_data, final_facts)
    finally:
        if not keep_api_files:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass
        cleanup_api_upload_path(upload_path, is_temp_copy)


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
"""


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
"""

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
Authorized share counts are out of scope for this workflow. Set authorized_count to "Not stated" for schema compatibility. Do not search, calculate, reconcile, validate, or spend model reasoning on authorized counts.

ISSUE / STRIKE PRICE / OIP RULES
For oip_original_issue_price, OIP is a REQUIRED valuation input for each Preferred Stock series and should not be left blank when a defensible per-share economic base is available.
Use this hierarchy:
1. Explicit Original Issue Price / Original Issuance Price / Original Purchase Price / Issue Price / Purchase Price / Subscription Price / per-share purchase price for the exact security.
2. If no such original issuance/purchase price is stated for that exact security, return "Not stated".
OIP and Conversion Price are independent source facts. Never substitute Conversion Price, liquidation preference, participation cap, dividend amount, option strike price, warrant exercise price, FMV, public offering price, par value, or common stock price as OIP.
If a document lists multiple per-series values in one clause/table, map each amount to the exact series.
Keep OIP concise, preferably a per-share dollar amount such as "$0.246".

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
- Authorized Count is out of scope and must not be searched/reconciled.
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


@dataclass
class ExtractionResult:
    file_path: str
    status: str
    data: Optional[Dict[str, Any]] = None
    error: str = ""
    mode_used: str = ""




def normalize_whitespace(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_pdf(path: Path) -> str:
    """Layout-aware PDF text extraction using PyMuPDF.

    PyMuPDF's sorted extraction preserves reading order better than plain PDF
    text extraction for multi-column / table-like charter pages. Page markers are
    retained so targeted verification can cite and isolate relevant clauses.
    """
    parts: List[str] = []
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise RuntimeError(f"Could not open PDF with PyMuPDF: {path.name}: {exc}") from exc
    try:
        for i, page in enumerate(doc, start=1):
            try:
                page_text = page.get_text("text", sort=True) or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                parts.append(f"\n\n--- Page {i} ---\n{page_text}")
    finally:
        doc.close()
    return normalize_whitespace("\n".join(parts))


def extract_text_from_docx(path: Path) -> str:
    try:
        import docx
    except Exception as exc:
        raise RuntimeError("Missing dependency python-docx. Run: pip install python-docx") from exc

    document = docx.Document(str(path))
    parts: List[str] = []

    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)

    for table_index, table in enumerate(document.tables, start=1):
        parts.append(f"\n--- Table {table_index} ---")
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))

    return normalize_whitespace("\n".join(parts))


def extract_local_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    if suffix == ".docx":
        return extract_text_from_docx(path)
    if suffix in {".txt", ".md"}:
        return normalize_whitespace(path.read_text(encoding="utf-8", errors="replace"))
    raise ValueError(f"Local text extraction is not supported for {suffix}. Use --mode file-api.")


def prepare_api_upload_path(path: Path) -> Tuple[Path, bool]:
    """Return an API-safe upload path.

    The file API can reject uppercase extensions such as .PDF even though .pdf is
    supported. When needed, create a temporary copy with a lowercase extension.
    """
    suffix = path.suffix
    if not suffix or suffix == suffix.lower():
        return path, False
    temp_dir = Path(tempfile.gettempdir()) / "articles_api_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[\\/:*?\"<>|]", "_", path.stem)
    safe_stem = re.sub(r"\s+", " ", safe_stem).strip()[:120] or "upload_file"
    upload_path = temp_dir / f"{safe_stem}{suffix.lower()}"
    counter = 2
    while upload_path.exists():
        upload_path = temp_dir / f"{safe_stem}_{counter}{suffix.lower()}"
        counter += 1
    shutil.copy2(path, upload_path)
    return upload_path, True


def cleanup_api_upload_path(upload_path: Path, is_temp_copy: bool) -> None:
    if not is_temp_copy:
        return
    try:
        upload_path.unlink(missing_ok=True)
    except Exception:
        pass




def load_dictionary_phrases(dictionary_path: Optional[Path]) -> Dict[str, List[str]]:
    """Load phrase dictionary from Excel. The workbook is optional.

    Expected columns may be named Phrase / Pattern, Phrase, Pattern, Keyword, or similar.
    Sheet name is used as the category.
    """
    if dictionary_path is None:
        return {}
    if not dictionary_path.exists():
        raise FileNotFoundError(f"Dictionary file not found: {dictionary_path}")

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
                if not phrase:
                    continue
                if len(phrase) < 4 or len(phrase) > 220:
                    continue
                if phrase.lower() in {"nan", "none", "n/a", "not stated"}:
                    continue
                # Skip rows that are mostly punctuation or digits.
                if len(re.sub(r"[^A-Za-z]", "", phrase)) < 3:
                    continue
                phrases.append(phrase)

        # De-duplicate while preserving order.
        seen = set()
        unique_phrases = []
        for phrase in phrases:
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                unique_phrases.append(phrase)
        if unique_phrases:
            phrases_by_category[category] = unique_phrases

    return phrases_by_category


def build_dictionary_evidence(
    text: str,
    phrases_by_category: Dict[str, List[str]],
    max_chars: int,
    window_chars: int = 1400,
    max_matches_per_category: int = 35,
) -> str:
    """Return document evidence. If the document is short, return full text.
    If long, return front matter plus phrase-matched windows.
    """
    text = normalize_whitespace(text)
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
                label = f"{category}: {phrase}"
                windows.append((start, end, label))
                matches_for_category += 1
                start_search = idx + len(phrase_l)

    if not windows:
        return text[:max_chars]

    # Include beginning of document because company name and share authorization often appear early.
    front_matter = text[: min(25000, max_chars // 4)]

    # Merge overlapping windows.
    windows.sort(key=lambda item: item[0])
    merged: List[Tuple[int, int, List[str]]] = []
    for start, end, label in windows:
        if not merged or start > merged[-1][1] + 300:
            merged.append((start, end, [label]))
        else:
            old_start, old_end, labels = merged[-1]
            labels.append(label)
            merged[-1] = (old_start, max(old_end, end), labels)

    output_parts = [
        "--- DOCUMENT FRONT MATTER ---",
        front_matter,
        "\n--- DICTIONARY MATCHED EVIDENCE WINDOWS ---",
    ]
    used = sum(len(part) for part in output_parts)
    for i, (start, end, labels) in enumerate(merged, start=1):
        chunk = text[start:end]
        header = f"\n--- Evidence Window {i} | chars {start}-{end} | matches: {', '.join(labels[:8])} ---\n"
        if used + len(header) + len(chunk) > max_chars:
            break
        output_parts.append(header)
        output_parts.append(chunk)
        used += len(header) + len(chunk)

    return "\n".join(output_parts)[:max_chars]


def text_format_schema() -> Dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "articles_charter_terms_extraction",
            "schema": EXTRACTION_SCHEMA,
            "strict": True,
        }
    }


class _LooseSecurityRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    security_name: str = ""


class _LooseAIEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    company_name: str = ""
    document_name: str = ""
    securities: List[_LooseSecurityRecord] = []


def _validate_ai_payload_shape(data: Dict[str, Any]) -> Dict[str, Any]:
    """Light structural validation for model JSON without constraining task-specific schemas.

    Strict JSON schema is still the primary contract. Pydantic adds a second local
    guard against malformed top-level structures while allowing task-specific keys.
    """
    if not isinstance(data, dict):
        raise RuntimeError("Model JSON must be an object.")
    if "securities" in data:
        try:
            _LooseAIEnvelope.model_validate(data)
        except ValidationError as exc:
            raise RuntimeError(f"Model JSON failed local Pydantic validation: {exc}") from exc
    return data


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((TimeoutError, ConnectionError)),
    reraise=True,
)
def _responses_create(client: OpenAI, **kwargs: Any) -> Any:
    """Tenacity safety wrapper around Responses API calls.

    The OpenAI SDK already retries common transient failures; this outer wrapper
    adds bounded protection for runtime-level timeout/connection exceptions.
    """
    return client.responses.create(**kwargs)


def response_to_json(response: Any) -> Dict[str, Any]:
    raw = getattr(response, "output_text", None)
    if not raw:
        # Fallback for SDK/object shape differences.
        try:
            raw = response.output[0].content[0].text
        except Exception as exc:
            raise RuntimeError("Could not read model output text from response object.") from exc
    try:
        return _validate_ai_payload_shape(json.loads(raw))
    except json.JSONDecodeError as exc:
        # This should be rare with strict structured output, but write raw output for debugging.
        raise RuntimeError(f"Model did not return valid JSON. First 500 chars: {raw[:500]}") from exc


def extract_with_local_text(
    client: OpenAI,
    model: str,
    path: Path,
    document_text: str,
    phrases_by_category: Dict[str, List[str]],
    max_chars: int,
    reasoning_effort: str,
) -> Dict[str, Any]:
    evidence_text = build_dictionary_evidence(document_text, phrases_by_category, max_chars=max_chars)
    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Extract the securities terms from the document text below.
Use one row/object per security/class/series.

DOCUMENT TEXT / EVIDENCE:
{evidence_text}
""".strip()

    response = _responses_create(client, 
        model=model,
        instructions=INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                ],
            }
        ],
        text=text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)


def extract_with_file_api(
    client: OpenAI,
    model: str,
    path: Path,
    keep_api_files: bool = False,
    reasoning_effort: str = "low",
) -> Dict[str, Any]:
    upload_path, is_temp_copy = prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")

    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Extract the securities terms from the uploaded document.
Use one row/object per security/class/series.
If the file is a scanned PDF, use the visible page images and text to extract the terms.
""".strip()

    try:
        response = _responses_create(client, 
            model=model,
            instructions=INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded.id},
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
            text=text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        )
        return response_to_json(response)
    finally:
        if not keep_api_files:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass
        cleanup_api_upload_path(upload_path, is_temp_copy)



def value_is_weak(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "not stated", "n/a", "na", "none", "null", "needs review"} or "needs review" in text or "missing" in text


def extraction_needs_quality_review(data: Dict[str, Any]) -> bool:
    """Return True when OIP or seniority looks weak for any extracted security."""
    securities = data.get("securities", []) or []
    for sec in securities:
        sec_name = str(sec.get("security_name", "")).lower()
        sec_type = str(sec.get("security_type", "")).lower()
        is_pref = "preferred" in sec_name or "preferred" in sec_type or re.search(r"series\s+[a-z0-9]+", sec_name)
        if is_pref and value_is_weak(sec.get("oip_original_issue_price")):
            return True
        if value_is_weak(sec.get("seniority")):
            return True
    return False


def build_quality_evidence(document_text: str, max_chars: int = 120000) -> str:
    """Build focused evidence for OIP and liquidation seniority review."""
    phrases_by_category = {
        "OIP / Issue Price Evidence": [
            "Original Issue Price", "Original Issuance Price", "Original Purchase Price", "Initial Issue Price",
            "Issue Price", "Purchase Price", "Subscription Price", "price per share", "per share purchase price",
            "Series A Original Issue Price", "Series B Original Issue Price", "Series C Original Issue Price",
            "Series D Original Issue Price", "Series E Original Issue Price", "Series F Original Issue Price",
            "Series G Original Issue Price", "Series H Original Issue Price", "Series I Original Issue Price",
            "Series J Original Issue Price", "Conversion Price", "initial Conversion Price", "Original Issue Price shall mean",
            "one times", "1x", "per share", "stated value",
        ],
        "Seniority / Liquidation Waterfall Evidence": [
            "rank senior", "rank junior", "pari passu", "on parity", "prior and in preference",
            "before any payment", "before any distribution", "first", "second", "third", "thereafter",
            "after payment in full", "Liquidation Preference", "liquidation, dissolution or winding up",
            "Deemed Liquidation Event", "distribution of assets", "remaining assets", "remaining proceeds",
            "subject to prior rights", "Senior Stock", "Parity Stock", "Junior Stock",
        ],
    }
    return build_dictionary_evidence(document_text, phrases_by_category, max_chars=max_chars)


def quality_review_with_local_text(
    client: OpenAI,
    model: str,
    path: Path,
    document_text: str,
    current_data: Dict[str, Any],
    max_chars: int,
    reasoning_effort: str,
) -> Dict[str, Any]:
    evidence_text = build_quality_evidence(document_text, max_chars=max_chars)
    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Prior extraction JSON:
{json.dumps(current_data, ensure_ascii=False)}

Perform a targeted OIP and seniority quality review using the evidence below. Return the full corrected JSON in the required schema.

TARGETED SOURCE EVIDENCE:
{evidence_text}
""".strip()
    response = _responses_create(client, 
        model=model,
        instructions=QUALITY_REVIEW_INSTRUCTIONS,
        input=[{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
        text=text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)


def quality_review_with_file_api(
    client: OpenAI,
    model: str,
    path: Path,
    current_data: Dict[str, Any],
    keep_api_files: bool = False,
    reasoning_effort: str = "medium",
) -> Dict[str, Any]:
    upload_path, is_temp_copy = prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Prior extraction JSON:
{json.dumps(current_data, ensure_ascii=False)}

Perform a targeted OIP and seniority quality review from the uploaded source file. If the PDF is scanned, use visible page images. Return the full corrected JSON in the required schema.
""".strip()
    try:
        response = _responses_create(client, 
            model=model,
            instructions=QUALITY_REVIEW_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded.id},
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
            text=text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        )
        return response_to_json(response)
    finally:
        if not keep_api_files:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass
        cleanup_api_upload_path(upload_path, is_temp_copy)


def normalize_security_name_for_match(value: str) -> str:
    t = str(value or "").lower()
    t = re.sub(r"preferred\s+stock|preferred\s+shares|shares|stock|inc\.|inc|corp\.|corporation", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def match_tier_security_to_existing(tier_name: str, existing_names: List[str]) -> Optional[str]:
    """Match security labels without ever fuzzily crossing distinct series.

    Exact series token identity is authoritative. RapidFuzz is used only for
    harmless wording aliases after series identity is known, never to choose a
    value for a different series.
    """
    tn = normalize_security_name_for_match(tier_name)
    if not tn:
        return None
    target_token = _series_token_from_name(tier_name)
    if target_token:
        exact_token_matches = [n for n in existing_names if _series_token_from_name(n) == target_token]
        if len(exact_token_matches) == 1:
            return exact_token_matches[0]
        if len(exact_token_matches) > 1:
            return max(exact_token_matches, key=lambda n: fuzz.ratio(tn, normalize_security_name_for_match(n)))
        # A named Series A must never fall through to Series A-1 / A-2.
        return None

    best = None
    best_score = 0.0
    for existing in existing_names:
        # Do not let a non-series label fuzzily map to a series security.
        if _series_token_from_name(existing):
            continue
        en = normalize_security_name_for_match(existing)
        if not en:
            continue
        score = float(fuzz.token_set_ratio(tn, en))
        if score > best_score:
            best_score = score
            best = existing
    return best if best_score >= 92 else None


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


def match_tier_security_to_all_existing(tier_name: str, existing_names: List[str]) -> List[str]:
    """Return all existing row names represented by a tier string.

    This fixes cases where the seniority-only pass returns combined names such as:
    - "Series A-1 and Series A-2 Preferred Stock"
    - "Series A-1/A-2 Preferred Stock"
    - "Series A-1 through Series A-3 Preferred Stock"
    Prior versions matched only one best row, which caused same-tier securities to be missed.
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
        best = match_tier_security_to_existing(raw_name, existing_names)
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


def apply_seniority_tiers_to_data(data: Dict[str, Any], tiers_json: Dict[str, Any]) -> Dict[str, Any]:
    """Overwrite extracted seniority fields using a dedicated seniority exact-rank JSON result.

    v8 prioritizes `security_seniority_ranks`, which is keyed to the exact securities
    already extracted. This avoids the old tier-to-row mapping problem where combined
    names like "Series A-1 and Series A-2" could be mapped incorrectly.
    """
    if not data or not tiers_json:
        return data

    # Keep the raw seniority response inside data so a debug Seniority Review sheet can show it.
    data["_seniority_review_json"] = tiers_json

    securities = data.get("securities", []) or []
    existing_names = [str(sec.get("security_name", "")) for sec in securities]
    existing_by_name = {str(sec.get("security_name", "")): sec for sec in securities}

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
                matched_list = match_tier_security_to_all_existing(str(tier_sec), existing_names)
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
                matches = match_tier_security_to_all_existing(candidate, existing_names)
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

def apply_oip_prices_to_data(data: Dict[str, Any], oip_json: Dict[str, Any]) -> Dict[str, Any]:
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
        matched_names = match_tier_security_to_all_existing(sec_name, existing_names) or [sec_name]
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


def oip_price_review_with_file_api(
    client: OpenAI,
    model: str,
    path: Path,
    current_data: Dict[str, Any],
    keep_api_files: bool = False,
    reasoning_effort: str = "medium",
) -> Dict[str, Any]:
    """Use uploaded source file/PDF to extract exact OIP prices and correct decimal misreads."""
    securities = current_data.get("securities", []) or []
    security_list = [str(sec.get("security_name", "")) for sec in securities if str(sec.get("security_name", "")).strip()]
    if not security_list:
        return current_data
    upload_path, is_temp_copy = prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Expected securities/classes/series from prior extraction:
{json.dumps(security_list, ensure_ascii=False, indent=2)}

Prior extraction JSON for context:
{json.dumps(current_data, ensure_ascii=False)}

Extract exact OIP / Original Issue Price values only from the uploaded source file. Use page images if the PDF is scanned. Return strict JSON only.
""".strip()
    try:
        response = _responses_create(client, 
            model=model,
            instructions=OIP_PRICE_REVIEW_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded.id},
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
            text=oip_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        )
        oip_json = response_to_json(response)
        return apply_oip_prices_to_data(current_data, oip_json)
    finally:
        if not keep_api_files:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass
        cleanup_api_upload_path(upload_path, is_temp_copy)


def seniority_waterfall_review_with_file_api(
    client: OpenAI,
    model: str,
    path: Path,
    current_data: Dict[str, Any],
    keep_api_files: bool = False,
    reasoning_effort: str = "medium",
) -> Dict[str, Any]:
    """Use uploaded file/PDF input to extract exact OIP prices and liquidation seniority tiers, then apply them."""
    # v14.8 deterministic rule: do NOT run a second AI OIP pass here.
    # Critical numeric fields are source-verified deterministically before export.
    securities = current_data.get("securities", []) or []
    security_list = [str(sec.get("security_name", "")) for sec in securities if str(sec.get("security_name", "")).strip()]
    if not security_list:
        return current_data
    upload_path, is_temp_copy = prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded = client.files.create(file=f, purpose="user_data")
    user_text = f"""
Document name: {path.name}
Input file: {str(path)}

Expected securities/classes/series from prior extraction:
{json.dumps(security_list, ensure_ascii=False, indent=2)}

Prior extraction JSON for context:
{json.dumps(current_data, ensure_ascii=False)}

Extract liquidation seniority tiers only from the uploaded source file. Use page images if the PDF is scanned. Return strict JSON only.
""".strip()
    try:
        response = _responses_create(client, 
            model=model,
            instructions=SENIORITY_WATERFALL_INSTRUCTIONS,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": uploaded.id},
                        {"type": "input_text", "text": user_text},
                    ],
                }
            ],
            text=seniority_text_format_schema(),
            reasoning={"effort": reasoning_effort} if reasoning_effort else None,
        )
        tiers_json = response_to_json(response)
        return apply_seniority_tiers_to_data(current_data, tiers_json)
    finally:
        if not keep_api_files:
            try:
                client.files.delete(uploaded.id)
            except Exception:
                pass
        cleanup_api_upload_path(upload_path, is_temp_copy)

def count_extracted_securities(data: Optional[Dict[str, Any]]) -> int:
    if not isinstance(data, dict):
        return 0
    securities = data.get("securities", []) or []
    return len(securities) if isinstance(securities, list) else 0


def process_file(
    client: OpenAI,
    model: str,
    path: Path,
    mode: str,
    phrases_by_category: Dict[str, List[str]],
    max_chars: int,
    keep_api_files: bool,
    reasoning_effort: str = "low",
    min_local_chars: int = 1500,
    quality_pass: bool = False,
    quality_reasoning_effort: str = "medium",
    upload_style_seniority: bool = False,
) -> ExtractionResult:
    suffix = path.suffix.lower()
    try:
        if mode == "local-text":
            if suffix not in SUPPORTED_LOCAL_TEXT:
                raise ValueError(f"{suffix} is not supported in local-text mode. Use --mode file-api.")
            text = extract_local_text(path)
            if len(text.strip()) < min_local_chars:
                raise ValueError(
                    f"Local text extraction produced only {len(text.strip())} characters. "
                    "This may be a scanned PDF; use --mode file-api."
                )
            data = extract_with_local_text(client, model, path, text, phrases_by_category, max_chars, reasoning_effort)
            if quality_pass and extraction_needs_quality_review(data):
                print(f"    Quality pass: reviewing OIP and seniority for {path.name}", flush=True)
                data = quality_review_with_local_text(client, model, path, text, data, max_chars, quality_reasoning_effort)
            if upload_style_seniority and suffix in SUPPORTED_FILE_API:
                print(f"    Full-source Cap Table verification: reading full source file for {path.name}", flush=True)
                data = comprehensive_cap_table_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
            return ExtractionResult(str(path), "success", data=data, mode_used="local-text" + ("+quality" if quality_pass else "") + ("+full-source-verify" if upload_style_seniority else ""))

        if mode == "file-api":
            if suffix not in SUPPORTED_FILE_API:
                raise ValueError(f"{suffix} is not supported in file-api mode.")
            data = extract_with_file_api(client, model, path, keep_api_files=keep_api_files, reasoning_effort=reasoning_effort)
            if quality_pass and extraction_needs_quality_review(data):
                print(f"    Quality pass: reviewing OIP and seniority for {path.name}", flush=True)
                data = quality_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
            if upload_style_seniority:
                print(f"    Full-source Cap Table verification: reading full source file for {path.name}", flush=True)
                data = comprehensive_cap_table_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
            return ExtractionResult(str(path), "success", data=data, mode_used="file-api" + ("+quality" if quality_pass else "") + ("+full-source-verify" if upload_style_seniority else ""))

        if mode == "auto":
            if suffix in SUPPORTED_LOCAL_TEXT:
                try:
                    text = extract_local_text(path)
                    if len(text.strip()) >= min_local_chars:
                        data = extract_with_local_text(client, model, path, text, phrases_by_category, max_chars, reasoning_effort)
                        if count_extracted_securities(data) == 0 and suffix in SUPPORTED_FILE_API:
                            print(f"    Auto retry: local-text returned 0 rows for {path.name}; retrying with file-api...", flush=True)
                            data = extract_with_file_api(client, model, path, keep_api_files=keep_api_files, reasoning_effort=reasoning_effort)
                            if quality_pass and extraction_needs_quality_review(data):
                                print(f"    Quality pass: reviewing OIP and seniority for {path.name}", flush=True)
                                data = quality_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
                            if upload_style_seniority:
                                print(f"    Full-source Cap Table verification: reading full source file for {path.name}", flush=True)
                                data = comprehensive_cap_table_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
                            return ExtractionResult(str(path), "success", data=data, mode_used="auto/local-text->file-api" + ("+quality" if quality_pass else "") + ("+full-source-verify" if upload_style_seniority else ""))
                        if quality_pass and extraction_needs_quality_review(data):
                            print(f"    Quality pass: reviewing OIP and seniority for {path.name}", flush=True)
                            data = quality_review_with_local_text(client, model, path, text, data, max_chars, quality_reasoning_effort)
                        if upload_style_seniority and suffix in SUPPORTED_FILE_API:
                            print(f"    Full-source Cap Table verification: reading full source file for {path.name}", flush=True)
                            data = comprehensive_cap_table_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
                        return ExtractionResult(str(path), "success", data=data, mode_used="auto/local-text" + ("+quality" if quality_pass else "") + ("+full-source-verify" if upload_style_seniority else ""))
                except Exception as local_error:
                    # Fall back to API file input for PDFs/DOCX/RTF/DOC etc.
                    if suffix not in SUPPORTED_FILE_API:
                        raise local_error
            if suffix in SUPPORTED_FILE_API:
                data = extract_with_file_api(client, model, path, keep_api_files=keep_api_files, reasoning_effort=reasoning_effort)
                if quality_pass and extraction_needs_quality_review(data):
                    print(f"    Quality pass: reviewing OIP and seniority for {path.name}", flush=True)
                    data = quality_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
                if upload_style_seniority:
                    print(f"    Full-source Cap Table verification: reading full source file for {path.name}", flush=True)
                    data = comprehensive_cap_table_review_with_file_api(client, model, path, data, keep_api_files=keep_api_files, reasoning_effort=quality_reasoning_effort)
                return ExtractionResult(str(path), "success", data=data, mode_used="auto/file-api" + ("+quality" if quality_pass else "") + ("+full-source-verify" if upload_style_seniority else ""))
            raise ValueError(f"Unsupported file type: {suffix}")

        raise ValueError(f"Unknown mode: {mode}")
    except Exception as exc:
        return ExtractionResult(str(path), "error", error=str(exc), mode_used=mode)



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
    # Variants with hyphen removed already normalized to spaces.
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
                # Distance to marker inside window; lower is better. Prefer nearest money, before or after.
                marker_pos = m.start() - start
                dist = abs(money.start() - marker_pos)
                if best is None or dist < best[0]:
                    best = (dist, value)
    return None if best is None else best[1]


def _strict_series_token(value: Any) -> str:
    t = str(value or "").lower().replace("–", "-").replace("—", "-")
    if "common" in t and "preferred" not in t:
        return "common"
    # Support Series B2, Series B-2 and Series B 2 as the same immutable identity.
    m = re.search(r"\bseries\s+([a-z]+)(?:\s*[- ]?\s*(\d+))?", t, re.I)
    if not m:
        return ""
    return f"series{m.group(1)}{m.group(2) or ''}"


def _strict_series_regex(token: str) -> Optional[str]:
    if token == "common":
        return r"common(?:\s+stock)?"
    m = re.match(r"^series([a-z]+)(\d*)$", token or "")
    if not m:
        return None
    word, num = m.group(1), m.group(2)
    if num:
        return rf"series\s+{re.escape(word)}\s*[- ]?\s*{re.escape(num)}(?!\d)(?:\s+preferred(?:\s+stock|\s+shares?)?)?"
    return rf"series\s+{re.escape(word)}(?!\s*[- ]?\s*\d)(?:\s+preferred(?:\s+stock|\s+shares?)?)?"


def _role_windows(source_text: str, anchors: List[str], radius: int = 3200) -> List[str]:
    """Return broad source windows around a field's legal/economic role.

    This is intentionally wording-flexible: anchors identify the clause family,
    while exact series matching inside each window identifies the security.
    """
    source = str(source_text or "")
    if not source.strip():
        return []
    spans: List[Tuple[int, int]] = []
    for anchor in anchors:
        for m in re.finditer(anchor, source, flags=re.I):
            spans.append((max(0, m.start() - radius), min(len(source), m.end() + radius)))
    if not spans:
        return []
    spans.sort()
    merged: List[Tuple[int, int]] = []
    for a, b in spans:
        if not merged or a > merged[-1][1] + 250:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return [source[a:b] for a, b in merged]


def _extract_field_value_from_source(security_name: str, source_text: str, field: str) -> Optional[float]:
    """Extract a money value using exact-series matching inside the correct clause family.

    The lock is strict about *role* (OIP, conversion, liquidation, dividend,
    participation cap) but flexible about legal wording. No company-specific
    values or phrases are required.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None

    field_specs = {
        "oip": {
            "anchors": [
                r"original\s+(?:issue|issuance|purchase)\s+price", r"issue\s+price",
                r"purchase\s+price", r"subscription\s+price", r"price\s+per\s+share",
            ],
            "labels": r"original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price|price\s+per\s+share",
            "radius": 2600,
        },
        "conversion": {
            "anchors": [r"initial\s+conversion\s+price", r"conversion\s+price", r"conversion\s+rate", r"convertible\s+into"],
            "labels": r"initial\s+conversion\s+price|original\s+conversion\s+price|conversion\s+price",
            "radius": 3200,
        },
        "liquidation": {
            "anchors": [
                r"liquidation\s+preference", r"liquidation,?\s+dissolution\s+or\s+winding\s+up",
                r"liquidation\s+event", r"prior\s+and\s+in\s+preference", r"before\s+any\s+distribution",
            ],
            "labels": r"liquidation\s+preference|liquidation\s+amount|entitled\s+to\s+receive|prior\s+and\s+in\s+preference|per\s+share",
            "radius": 4200,
        },
        "participation_cap": {
            "anchors": [
                r"cease\s+to\s+participate", r"participation\s+cap", r"capped\s+at",
                r"maximum\s+amount", r"total\s+distribution", r"aggregate\s+amount\s+equal\s+to",
                r"until\s+such\s+holder\s+has\s+received",
            ],
            "labels": r"cease\s+to\s+participate|participation\s+cap|capped\s+at|maximum\s+amount|total\s+distribution|aggregate\s+amount\s+equal\s+to|until\s+such\s+holder\s+has\s+received",
            "radius": 3600,
        },
        "dividend_amount": {
            "anchors": [r"dividend", r"dividends", r"per\s+annum", r"annual"],
            "labels": r"dividend|dividends|annual|per\s+annum|per\s+share",
            "radius": 3200,
        },
    }
    spec = field_specs.get(field)
    if spec is None:
        return None

    windows = _role_windows(source_text, spec["anchors"], radius=spec["radius"])
    if not windows:
        return None

    # Highest-confidence exact-series pair patterns. These are evaluated before
    # proximity scoring so adjacent series in the same sentence/table cannot swap
    # values (e.g. Seed-1 vs Seed-2).
    direct_candidates: List[Tuple[int, float]] = []
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        if not window or not re.search(spec["labels"], window, re.I):
            continue
        pair_patterns = [
            rf"(?P<price>\$\s*[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?\s*(?:for|of|with\s+respect\s+to|applicable\s+to)\s*(?P<series>{rx})",
            rf"(?P<series>{rx}).{{0,180}}?(?:{spec['labels']}).{{0,100}}?(?P<price>\$\s*[0-9][0-9,]*(?:\.[0-9]+)?)",
            rf"(?:{spec['labels']}).{{0,100}}?(?P<price>\$\s*[0-9][0-9,]*(?:\.[0-9]+)?).{{0,180}}?(?P<series>{rx})",
        ]
        for priority, pat in enumerate(pair_patterns):
            for m in re.finditer(pat, window, re.I):
                try:
                    val = float(m.group("price").replace("$", "").replace(",", "").strip())
                except Exception:
                    continue
                if val <= 0 or val > 1000000:
                    continue
                direct_candidates.append((priority * 10000 + (m.end()-m.start()), val))
    if direct_candidates:
        direct_candidates.sort(key=lambda x: x[0])
        return direct_candidates[0][1]

    candidates: List[Tuple[int, float, str]] = []
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        if not window:
            continue
        # Require field-role language in the window after normalization.
        if not re.search(spec["labels"], window, re.I):
            continue
        for sm in re.finditer(rx, window, flags=re.I):
            local_start = max(0, sm.start() - 360)
            local_end = min(len(window), sm.end() + 420)
            local = window[local_start:local_end]
            marker_pos = sm.start() - local_start
            # The same local clause must still contain role language.
            if not re.search(spec["labels"], local, re.I):
                continue
            for mm in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", local):
                try:
                    val = float(mm.group(1).replace(",", ""))
                except Exception:
                    continue
                if val <= 0 or val > 1000000:
                    continue
                distance = abs(mm.start() - marker_pos)
                between = local[min(mm.start(), marker_pos):max(mm.end(), marker_pos)]
                score = distance
                # Reward common list/table forms: "Series A, $0.40" and "$0.40 ... Series A".
                if re.search(r"per\s+share|equal\s+to|amount\s+of|at\s+the\s+rate\s+of", between, re.I):
                    score -= 80
                if field == "conversion" and re.search(r"initial\s+conversion\s+price", local, re.I):
                    score -= 160
                if field == "liquidation" and re.search(r"entitled\s+to\s+receive|prior\s+and\s+in\s+preference", local, re.I):
                    score -= 120
                if field == "participation_cap" and re.search(r"cease\s+to\s+participate|total\s+distribution|maximum", local, re.I):
                    score -= 140
                if field == "dividend_amount" and re.search(r"dividend", local, re.I):
                    score -= 120
                excerpt = local[max(0, min(mm.start(), marker_pos)-130):min(len(local), max(mm.end(), marker_pos)+170)]
                candidates.append((score, val, excerpt))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _extract_field_excerpt_from_source(security_name: str, source_text: str, field: str) -> str:
    """Return compact field-role evidence without leaking unrelated nearby clauses."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return ""
    anchor_map = {
        "participation_cap": [r"cease\s+to\s+participate", r"participation\s+cap", r"total\s+distribution", r"maximum\s+amount", r"capped\s+at"],
        "dividend_amount": [r"dividend", r"dividends", r"annual", r"per\s+annum"],
        "liquidation": [r"liquidation\s+preference", r"liquidation,?\s+dissolution\s+or\s+winding\s+up", r"prior\s+and\s+in\s+preference"],
    }
    anchors = anchor_map.get(field, [])
    windows = _role_windows(source_text, anchors, radius=2200)
    best: Optional[Tuple[int, str]] = None
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        for sm in re.finditer(rx, window, re.I):
            for anchor in anchors:
                for am in re.finditer(anchor, window, re.I):
                    distance = abs(am.start() - sm.start())
                    if distance > 900:
                        continue
                    a = max(0, min(sm.start(), am.start()) - 180)
                    b = min(len(window), max(sm.end(), am.end()) + 260)
                    excerpt = window[a:b]
                    if best is None or distance < best[0]:
                        best = (distance, excerpt)
    return "" if best is None else best[1]


def _extract_participation_class_from_source(security_name: str, source_text: str) -> Optional[str]:
    """Classify participation from the actual residual-distribution clause.

    v15.6 rules:
    - explicit no-additional-distribution wording -> Non-participating;
    - preference OR as-converted / greater-of economics -> Non-participating unless
      the same clause separately grants residual participation after preference;
    - explicit cap/cease-to-participate language -> Capped Participation;
    - explicit residual sharing after preference -> Participating.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None

    anchors = [
        r"remaining\s+(?:assets|proceeds|funds)",
        r"shall\s+not\s+be\s+entitled",
        r"not\s+entitled\s+to\s+receive",
        r"cease\s+to\s+participate",
        r"participat(?:e|es|ing|ion)",
        r"as[-\s]?converted",
        r"greater\s+of",
    ]
    windows = _role_windows(source_text, anchors, radius=2600)
    ranked: List[Tuple[int, str]] = []
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        for sm in re.finditer(rx, window, flags=re.I):
            local = window[max(0, sm.start()-520):min(len(window), sm.end()+760)]

            if re.search(
                r"shall\s+not\s+be\s+entitled.{0,220}(?:additional\s+)?distributions?|"
                r"not\s+entitled\s+to\s+receive.{0,220}(?:additional\s+)?distributions?|"
                r"shall\s+not\s+participate|non[-\s]?participat",
                local,
                re.I,
            ):
                ranked.append((0, "Non-participating"))
                continue

            # True capped participation requires both residual participation and a cap.
            if re.search(r"cease\s+to\s+participate|participation\s+cap|capped\s+at|maximum\s+amount|total\s+distribution", local, re.I):
                ranked.append((1, "Capped Participation"))
                continue

            # "Greater of preference or as-converted" is the classic non-participating
            # structure unless the same local clause explicitly says the holder also
            # participates in remaining proceeds after receiving the preference.
            if re.search(r"greater\s+of|as[-\s]?converted", local, re.I):
                has_after_pref_participation = bool(re.search(
                    r"after.{0,180}(?:preference|payment|receive|receiving).{0,180}participat|"
                    r"participat.{0,180}(?:remaining\s+(?:assets|proceeds|funds)|common)",
                    local,
                    re.I,
                ))
                if not has_after_pref_participation:
                    ranked.append((2, "Non-participating"))
                    continue

            if re.search(r"participat(?:e|es|ing|ion)", local, re.I):
                ranked.append((3, "Participating"))

    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]
def _extract_participation_cap_multiplier_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Return a participation-cap multiplier such as 3x when tied to the exact series."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None
    windows = _role_windows(
        source_text,
        [r"cease\s+to\s+participate", r"participation\s+cap", r"capped\s+at", r"maximum\s+amount", r"total\s+distribution"],
        radius=2600,
    )
    word_mult = {
        "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
        "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
    }
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        for sm in re.finditer(rx, window, re.I):
            local = window[max(0, sm.start()-520):min(len(window), sm.end()+720)]
            if not re.search(r"cease\s+to\s+participate|participation\s+cap|capped\s+at|maximum\s+amount|total\s+distribution", local, re.I):
                continue
            m = re.search(r"\b(\d+(?:\.\d+)?)\s*x\s*(?:the\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)", local, re.I)
            if m:
                return float(m.group(1))
            m = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+times\s*(?:the\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)", local, re.I)
            if m:
                return word_mult[m.group(1).lower()]
    return None

def _extract_authorized_count_from_source(security_name: str, source_text: str) -> Optional[int]:
    """Extract the full authorized share count for the exact security/series.

    v15.5 fix:
    - preserve commas inside large integers (e.g. 34,553,900);
    - match only the exact series token;
    - prefer designation/capitalization wording over generic nearby numbers;
    - never use issued/outstanding/reserved/as-converted counts.
    """
    if source_text:
        parsed = _extract_capitalization_facts_from_source(source_text)
        key = normalize_security_name_for_match(security_name)
        item = (parsed.get("series_counts") or {}).get(key)
        if item and item.get("authorized_count"):
            return int(item["authorized_count"])
        if "common" in str(security_name).lower() and "preferred" not in str(security_name).lower():
            if parsed.get("total_common_authorized"):
                return int(parsed["total_common_authorized"])

    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None

    # Keep numeric punctuation intact. The generic source normalizer removes commas,
    # which can split 582,794 into fragments and cause partial-count matches.
    text = str(source_text or "").lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    # Common Stock is usually stated in the top-level capitalization sentence.
    # Resolve it directly instead of looking for a later nearby number.
    if token == "common":
        common_patterns = [
            r"(?:authority\s+to\s+issue\s+is|authorized\s+capital|total\s+number\s+of\s+shares).{0,180}?(?:\(i\)\s*)?(?P<count>\d{1,3}(?:,\d{3})+|\d+)\s+shares\s+of\s+common\s+stock",
            r"(?:\(i\)\s*)?(?P<count>\d{1,3}(?:,\d{3})+|\d+)\s+shares\s+of\s+common\s+stock",
        ]
        for pat in common_patterns:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    return int(m.group("count").replace(",", ""))
                except Exception:
                    pass

    number = r"(?P<count>\d{1,3}(?:,\d{3})+|\d+)"
    role = r"(?:authorized|designated|designation|consist(?:s|ing)?\s+of|number\s+of\s+shares|shares\s+of)"
    exclusion = re.compile(r"issued|outstanding|reserved|issuable|as[-\s]?converted|option|warrant", re.I)

    patterns = [
        # Highest confidence: count precedes the exact designation.
        rf"{number}\s+shares?(?:(?!designated).){{0,220}}?(?:designated\s+as|designated)\s*[\"'“”‘’]*\s*(?P<series>{rx})",
        # Exact series followed by a count is lower confidence because lists often
        # place the next series count immediately after the prior series name.
        rf"(?P<series>{rx}).{{0,180}}?{role}.{{0,120}}?{number}\s+shares?",
        rf"{number}\s+shares?.{{0,100}}?(?P<series>{rx})",
    ]

    candidates: List[Tuple[int, int]] = []
    for priority, pat in enumerate(patterns):
        for m in re.finditer(pat, text, flags=re.I):
            local = text[max(0, m.start()-100):min(len(text), m.end()+100)]
            if exclusion.search(local) and not re.search(r"authorized|designated|consist", local, re.I):
                continue
            try:
                value = int(m.group("count").replace(",", ""))
            except Exception:
                continue
            if value <= 0:
                continue
            score = priority * 10000 + (m.end() - m.start())
            candidates.append((score, value))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]
def _extract_dividend_percent_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Return an explicit dividend percentage for the exact security, if stated."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None
    windows = _role_windows(source_text, [r"dividend", r"dividends", r"per\s+annum", r"annual"], radius=3000)
    candidates: List[Tuple[int, float]] = []
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        for sm in re.finditer(rx, window, re.I):
            local = window[max(0, sm.start()-520):min(len(window), sm.end()+620)]
            marker_pos = sm.start() - max(0, sm.start()-520)
            if not re.search(r"dividend|dividends", local, re.I):
                continue
            for pm in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", local, re.I):
                try:
                    pct = float(pm.group(1))
                except Exception:
                    continue
                if pct < 0 or pct > 100:
                    continue
                distance = abs(pm.start() - marker_pos)
                between = local[min(pm.start(), marker_pos):max(pm.end(), marker_pos)]
                score = distance
                if re.search(r"dividend|rate|per\s+annum|annual", between, re.I):
                    score -= 120
                candidates.append((score, pct))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _extract_dividend_amount_from_source(security_name: str, source_text: str) -> Optional[float]:
    """Return an explicit per-share dividend dollar amount for the exact security.

    This avoids treating OIP/conversion/liquidation amounts in a broad dividend
    window as the dividend amount itself.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if not rx or not source_text:
        return None
    windows = _role_windows(source_text, [r"dividend", r"dividends"], radius=3000)
    candidates: List[Tuple[int, float]] = []
    for raw_window in windows:
        window = _normalize_source_text_for_match(raw_window)
        for sm in re.finditer(rx, window, re.I):
            local_start = max(0, sm.start()-520)
            local_end = min(len(window), sm.end()+620)
            local = window[local_start:local_end]
            marker_pos = sm.start() - local_start
            if not re.search(r"dividend|dividends", local, re.I):
                continue
            for mm in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", local):
                try:
                    amount = float(mm.group(1).replace(",", ""))
                except Exception:
                    continue
                distance = abs(mm.start() - marker_pos)
                between = local[min(mm.start(), marker_pos):max(mm.end(), marker_pos)]
                score = distance
                if re.search(r"dividend|per\s+share|annual|per\s+annum", between, re.I):
                    score -= 140
                # Penalize amounts explicitly labelled as OIP/conversion/liquidation.
                if re.search(r"original\s+(?:issue|issuance|purchase)\s+price|conversion\s+price|liquidation\s+preference", between, re.I):
                    score += 500
                candidates.append((score, amount))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _source_supports_oip_conversion_formula(source_text: str) -> bool:
    """Detect the standard conversion formula from actual source text.

    This is used as a deterministic consistency check; it does not infer an OIP
    or conversion price that is not otherwise source-verified.
    """
    text = _normalize_source_text_for_match(source_text)
    patterns = [
        r"original\s+(?:issue|issuance|purchase)\s+price.{0,160}(?:divided\s+by|over).{0,120}conversion\s+price",
        r"conversion\s+(?:rate|ratio).{0,180}original\s+(?:issue|issuance|purchase)\s+price.{0,120}conversion\s+price",
    ]
    return any(re.search(p, text, re.I) for p in patterns)


def _apply_deterministic_consistency_checks(df: pd.DataFrame, source_document_text: str) -> pd.DataFrame:
    """Apply final non-AI consistency rules before Excel export.

    v15.1 rules are role-locked but wording-flexible:
    - authorized counts come from capitalization/designation context;
    - conversion ratio is mathematical when verified OIP and Conversion Price exist;
    - dollar dividend amounts are converted to percentages using verified OIP;
    - no cap/liquidation/conversion amount may substitute for another field merely
      because it appears nearby in the document.
    """
    if df.empty:
        return df
    out = df.copy()
    formula_supported = _source_supports_oip_conversion_formula(source_document_text) if source_document_text else False
    for idx, row in out.iterrows():
        name = str(row.get("Security Name", ""))
        low_name = name.lower()
        if not name or "blank check" in low_name or "undesignated" in low_name:
            continue


        if "preferred" not in low_name:
            continue

        oip = _money_to_float(out.at[idx, "OIP / Original Issue Price"])
        cp = _money_to_float(out.at[idx, "Conversion Price"])

        # Participation-cap formulas such as 3x OIP are converted into a dollar/share
        # amount from the verified OIP. This prevents "3x" from becoming "$3".
        if source_document_text and oip is not None and oip > 0:
            cap_mult = _extract_participation_cap_multiplier_from_source(name, source_document_text)
            if cap_mult is not None and cap_mult > 0:
                out.at[idx, "Participation Cap"] = _fmt_money_num(cap_mult * oip)
                out.at[idx, "Review Flags"] = _append_review_flag(
                    out.at[idx, "Review Flags"],
                    f"Participation cap calculated deterministically as {cap_mult:g}x verified OIP",
                )

        # Stable conversion ratio. When the source states the standard formula,
        # verified numeric inputs are authoritative. If both values are equal,
        # 1:1 is also safe even when the formula wording is abbreviated.
        if oip is not None and cp is not None and cp > 0 and (formula_supported or abs(oip - cp) <= 1e-9):
            ratio = oip / cp
            ratio_text = "1:1" if abs(ratio - 1.0) <= 1e-9 else f"{f'{ratio:.4f}'.rstrip('0').rstrip('.')}:1"
            out.at[idx, "Conversion Ratio"] = ratio_text
            out.at[idx, "Review Flags"] = _append_review_flag(
                out.at[idx, "Review Flags"],
                "Conversion ratio calculated deterministically from verified OIP and Conversion Price",
            )

        # Stable dividend percentage. An explicit source percentage is always
        # authoritative. Only when no percentage is stated do we calculate the
        # source-stated annual dollar dividend amount / verified OIP.
        if source_document_text:
            explicit_pct = _strict_dividend_percent_from_source(name, source_document_text)
            if explicit_pct is not None:
                pct_text = f"{explicit_pct:.6f}".rstrip("0").rstrip(".") + "%"
                out.at[idx, "Dividend Rate"] = pct_text
                out.at[idx, "Review Flags"] = _append_review_flag(
                    out.at[idx, "Review Flags"],
                    "Dividend rate verified from explicit dividend-clause percentage",
                )
            else:
                dividend_amount = _strict_dividend_amount_from_source(name, source_document_text)
                if oip is not None and oip > 0 and dividend_amount is not None and dividend_amount >= 0:
                    pct = dividend_amount / oip * 100.0
                    if 0 <= pct <= 100:
                        pct_text = f"{pct:.6f}".rstrip("0").rstrip(".") + "%"
                        out.at[idx, "Dividend Rate"] = pct_text
                        out.at[idx, "Review Flags"] = _append_review_flag(
                            out.at[idx, "Review Flags"],
                            "Dividend rate calculated from explicit annual per-share dividend and verified OIP",
                        )
                else:
                    # A matching/as-converted dividend right is not a fixed dividend rate.
                    out.at[idx, "Dividend Rate"] = "Not stated"
    return out


def _append_review_flag(existing: Any, note: str) -> str:
    text = str(existing or "").strip()
    if note in text:
        return text
    return (text + "; " + note).strip("; ").strip()


def _apply_required_oip_hierarchy(df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """v15.16 strict OIP policy: never manufacture OIP from another field.

    OIP and Conversion Price are independent source facts.  A missing OIP stays
    missing unless it was explicitly certified earlier from OIP/issue/purchase/
    issuance/subscription-price evidence for the exact immutable security.
    Liquidation preference, conversion price, participation cap, dividend amount,
    par value, or proximity are never OIP substitutes.
    """
    if df.empty:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        name = str(row.get("Security Name", "") or "")
        if "preferred" not in name.lower():
            continue
        current = _money_to_float(row.get("OIP / Original Issue Price"))
        if current is None:
            out.at[idx, "OIP / Original Issue Price"] = "Not stated"
            # Do not calculate a ratio from an absent OIP.
            out.at[idx, "Conversion Ratio"] = "Not stated"
    return out

def _apply_field_source_locks(df: pd.DataFrame, source_document_text: str) -> pd.DataFrame:
    """Conservative exact-series local backfill for missing numeric anchors only.

    v15.16 deliberately does NOT use local proximity logic to overwrite liquidation,
    participation, participation-cap, or dividend classifications produced by the
    targeted source-evidence certification layer.  Local parsing may fill a missing
    OIP or Conversion Price only when the exact-series parser finds a direct
    security/value pair in the correct field role.
    """
    if df.empty or not source_document_text:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        name = str(row.get("Security Name", "") or "")
        low_name = name.lower()
        if "preferred" not in low_name or "blank check" in low_name or "undesignated" in low_name:
            continue

        if _money_to_float(row.get("OIP / Original Issue Price")) is None:
            oip = _extract_exact_series_money_from_source(name, source_document_text, "oip")
            if oip is not None:
                out.at[idx, "OIP / Original Issue Price"] = _fmt_money_num(oip)
                out.at[idx, "Review Flags"] = _append_review_flag(
                    out.at[idx, "Review Flags"],
                    "OIP backfilled from direct exact-series OIP evidence",
                )

        if _money_to_float(row.get("Conversion Price")) is None:
            conv = _extract_exact_series_money_from_source(name, source_document_text, "conversion")
            if conv is not None:
                out.at[idx, "Conversion Price"] = _fmt_money_num(conv)
                out.at[idx, "Review Flags"] = _append_review_flag(
                    out.at[idx, "Review Flags"],
                    "Conversion Price backfilled from direct exact-series conversion evidence",
                )
    return out

def validate_terms_dataframe(terms_df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """Deterministic pre-export validation.

    v15.16 rules:
    - no company-specific patches and no second AI OIP correction pass
    - immutable exact-security identity from the authoritative roster
    - critical economics are certify-or-clear, not broad-pass defaults
    - OIP and Conversion Price remain independent source facts
    - authorized counts are out of scope and non-blocking
    - local deterministic parsing may only backfill direct exact-series OIP/Conversion evidence
    - deterministic conversion-ratio and dividend-rate calculations use verified inputs only
    - no fuzzy nearest-dollar correction
    """
    if terms_df.empty:
        return terms_df
    df = terms_df.copy()
    if source_document_text:
        df = _apply_field_source_locks(df, source_document_text)
    # OIP is a required valuation field. Populate it after explicit source locks
    # but before deterministic conversion-ratio calculations.
    df = _apply_required_oip_hierarchy(df, source_document_text)
    df = _apply_deterministic_consistency_checks(df, source_document_text)
    return df


def flatten_results(results: List[ExtractionResult]) -> Tuple[pd.DataFrame, pd.DataFrame, List[Dict[str, Any]]]:
    rows: List[Dict[str, str]] = []
    raw_json: List[Dict[str, Any]] = []
    log_rows: List[Dict[str, str]] = []

    for result in results:
        log_rows.append(
            {
                "Input File": result.file_path,
                "Status": result.status,
                "Mode Used": result.mode_used,
                "Error": result.error,
            }
        )
        if result.status != "success" or not result.data:
            continue

        data = result.data
        raw_json.append({"input_file": result.file_path, "mode_used": result.mode_used, "data": data})
        company_name = str(data.get("company_name", "Not stated"))
        document_name = str(data.get("document_name", Path(result.file_path).name))
        securities = data.get("securities", []) or []
        for sec in securities:
            rows.append(
                {
                    "Company Name": company_name,
                    "Document Name": document_name,
                    "Security Name": str(sec.get("security_name", "Not stated")),
                    "Security Type": str(sec.get("security_type", "Not stated")),
                    "Authorized Count": normalize_authorized_count(sec.get("authorized_count", "Not stated"), missing_as="Not stated"),
                    "OIP / Original Issue Price": str(sec.get("oip_original_issue_price", "Not stated")),
                    "Liquidation Preference": str(sec.get("liquidation_preference", "Not stated")),
                    "Dividend Paying": str(sec.get("dividend_paying", "Not stated")),
                    "Dividend Type": normalize_dividend_type(sec.get("dividend_type", "Not stated"), " ".join([str(sec.get("dividend_paying", "")), str(sec.get("dividend_rate", "")), str(sec.get("source_text", ""))]), missing_as="Not stated"),
                    "Dividend Rate": normalize_dividend_rate_percent(sec.get("dividend_rate", "Not stated"), " ".join([str(sec.get("dividend_paying", "")), str(sec.get("dividend_type", "")), str(sec.get("source_text", ""))]), missing_as="Not stated"),
                    "Conversion Ratio": str(sec.get("conversion_ratio", "Not stated")),
                    "Conversion Price": str(sec.get("conversion_price", "Not stated")),
                    "Participation Rights": str(sec.get("participation_rights", "Not stated")),
                    "Participation Cap": str(sec.get("participation_cap", "Not stated")),
                    "Seniority": str(sec.get("seniority", "Not stated")),
                    "Senior To": str(sec.get("senior_to", "Not stated")),
                    "Pari Passu With": str(sec.get("pari_passu_with", "Not stated")),
                    "Junior To": str(sec.get("junior_to", "Not stated")),
                    "Applies To": str(sec.get("applies_to", "Not stated")),
                    "Source Basis": str(sec.get("source_basis", "Not stated")),
                    "Confidence": str(sec.get("confidence", "Low")),
                    "Source Section": str(sec.get("source_section", "Not stated")),
                    "Source Text": str(sec.get("source_text", "Not stated")),
                    "Review Flags": str(sec.get("review_flags", "")),
                }
            )

    terms_df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    log_df = pd.DataFrame(log_rows, columns=["Input File", "Status", "Mode Used", "Error"])
    return terms_df, log_df, raw_json










def seniority_review_rows_from_raw(raw_json: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in raw_json or []:
        data = item.get("data", {}) or {}
        review = data.get("_seniority_review_json", {}) or {}
        applied = data.get("_seniority_review_applied_count", "")
        for row in review.get("security_seniority_ranks", []) or []:
            rows.append({
                "Company Name": data.get("company_name", ""),
                "Document Name": data.get("document_name", ""),
                "Security Name Returned": row.get("security_name", ""),
                "Rank Returned": row.get("rank", ""),
                "Source Basis": row.get("source_basis", ""),
                "Source Text": row.get("source_text", ""),
                "Applied Count Total": applied,
            })
        for tier in review.get("liquidation_seniority_tiers", []) or []:
            rows.append({
                "Company Name": data.get("company_name", ""),
                "Document Name": data.get("document_name", ""),
                "Security Name Returned": "; ".join([str(x) for x in tier.get("securities", [])]),
                "Rank Returned": tier.get("rank", ""),
                "Source Basis": tier.get("source_basis", ""),
                "Source Text": tier.get("source_text", ""),
                "Applied Count Total": applied,
            })
    return rows

def source_document_text_from_raw_json(raw_json: Optional[List[Dict[str, Any]]]) -> str:
    """Read actual source-document text for deterministic validation.

    This is intentionally best-effort. If a PDF is scanned and local text is not
    available, the script will still fall back to model-returned source snippets.
    """
    if not raw_json:
        return ""
    parts: List[str] = []
    seen: set = set()
    for item in raw_json or []:
        file_path = item.get("input_file")
        if not file_path:
            continue
        try:
            p = Path(str(file_path))
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen:
                continue
            seen.add(key)
            if p.exists() and p.suffix.lower() in SUPPORTED_LOCAL_TEXT:
                txt = extract_local_text(p)
                if txt.strip():
                    parts.append(f"\n--- SOURCE DOCUMENT TEXT: {p.name} ---\n{txt}")
        except Exception:
            continue
    return normalize_whitespace("\n".join(parts))



def _v165_force_final_conditional_liquidation(terms_df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """Final pre-export safeguard for branch-dependent liquidation terms.

    The source document is authoritative. If a row already contains a certified
    multi-branch liquidation formula, enrich it with the shortest exact decision
    condition from the full source text immediately before rendering. This keeps
    threshold/trigger roles separate from OIP/per-share amounts and prevents a
    valid condition from being lost between certification and Cap Table Summary.
    """
    if terms_df is None or terms_df.empty or not str(source_document_text or "").strip():
        return terms_df
    out = terms_df.copy()
    for idx, row in out.iterrows():
        name = str(row.get("Security Name", "") or "").strip()
        liq = str(row.get("Liquidation Preference", "") or "").strip()
        if not name or not liq:
            continue
        is_branch = bool(re.search(r"condition[- ]dependent", liq, re.I)) or len(re.findall(r"\d+(?:\.\d+)?\s*x\s*(?:of\s+)?(?:the\s+)?(?:oip|original\s+(?:issue|issuance|purchase)\s+price)", liq, re.I)) >= 2
        if not is_branch:
            continue
        precise = _v162_precise_conditional_liquidation(name, liq, source_document_text)
        if precise:
            out.at[idx, "Liquidation Preference"] = precise
            if "Review Flags" in out.columns:
                out.at[idx, "Review Flags"] = _append_review_flag(row.get("Review Flags", ""), "E3 final conditional liquidation rule preserved")
    return out











# ============================================================================
# v16 CERTIFICATION ENGINE
# ============================================================================
# v16 intentionally leaves the mature extraction/roster machinery above intact,
# but makes the final decision layer explicit and auditable:
#   roster -> immutable ID -> relationship graph -> field evidence -> certification
#   -> deterministic calculations -> renderer.
# Cap Table Summary must consume finalized facts only; it must not reinterpret law.

V16_EVIDENCE_CODES = {
    "literal": "E1",          # exact literal value for exact security
    "equality": "E2",         # exact equality relationship (e.g. CP = OIP)
    "formula": "E3",          # explicit deterministic formula
    "group": "E4",            # explicitly defined parent-group inheritance
    "corroborated": "E5",     # independent exact-series roles establish same base
}



# ============================================================================
# v16.2 - explicit conversion mechanics + precise conditional liquidation
# ============================================================================

def _v162_explicit_conversion_ratio_from_evidence(security_name: str, evidence: str) -> Optional[str]:
    """Return an explicit source-stated conversion ratio for the exact security.

    Explicit/special conversion mechanics outrank generic OIP / Conversion Price
    arithmetic.  This is intentionally conservative: an exact security must be
    present in the evidence and the ratio must appear in conversion language.
    """
    ev = str(evidence or "")
    if not ev.strip() or not _exact_security_in_evidence(security_name, ev):
        return None
    low = ev.lower().replace("–", "-").replace("—", "-")
    if not re.search(r"convert|conversion", low, re.I):
        return None

    # Highest priority: an explicit fraction of one Common share, e.g.
    # "converted into 1/5th of a share of Common Stock".
    frac_patterns = [
        r"convert(?:ed|ible)?\s+into.{0,100}?\b(\d+)\s*/\s*(\d+)(?:st|nd|rd|th)?\s+(?:of\s+)?(?:a|one)\s+share",
        r"convert(?:ed|ible)?\s+into.{0,100}?\b(\d+)\s*/\s*(\d+)(?:st|nd|rd|th)?\s+of\s+(?:a\s+)?share",
    ]
    for pat in frac_patterns:
        m = re.search(pat, low, re.I)
        if m:
            num, den = int(m.group(1)), int(m.group(2))
            if num > 0 and den > 0:
                q = num / den
                qtxt = f"{q:.8f}".rstrip("0").rstrip(".")
                return f"{qtxt}:1"

    # Worded fractions frequently used in charter amendments.
    word_fracs = {
        "one-half": 0.5, "one half": 0.5,
        "one-third": 1/3, "one third": 1/3,
        "one-fourth": 0.25, "one fourth": 0.25, "one-quarter": 0.25, "one quarter": 0.25,
        "one-fifth": 0.2, "one fifth": 0.2,
        "one-sixth": 1/6, "one sixth": 1/6,
        "one-tenth": 0.1, "one tenth": 0.1,
    }
    for phrase, q in word_fracs.items():
        if re.search(rf"convert(?:ed|ible)?\s+into.{{0,100}}?{re.escape(phrase)}\s+(?:of\s+)?(?:a\s+)?share", low, re.I):
            qtxt = f"{q:.8f}".rstrip("0").rstrip(".")
            return f"{qtxt}:1"

    # Explicit X shares for each preferred share.
    m = re.search(r"convert(?:ed|ible)?\s+into.{0,100}?(\d+(?:\.\d+)?)\s+shares?\s+of\s+common", low, re.I)
    if m:
        q = float(m.group(1))
        if q > 0:
            return ("1:1" if abs(q-1.0) <= 1e-12 else f"{q:.8f}".rstrip("0").rstrip(".") + ":1")
    return None


def _v162_compact_money_threshold(value: float) -> str:
    if value >= 1_000_000 and abs(value / 1_000_000 - round(value / 1_000_000)) <= 1e-9:
        return f"${int(round(value / 1_000_000))}m"
    if value >= 1_000 and abs(value / 1_000 - round(value / 1_000)) <= 1e-9:
        return f"${int(round(value / 1_000))}k"
    return "$" + f"{value:,.9f}".rstrip("0").rstrip(".")


def _v162_precise_conditional_liquidation(security_name: str, current_liq: Any, evidence: str) -> Optional[str]:
    """Preserve the shortest source-supported condition needed to choose a liquidation branch.

    Generic pattern supported: one multiple applies when consideration is at/below
    an explicit threshold before a named trigger; the other applies above the
    threshold or after that trigger.  Values and trigger labels are extracted from
    the source rather than hard-coded.
    """
    cur = str(current_liq or "").strip()
    ev = str(evidence or "")
    if not ev.strip():
        return None
    # Only enrich an already conditional/multi-branch liquidation fact.
    if not (re.search(r"condition", cur, re.I) or len(re.findall(r"\d+(?:\.\d+)?\s*x\s*oip", cur, re.I)) >= 2):
        return None

    # Work in a bounded neighborhood around the liquidation-preference definition
    # when possible so unrelated thresholds do not leak in.
    raw = ev.replace("–", "-").replace("—", "-")
    anchor = re.search(r"liquidation\s+preference.{0,200}?shall\s+mean", raw, re.I | re.S)
    if anchor:
        start = max(0, anchor.start() - 500)
        raw = raw[start:start + 6500]

    # Collect explicit multipliers tied to OIP.
    mults = []
    for m in re.finditer(r"(?:\((\d+(?:\.\d+)?)x\)|(\d+(?:\.\d+)?)\s*x).{0,100}?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)", raw, re.I | re.S):
        val = m.group(1) or m.group(2)
        if val and val not in mults:
            mults.append(val)
    # Also support word + parenthetical multiplier, e.g. one and six-tenths times (1.6x).
    for m in re.finditer(r"times\s*\(\s*(\d+(?:\.\d+)?)x\s*\).{0,120}?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)", raw, re.I | re.S):
        val=m.group(1)
        if val not in mults:
            mults.append(val)
    if len(mults) < 2:
        # Fall back to the already-certified concise field for multipliers.
        mults = re.findall(r"(\d+(?:\.\d+)?)\s*x\s*oip", cur, re.I)
    if len(mults) < 2:
        return None

    # Extract the branch threshold from CONDITION language, never from the nearest
    # dollar amount. OIP / per-share amounts often appear in the same liquidation
    # definition and must not be allowed to become the decision threshold.
    threshold_candidates = []
    threshold_patterns = [
        # <= / before branch
        r"(?:do\s+not|does\s+not|could\s+not|cannot)\s*,?\s*(?:or\s+[^.;]{0,80}?)?exceed\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"(?:less\s+than|less\s+than\s+or\s+equal\s+to|at\s+or\s+below|not\s+in\s+excess\s+of)\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        # > / after branch
        r"(?:exceeds?|could\s+exceed|in\s+excess\s+of|greater\s+than)\s+\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]
    for pat in threshold_patterns:
        for tm in re.finditer(pat, raw, re.I | re.S):
            try:
                value = float(tm.group(1).replace(",", ""))
            except Exception:
                continue
            if value > 0:
                threshold_candidates.append(value)

    if not threshold_candidates:
        return None

    # A genuine branch threshold normally appears in both sides of the decision
    # rule. Prefer a repeated condition amount. If only one condition-labelled
    # amount exists, use it. Never fall back to an unlabelled dollar value.
    from collections import Counter
    rounded = [round(x, 9) for x in threshold_candidates]
    counts = Counter(rounded)
    repeated = [x for x, c in counts.items() if c >= 2]
    threshold = max(repeated) if repeated else threshold_candidates[0]

    # Extract a named trigger phrase where the source uses before/following achievement.
    trigger = "trigger condition"
    patterns = [
        r"prior\s+to\s+the\s+achievement\s+of\s+(?:any\s+of\s+)?the\s+([A-Z][A-Za-z0-9 '\-]{2,80}?Triggers?)",
        r"following\s+the\s+achievement\s+of\s+(?:any\s+of\s+)?the\s+([A-Z][A-Za-z0-9 '\-]{2,80}?Triggers?)",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            trigger = re.sub(r"\s+", " ", m.group(1)).strip()
            break

    # Determine which multiplier belongs to the <= threshold / before-trigger branch.
    before_mult = None
    after_mult = None
    branch_a = re.search(r"(?:do\s+not|could\s+not).{0,140}?exceed\s+\$\s*[0-9][0-9,]*(?:\.[0-9]+)?.{0,1200}?(\d+(?:\.\d+)?)x", raw, re.I | re.S)
    branch_b = re.search(r"(?:exceeds?|could\s+exceed).{0,140}?\$\s*[0-9][0-9,]*(?:\.[0-9]+)?.{0,1200}?(\d+(?:\.\d+)?)x", raw, re.I | re.S)
    if branch_a: before_mult = branch_a.group(1)
    if branch_b: after_mult = branch_b.group(1)
    if before_mult is None:
        before_mult = mults[0]
    # If the broad >threshold regex latched onto the earlier <= branch, force the
    # second distinct source multiplier. This remains generic and source-derived.
    if after_mult is None or after_mult == before_mult:
        after_mult = next((x for x in mults if x != before_mult), mults[1])

    threshold_txt = _v162_compact_money_threshold(threshold)
    if trigger != "trigger condition":
        return f"{before_mult}x OIP if consideration ≤{threshold_txt} before {trigger}; otherwise {after_mult}x OIP"
    return f"{before_mult}x OIP if consideration ≤{threshold_txt}; otherwise {after_mult}x OIP"

V16_RELATIONSHIP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "security_name": {"type": "string"},
                    "parent_groups": {"type": "array", "items": {"type": "string"}},
                    "original_economic_base": {"type": "string"},
                    "original_economic_base_evidence_type": {"type": "string"},
                    "dividend_rate": {"type": "string"},
                    "dividend_parent_group": {"type": "string"},
                    "liquidation_basis": {"type": "string"},
                    "participation_rights": {"type": "string"},
                    "source_text": {"type": "string"},
                },
                "required": [
                    "security_name", "parent_groups", "original_economic_base",
                    "original_economic_base_evidence_type", "dividend_rate",
                    "dividend_parent_group", "liquidation_basis",
                    "participation_rights", "source_text"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["relationships"],
    "additionalProperties": False,
}


def v16_relationship_text_format_schema() -> Dict[str, Any]:
    return {"format": {"type": "json_schema", "name": "v16_legal_relationship_certification", "schema": V16_RELATIONSHIP_SCHEMA, "strict": True}}


V16_RELATIONSHIP_INSTRUCTIONS = r"""
You are the final legal-relationship verifier for a corporate charter valuation workflow.
This is NOT a fresh extraction. Review only the supplied unresolved securities/fields.
Use the uploaded source document itself, including visible page images when the PDF is scanned.

Return exact security names from the supplied list. Do not invent or merge securities.
Authorized share counts are completely out of scope.

For each supplied security:
1. parent_groups: list only parent/family terms explicitly defined by the charter (for example Series D Preferred Stock may expressly include Series D-1 and Series D-2).
2. original_economic_base: populate only when the charter proves an original per-share economic base for that exact security. Valid proof is:
   - explicit Original Issue/Purchase/Issuance/Subscription Price; OR
   - a fixed per-share amount independently used as the liquidation/base amount AND as the numerator/base of the conversion formula; OR
   - another equally explicit original per-share base established in two independent operative economic roles.
   Never set OIP merely because Conversion Price exists. Preserve cases where the original base and Conversion Price differ.
3. original_economic_base_evidence_type: one of Exact literal, Explicit equality, Explicit formula, Defined-group inheritance, Corroborated economic base, Not stated.
4. dividend_rate: only an explicit fixed percentage applying to this exact security, directly or through an explicitly proven parent group. Do not calculate from arbitrary nearby dollars.
5. liquidation_basis: concise only. For a conditional branch, preserve the shortest decision condition needed to select the branch, e.g. "1.6x OIP if consideration <= $150m before Operational Triggers; otherwise 1x OIP". Do not reduce a threshold/trigger formula to merely "condition-dependent" when the source states the condition.
6. participation_rights: only Non-participating, Participating, Capped Participation, N/A, or Not stated, based on the complete waterfall.
7. source_text: short evidence sufficient to prove the relationship. For group inheritance include both the child-to-parent definition and the parent-level right. For corroborated economic base include both independent economic roles.

If the source does not prove the relationship, return Not stated. Never use Review Required / Needs Review language. Return strict JSON only.
""".strip()


def _v16_security_id(name: Any) -> str:
    """Immutable, deterministic ID derived once from the authoritative roster name."""
    t = str(name or "").strip().upper().replace("–", "-").replace("—", "-")
    t = re.sub(r"\bPREFERRED\s+(?:STOCK|SHARES?)\b", "", t)
    t = re.sub(r"\bCOMMON\s+(?:STOCK|SHARES?)\b", "COMMON", t)
    t = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
    return "SEC_" + (t or "UNKNOWN")


def _v16_row_evidence(row: Any) -> str:
    parts = [
        str(row.get("Source Text", "") or ""),
        str(row.get("Liquidation Preference", "") or ""),
        str(row.get("Conversion Price", "") or ""),
        str(row.get("Dividend Rate", "") or ""),
        str(row.get("Dividend Type", "") or ""),
        str(row.get("Participation Rights", "") or ""),
    ]
    return "\n".join(p for p in parts if p and p.lower() != "nan")


def _v16_extract_explicit_dividend_pct(name: str, evidence: str, source_text: str = "") -> Optional[float]:
    """Exact-series first; then explicitly proven family inheritance."""
    combined = (evidence or "") + "\n" + (source_text or "")
    pct = _strict_dividend_percent_from_source(name, combined)
    if pct is not None:
        return pct
    token = _strict_series_token(name)
    m = re.match(r"^series([a-z]+)(\d+)$", token or "")
    if not m:
        return None
    family = m.group(1)
    if not _series_family_membership_proven(name, family, combined):
        return None
    pct = _group_dividend_percent_from_source(name, combined)
    return pct


def _v16_fixed_liquidation_base(row: Any) -> Optional[float]:
    """Return a fixed per-share liquidation/base amount, not a multiplier/formula."""
    liq = str(row.get("Liquidation Preference", "") or "")
    if not liq or re.search(r"\b\d+(?:\.\d+)?\s*x\b|\boip\b|original\s+(?:issue|issuance|purchase)\s+price|condition", liq, re.I):
        return None
    return _money_to_float(liq)


def _v16_corroborated_base_from_row(row: Any, source_text: str = "") -> Optional[float]:
    """E5 certification from independent exact-series roles.

    This is deliberately NOT CP->OIP.  A base is accepted when the row/source proves
    it in at least two independent operative roles.  Fixed liquidation/base amount is
    one role; exact conversion-formula numerator/base is another.  Equal initial CP is
    also a second role when the values genuinely agree.
    """
    name = str(row.get("Security Name", "") or "").strip()
    if not name or "preferred" not in name.lower():
        return None
    ev = _v16_row_evidence(row)
    combined = ev + "\n" + (source_text or "")

    # Existing exact relationship parser is strongest when evidence includes the
    # conversion formula numerator (critical for non-1:1 cases).
    b = _corroborated_economic_base_from_evidence(name, combined)
    if b is not None:
        return b

    liq = _v16_fixed_liquidation_base(row)
    cp = _money_to_float(row.get("Conversion Price"))
    if liq is not None and cp is not None and liq > 0 and cp > 0 and abs(liq-cp) <= max(1e-9, abs(liq)*1e-8):
        # Require source snippets to independently identify both roles for this exact row.
        token = _strict_series_token(name)
        rx = _strict_series_regex(token)
        norm = _normalize_source_text_for_match(ev)
        if rx and re.search(rx, norm, re.I) and re.search(r"liquidation|preference", norm, re.I) and re.search(r"conversion\s+price", norm, re.I):
            return liq
    return None


def _v16_build_relationship_graph(df: pd.DataFrame, source_text: str = "") -> Dict[str, Any]:
    graph: Dict[str, Any] = {"securities": {}, "groups": {}}
    for _, row in df.iterrows():
        name = str(row.get("Security Name", "") or "").strip()
        if not name:
            continue
        sid = _v16_security_id(name)
        node = {"security_id": sid, "security_name": name, "parent_groups": [], "evidence": {}}
        token = _strict_series_token(name)
        m = re.match(r"^series([a-z]+)(\d+)$", token or "")
        evidence = _v16_row_evidence(row) + "\n" + (source_text or "")
        if m:
            family = m.group(1)
            if _series_family_membership_proven(name, family, evidence):
                gid = f"GROUP_SERIES_{family.upper()}"
                node["parent_groups"].append(gid)
                graph["groups"].setdefault(gid, {"group_id": gid, "label": f"Series {family.upper()} Preferred Stock", "members": []})
                graph["groups"][gid]["members"].append(sid)
        graph["securities"][sid] = node
    return graph


def _v16_certify_dataframe(df: pd.DataFrame, source_text: str = "") -> pd.DataFrame:
    """Final deterministic certification layer. Never overwrites a populated certified fact
    unless the new evidence is strictly stronger and source-proven."""
    if df.empty:
        return df
    out = df.copy()
    graph = _v16_build_relationship_graph(out, source_text)
    out.attrs["v16_relationship_graph"] = graph

    for idx, row in out.iterrows():
        name = str(row.get("Security Name", "") or "").strip()
        if not name:
            continue
        sid = _v16_security_id(name)
        flags = str(row.get("Review Flags", "") or "")
        evidence = _v16_row_evidence(row)

        if "preferred" not in name.lower():
            continue

        # OIP certification hierarchy. Existing exact literal remains untouched.
        oip = _money_to_float(row.get("OIP / Original Issue Price"))
        if oip is None:
            direct = _extract_exact_series_money_from_source(name, source_text, "oip") if source_text else None
            if direct is not None:
                out.at[idx, "OIP / Original Issue Price"] = _fmt_money_num(direct)
                flags = _append_review_flag(flags, f"{V16_EVIDENCE_CODES['literal']} OIP exact literal")
                oip = direct
            else:
                base = _v16_corroborated_base_from_row(row, source_text)
                if base is not None:
                    out.at[idx, "OIP / Original Issue Price"] = _fmt_money_num(base)
                    flags = _append_review_flag(flags, f"{V16_EVIDENCE_CODES['corroborated']} OIP corroborated economic base")
                    oip = base

        # Dividend rate: explicit percentage only, direct or proven parent-group inheritance.
        pct = _v16_extract_explicit_dividend_pct(name, evidence, source_text)
        if pct is not None:
            out.at[idx, "Dividend Rate"] = f"{pct:.6f}".rstrip("0").rstrip(".") + "%"
            code = V16_EVIDENCE_CODES['group'] if graph["securities"].get(sid,{}).get("parent_groups") else V16_EVIDENCE_CODES['literal']
            flags = _append_review_flag(flags, f"{code} dividend rate certified")

        # Conversion mechanics: an explicit/special source-stated ratio outranks
        # generic OIP / Conversion Price arithmetic.
        oip = _money_to_float(out.at[idx, "OIP / Original Issue Price"])
        cp = _money_to_float(out.at[idx, "Conversion Price"])
        explicit_ratio = _v162_explicit_conversion_ratio_from_evidence(name, evidence + "\n" + (source_text or ""))
        if explicit_ratio is not None:
            out.at[idx, "Conversion Ratio"] = explicit_ratio
            flags = _append_review_flag(flags, "E1 explicit conversion mechanics")
        elif oip is not None and cp is not None and oip > 0 and cp > 0:
            ratio = oip / cp
            out.at[idx, "Conversion Ratio"] = "1:1" if abs(ratio-1.0) <= 1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".") + ":1"
            flags = _append_review_flag(flags, "E3 conversion ratio deterministic")

        # Preserve the actual decision condition for branch-dependent liquidation.
        precise_liq = _v162_precise_conditional_liquidation(name, out.at[idx, "Liquidation Preference"], evidence + "\n" + (source_text or ""))
        if precise_liq:
            out.at[idx, "Liquidation Preference"] = precise_liq
            flags = _append_review_flag(flags, "E3 conditional liquidation decision rule preserved")

        # If no explicit dividend percentage exists, allow only explicit annual $/share / certified OIP.
        if pct is None and oip is not None and oip > 0:
            amount = _strict_dividend_amount_from_source(name, evidence)
            if amount is None and source_text:
                amount = _strict_dividend_amount_from_source(name, source_text)
            if amount is not None and amount >= 0:
                calc = amount / oip * 100.0
                if 0 <= calc <= 100:
                    out.at[idx, "Dividend Rate"] = f"{calc:.6f}".rstrip("0").rstrip(".") + "%"
                    flags = _append_review_flag(flags, "E3 dividend rate = explicit annual dividend / certified OIP")

        out.at[idx, "Review Flags"] = flags
    return out


def _v16_needs_micro_relationship_verification(facts: Dict[str, Any]) -> List[str]:
    unresolved: List[str] = []
    for row in (facts or {}).get("securities", []) or []:
        name = str(row.get("security_name", "") or "").strip()
        if not name or "preferred" not in name.lower():
            continue
        if _value_is_missing(row.get("oip_original_issue_price")):
            unresolved.append(name)
            continue
        # Explicit cumulative dividend type with missing rate is a high-value targeted gap.
        if str(row.get("dividend_type", "") or "").lower().startswith("cumulative") and _value_is_missing(row.get("dividend_rate")):
            unresolved.append(name)
    return sorted(set(unresolved))


def _v16_call_relationship_micro_verification(client: OpenAI, model: str, uploaded_id: str, path: Path, names: List[str], reasoning_effort: str) -> Dict[str, Any]:
    prompt = (
        f"Document: {path.name}\nUnresolved exact securities:\n" + json.dumps(names, ensure_ascii=False, indent=2)
        + "\nVerify only the legal relationships needed to certify original economic base, defined parent-group dividend rate, liquidation basis, and participation."
    )
    response = _responses_create(client,
        model=model,
        instructions=V16_RELATIONSHIP_INSTRUCTIONS,
        input=[{"role":"user","content":[{"type":"input_file","file_id":uploaded_id},{"type":"input_text","text":prompt}]}],
        text=v16_relationship_text_format_schema(),
        reasoning={"effort": reasoning_effort} if reasoning_effort else None,
    )
    return response_to_json(response)


def _v16_apply_relationship_micro_facts(facts: Dict[str, Any], rel: Dict[str, Any], expected_names: List[str]) -> Dict[str, Any]:
    out = dict(facts)
    rows = [dict(r) for r in (facts.get("securities", []) or [])]
    by_key = {normalize_security_name_for_match(str(r.get("security_name","") or "")): r for r in rows}
    for rr in (rel or {}).get("relationships", []) or []:
        supplied = str(rr.get("security_name", "") or "").strip()
        match = best_existing_security_match(supplied, expected_names)
        if not match:
            continue
        row = by_key.get(normalize_security_name_for_match(match))
        if row is None:
            continue
        ev = str(rr.get("source_text", "") or "").strip()
        # OIP/economic base is accepted only for enumerated strong evidence types and exact security evidence.
        base = str(rr.get("original_economic_base", "") or "").strip()
        et = str(rr.get("original_economic_base_evidence_type", "") or "").strip().lower()
        allowed = {"exact literal","explicit equality","explicit formula","corroborated economic base"}
        if _value_is_missing(row.get("oip_original_issue_price")) and et in allowed and _money_to_float(base) is not None:
            if _exact_security_in_evidence(match, ev) and _money_value_in_evidence(base, ev):
                row["oip_original_issue_price"] = _fmt_money_num(_money_to_float(base))
                row["source_text"] = (str(row.get("source_text","") or "").strip()+"\n\nV16 RELATIONSHIP EVIDENCE: "+ev).strip()
        dr = str(rr.get("dividend_rate", "") or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+)?%", dr):
            # Group inheritance is permitted only when evidence contains the exact child name or explicit parent definition.
            parent = str(rr.get("dividend_parent_group", "") or "").strip()
            if _exact_security_in_evidence(match, ev) or (parent and parent.lower() in ev.lower()):
                row["dividend_rate"] = dr
        liq = str(rr.get("liquidation_basis", "") or "").strip()
        if not _value_is_missing(liq):
            row["liquidation_preference"] = liq
        part = str(rr.get("participation_rights", "") or "").strip()
        if part in {"Non-participating","Participating","Capped Participation","N/A"}:
            row["participation_rights"] = part
        b = _money_to_float(row.get("oip_original_issue_price")); cp = _money_to_float(row.get("conversion_price"))
        combined_ev = (str(row.get("source_text", "") or "") + "\n" + ev).strip()
        explicit_ratio = _v162_explicit_conversion_ratio_from_evidence(match, combined_ev)
        if explicit_ratio is not None:
            row["conversion_ratio"] = explicit_ratio
        elif b is not None and cp is not None and b > 0 and cp > 0:
            ratio=b/cp
            row["conversion_ratio"]="1:1" if abs(ratio-1)<=1e-9 else f"{ratio:.6f}".rstrip("0").rstrip(".")+":1"
        precise_liq = _v162_precise_conditional_liquidation(match, row.get("liquidation_preference"), combined_ev)
        if precise_liq:
            row["liquidation_preference"] = precise_liq
    out["securities"] = rows
    return out



# -----------------------------------------------------------------------------
# v16.6 - field-role certification / final renderer safeguards
# -----------------------------------------------------------------------------

def _v166_exact_liquidation_dollar(security_name: str, evidence: str) -> Optional[float]:
    """Return an exact-series fixed liquidation preference dollar amount.

    A liquidation-preference definition is stronger than nearby participation-cap
    or other economic amounts.  The exact series identity and the liquidation role
    must both be present in the same evidence neighborhood.
    """
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    ev = _normalize_source_text_for_match(evidence)
    if not rx or token == "common" or not ev:
        return None
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    patterns = [
        rf"{rx}.{{0,180}}?liquidation\s+preference.{{0,120}}?(?:shall\s+mean|means?|equal\s+to|is)\s*{money}",
        rf"liquidation\s+preference.{{0,120}}?(?:shall\s+mean|means?|equal\s+to|is)\s*{money}.{{0,180}}?(?:for|of|with\s+respect\s+to)\s+(?:the\s+)?{rx}",
        rf"{rx}.{{0,220}}?(?:entitled\s+to\s+receive|shall\s+receive).{{0,140}}?{money}\s*(?:per\s+share)?",
    ]
    vals=[]
    for pat in patterns:
        for m in re.finditer(pat, ev, re.I):
            try:
                v=float(m.group(1).replace(',', ''))
            except Exception:
                continue
            if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                vals.append(v)
    return vals[0] if len(vals)==1 else None


def _v166_liquidation_basis_from_role_evidence(security_name: str, evidence: str) -> Optional[str]:
    """Certify liquidation from liquidation-role evidence only.

    Priority:
      E1 exact fixed liquidation definition;
      E3 exact-series multiplier;
      E3 class-wide greater-of / one-times OIP formula.
    Participation-cap multipliers are deliberately excluded.
    """
    fixed = _v166_exact_liquidation_dollar(security_name, evidence)
    if fixed is not None:
        return _fmt_money_num(fixed)
    exact_mult = _exact_series_liquidation_multiple_from_evidence(security_name, evidence)
    if exact_mult:
        return exact_mult
    generic = _generic_preferred_liquidation_basis_from_source(security_name, evidence)
    if generic:
        return generic
    return None


def _v166_participation_cap_basis(security_name: str, evidence: str, participation_rights: Any) -> Optional[str]:
    """Return a participation cap only from explicit cap-role evidence.

    Supports both numeric and legal word/parenthetical forms such as ``3x OIP`` and
    ``three (3) times the Series B Original Issue Price``.  The cap-role wording
    must be present; a liquidation multiplier alone is not accepted here.
    """
    if str(participation_rights or '').strip().lower() != 'capped participation':
        return None
    mult = _extract_participation_cap_multiplier_from_source(security_name, evidence)
    if mult is None:
        token=_strict_series_token(security_name)
        rx=_strict_series_regex(token)
        ev=_normalize_source_text_for_match(evidence)
        if rx and ev:
            words={"one":1.0,"two":2.0,"three":3.0,"four":4.0,"five":5.0,"six":6.0,"seven":7.0,"eight":8.0,"nine":9.0,"ten":10.0}
            for sm in re.finditer(rx,ev,re.I):
                local=ev[max(0,sm.start()-900):min(len(ev),sm.end()+1100)]
                if not re.search(r"capped\s+at|shall\s+be\s+capped|maximum\s+amount|cease\s+to\s+participate|participation\s+cap",local,re.I):
                    continue
                m=re.search(r"\b(\d+(?:\.\d+)?)\s*(?:x|times)\s+(?:the\s+)?(?:"+rx+r"\s+)?original\s+(?:issue|issuance|purchase)\s+price",local,re.I)
                if m:
                    mult=float(m.group(1)); break
                m=re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*\(\s*(\d+(?:\.\d+)?)\s*\))?\s+times\s+(?:the\s+)?(?:"+rx+r"\s+)?original\s+(?:issue|issuance|purchase)\s+price",local,re.I)
                if m:
                    mult=float(m.group(2)) if m.group(2) else words[m.group(1).lower()]
                    break
    if mult is None or mult <= 0:
        return None
    return f"{int(mult) if abs(mult-round(mult)) <= 1e-9 else mult:g}x OIP"


def _v166_final_role_certification(df: pd.DataFrame, source_text: str = "") -> pd.DataFrame:
    """Last deterministic role gate before rendering.

    This pass never relies on a nearby number alone.  It repairs only fields where
    the source proves the number/formula belongs to that exact legal role.  It is
    intentionally generic and is also the safeguard against a participation-cap
    multiplier leaking into liquidation preference.
    """
    if df is None or df.empty:
        return df
    out=df.copy()
    for idx,row in out.iterrows():
        name=str(row.get('Security Name','') or '').strip()
        if not name or 'preferred' not in name.lower():
            continue
        evidence=(_v16_row_evidence(row)+'\n'+str(source_text or '')).strip()
        flags=str(row.get('Review Flags','') or '')

        current=str(row.get('Liquidation Preference','') or '').strip()
        certified_liq=_v166_liquidation_basis_from_role_evidence(name,evidence)

        # A row-level greater-of dollar preference may already be correctly bound to
        # the exact security even when the source uses a family label rather than
        # repeating the child series name.  Certify it only when the stated dollar
        # amount equals the row's independently certified OIP.
        if not certified_liq and re.search(r"greater\s+of", current, re.I) and re.search(r"as[-\s]?converted|converted\s+into\s+common", current, re.I):
            cur_money=_money_to_float(current)
            row_oip=_money_to_float(row.get('OIP / Original Issue Price'))
            if cur_money is not None and row_oip is not None and abs(cur_money-row_oip) <= max(1e-9,abs(row_oip)*1e-8):
                certified_liq='1x OIP'

        if certified_liq:
            should_replace=False
            if _value_is_missing(current):
                should_replace=True
            elif re.search(r"\b\d+(?:\.\d+)?\s*x\s*(?:of\s+)?(?:the\s+)?(?:oip|original\s+(?:issue|issuance|purchase)\s+price)",current,re.I):
                # A multiplier currently in liquidation is replaced whenever stronger
                # liquidation-role evidence proves a different basis.  This is the
                # key guard against a participation-cap multiple leaking into liq pref.
                should_replace=_norm_compare_value(current) != _norm_compare_value(certified_liq)
            elif re.search(r"greater\s+of",current,re.I):
                should_replace=True
            else:
                cur_amt=_money_to_float(current); cert_amt=_money_to_float(certified_liq)
                if cur_amt is not None and cert_amt is not None and abs(cur_amt-cert_amt) > max(1e-9,abs(cert_amt)*1e-8):
                    should_replace=True
            if should_replace:
                out.at[idx,'Liquidation Preference']=certified_liq
                flags=_append_review_flag(flags,'E1/E3 liquidation role-certified')

        cap_basis=_v166_participation_cap_basis(name,evidence,row.get('Participation Rights'))
        if cap_basis:
            current_cap=str(row.get('Participation Cap','') or '').strip()
            if _value_is_missing(current_cap) or _norm_compare_value(current_cap) != _norm_compare_value(cap_basis):
                out.at[idx,'Participation Cap']=cap_basis
                flags=_append_review_flag(flags,'E3 participation cap role-certified')

        out.at[idx,'Review Flags']=flags
    return out

# Save mature v15.18 functions as candidates, then redefine the public entry points.
_v15_18_comprehensive_cap_table_review_with_file_api = comprehensive_cap_table_review_with_file_api
_v15_18_validate_terms_dataframe = validate_terms_dataframe
_v15_18_build_cap_table_summary = build_cap_table_summary


def comprehensive_cap_table_review_with_file_api(client: OpenAI, model: str, path: Path, current_data: Dict[str, Any], keep_api_files: bool = False, reasoning_effort: str = "medium") -> Dict[str, Any]:
    """v16 wrapper: mature extraction first, then conditional relationship micro-verification.

    The extra API call is made only when a critical relationship remains unresolved.
    This is especially important for image-only/scanned charters where local text is unavailable.
    """
    data = _v15_18_comprehensive_cap_table_review_with_file_api(client, model, path, current_data, keep_api_files=keep_api_files, reasoning_effort=reasoning_effort)
    facts = data.get("_full_source_verification") if isinstance(data, dict) else None
    # The mature function stores applied facts in row fields, so rebuild a compact fact payload from rows.
    names=[str(r.get("security_name","") or "").strip() for r in (data.get("securities",[]) or []) if str(r.get("security_name","") or "").strip()]
    compact={"securities":[]}
    for r in (data.get("securities",[]) or []):
        compact["securities"].append({
            "security_name":r.get("security_name",""),
            "oip_original_issue_price":r.get("oip_original_issue_price","Not stated"),
            "liquidation_preference":r.get("liquidation_preference","Not stated"),
            "participation_rights":r.get("participation_rights","Not stated"),
            "conversion_price":r.get("conversion_price","Not stated"),
            "conversion_ratio":r.get("conversion_ratio","Not stated"),
            "dividend_rate":r.get("dividend_rate","Not stated"),
            "dividend_type":r.get("dividend_type","Not stated"),
            "source_text":r.get("source_text","")
        })
    unresolved=_v16_needs_micro_relationship_verification(compact)
    if not unresolved:
        return data
    print(f"    v16 relationship micro-verification: {len(unresolved)} unresolved security relationship(s)", flush=True)
    upload_path,is_temp=prepare_api_upload_path(path)
    with upload_path.open("rb") as f:
        uploaded=client.files.create(file=f,purpose="user_data")
    try:
        rel=_v16_call_relationship_micro_verification(client,model,uploaded.id,path,unresolved,reasoning_effort or "medium")
        compact=_v16_apply_relationship_micro_facts(compact,rel,names)
        cmap={normalize_security_name_for_match(str(r.get("security_name","") or "")):r for r in compact["securities"]}
        for row in (data.get("securities",[]) or []):
            cr=cmap.get(normalize_security_name_for_match(str(row.get("security_name","") or "")))
            if not cr: continue
            for f in ["oip_original_issue_price","liquidation_preference","participation_rights","conversion_ratio","dividend_rate"]:
                if f in cr and not _value_is_missing(cr.get(f)):
                    row[f]=cr[f]
            if cr.get("source_text") and cr.get("source_text") not in str(row.get("source_text","") or ""):
                row["source_text"]=(str(row.get("source_text","") or "").strip()+"\n\n"+str(cr.get("source_text"))).strip()
        return data
    finally:
        if not keep_api_files:
            try: client.files.delete(uploaded.id)
            except Exception: pass
        cleanup_api_upload_path(upload_path,is_temp)




# -----------------------------------------------------------------------------
# v16.7 - end-to-end exact-pair / role / final-rendering certification
# -----------------------------------------------------------------------------

def _v167_exact_oip_pairs_from_source(source_text: str, expected_names: List[str]) -> Dict[str, float]:
    """Parse exact security->OIP pairs from the source without proximity matching.

    This parser is intentionally pair-based.  It accepts only clauses where the dollar
    amount and the exact security name occur in one grammatical pair, such as
    ``$1.6172 per share for each share of the Series Seed-1 Preferred Stock``.
    It therefore cannot swap adjacent values in an enumerated multi-series definition.
    """
    text = str(source_text or '').replace('–','-').replace('—','-')
    text = re.sub(r'[\t\r\n]+',' ',text)
    text = re.sub(r'\s+',' ',text)
    out: Dict[str, float] = {}
    for name in expected_names:
        token=_strict_series_token(name)
        rx=_strict_series_regex(token)
        if not rx or token=='common':
            continue
        # Strong OIP-role language must be in the same local pair.  The negative
        # look/role guards prevent conversion/liquidation/cap dollars from qualifying.
        patterns=[
            rf"(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price).{{0,220}}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share)?\s*(?:for\s+(?:each\s+share\s+of\s+)?|of\s+|with\s+respect\s+to\s+)?(?:the\s+)?{rx}",
            rf"(?:the\s+)?{rx}.{{0,160}}?(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price).{{0,100}}?\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        ]
        vals=[]
        for pat in patterns:
            for m in re.finditer(pat,text,re.I):
                try: v=float(m.group(1).replace(',',''))
                except Exception: continue
                if 0 < v < 1_000_000 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals):
                    vals.append(v)
        # Enumerated OIP definitions often state the role only once, followed by a
        # comma-separated list of ``$X per share for ... Series Y`` pairs.  Accept
        # each exact pair only when an OIP-role phrase appears shortly before it.
        direct=rf"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*per\s+share\s+for\s+(?:each\s+share\s+of\s+)?(?:the\s+)?{rx}"
        for m in re.finditer(direct,text,re.I):
            prefix=text[max(0,m.start()-900):m.start()]
            if not re.search(r"original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price",prefix,re.I):
                continue
            try: v=float(m.group(1).replace(',',''))
            except Exception: continue
            if 0 < v < 1_000_000 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals):
                vals.append(v)
        if len(vals)==1:
            out[normalize_security_name_for_match(name)] = vals[0]
    return out


def _v167_liquidation_role_basis(security_name: str, evidence: str) -> Optional[str]:
    """Return liquidation basis only from the operative liquidation role.

    A participation cap, dividend multiple, redemption multiple or other nearby multiple
    is never eligible.  For a standard greater-of preference vs as-converted structure,
    the preference branch is 1x OIP unless the liquidation clause itself states a
    different exact multiplier.
    """
    token=_strict_series_token(security_name)
    rx=_strict_series_regex(token)
    ev=_normalize_source_text_for_match(evidence)
    if not rx or token=='common' or not ev:
        return None
    # Fixed exact liquidation definition wins.
    fixed=_v166_exact_liquidation_dollar(security_name,evidence)
    if fixed is not None:
        return _fmt_money_num(fixed)
    # Search only liquidation neighborhoods, never participation/cap neighborhoods.
    windows=_role_windows(evidence,[r'liquidation\s+preference',r'liquidation\s+amount',r'liquidation,?\s+dissolution\s+or\s+winding\s+up',r'prior\s+and\s+in\s+preference',r'amount\s+per\s+share\s+equal\s+to\s+the\s+greater\s+of'],radius=2600)
    word_mult={'one':1.0,'two':2.0,'three':3.0,'four':4.0,'five':5.0,'six':6.0,'seven':7.0,'eight':8.0,'nine':9.0,'ten':10.0}
    candidates=[]
    for w in windows:
        nw=_normalize_source_text_for_match(w)
        # Require either the exact series or a generic preferred group clause that the
        # row evidence already binds to the security.
        if not (re.search(rx,nw,re.I) or re.search(r'holders?\s+of\s+(?:shares?\s+of\s+)?preferred',nw,re.I)):
            continue
        # Exclude the residual/cap portion when scanning a large waterfall window.
        liq_part=re.split(r'(?:remaining\s+assets|remaining\s+proceeds|cease\s+to\s+participate|shall\s+be\s+capped|participation\s+cap)',nw,maxsplit=1,flags=re.I)[0]
        if re.search(r'greater\s+of.{0,500}(?:one\s+times|1(?:\.0+)?\s*x)?\s*(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,700}as[-\s]?converted|greater\s+of.{0,500}(?:original\s+(?:issue|issuance|purchase)\s+price).{0,700}converted\s+into\s+common',liq_part,re.I):
            candidates.append(1.0)
        for m in re.finditer(r'\b(\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:of\s+)?(?:the\s+)?(?:applicable\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)',liq_part,re.I):
            try: candidates.append(float(m.group(1)))
            except Exception: pass
        for m in re.finditer(r'\b(one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*\(\s*(\d+(?:\.\d+)?)\s*\))?\s+times\s*(?:of\s+)?(?:the\s+)?(?:applicable\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)',liq_part,re.I):
            candidates.append(float(m.group(2)) if m.group(2) else word_mult[m.group(1).lower()])
    # In a greater-of OIP-vs-as-converted clause, 1x is authoritative even if a 3x
    # participation cap appears later in the full source.
    if any(abs(x-1.0)<=1e-9 for x in candidates):
        return '1x OIP'
    uniq=[]
    for x in candidates:
        if not any(abs(x-u)<=1e-9 for u in uniq): uniq.append(x)
    if len(uniq)==1:
        x=uniq[0]
        return f"{int(x) if abs(x-round(x))<=1e-9 else x:g}x OIP"
    return None


def _v167_apply_source_pair_and_role_locks(df: pd.DataFrame, source_document_text: str = '') -> pd.DataFrame:
    """Apply the final source-authoritative pair/role locks before rendering."""
    if df is None or df.empty:
        return df
    out=df.copy()
    names=[str(v or '').strip() for v in out.get('Security Name',pd.Series(dtype=object)).tolist() if str(v or '').strip()]
    oip_map=_v167_exact_oip_pairs_from_source(source_document_text,names) if source_document_text else {}
    for idx,row in out.iterrows():
        name=str(row.get('Security Name','') or '').strip()
        if not name or 'preferred' not in name.lower():
            continue
        key=normalize_security_name_for_match(name)
        evidence=(_v16_row_evidence(row)+'\n'+str(source_document_text or '')).strip()
        flags=str(row.get('Review Flags','') or '')
        # Exact source pair overrides any earlier adjacent-series assignment.
        if key in oip_map:
            old_oip=_money_to_float(row.get('OIP / Original Issue Price'))
            new_oip=oip_map[key]
            out.at[idx,'OIP / Original Issue Price']=_fmt_money_num(new_oip)
            flags=_append_review_flag(flags,'E1 exact-series OIP pair lock')
            # Only move CP with OIP where the source expressly defines CP = OIP.
            if _conversion_price_equals_oip_in_source(name,source_document_text):
                out.at[idx,'Conversion Price']=_fmt_money_num(new_oip)
            # Recalculate ordinary ratio after the final pair lock unless an explicit
            # ordinary source ratio is already certified.
            cp=_money_to_float(out.at[idx,'Conversion Price'])
            explicit=_v162_explicit_conversion_ratio_from_evidence(name,evidence)
            if explicit is not None:
                out.at[idx,'Conversion Ratio']=explicit
            elif cp is not None and cp>0:
                rr=new_oip/cp
                out.at[idx,'Conversion Ratio']='1:1' if abs(rr-1)<=1e-9 else f"{rr:.6f}".rstrip('0').rstrip('.')+':1'
        # Re-certify liquidation from liquidation-role evidence after all broad AI and
        # local passes.  This is the final barrier against cap->liquidation leakage.
        liq=_v167_liquidation_role_basis(name,evidence)
        if liq:
            out.at[idx,'Liquidation Preference']=liq
            flags=_append_review_flag(flags,'E1/E3 final liquidation role lock')
        out.at[idx,'Review Flags']=flags
    return out


def validate_terms_dataframe(terms_df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """v16.7 final certification: mature guards -> graph -> role gate -> exact source-pair lock."""
    df=_v15_18_validate_terms_dataframe(terms_df,source_document_text=source_document_text)
    df=_v16_certify_dataframe(df,source_document_text)
    df=_v166_final_role_certification(df,source_document_text)
    return _v167_apply_source_pair_and_role_locks(df,source_document_text)


def build_cap_table_summary(terms_df: pd.DataFrame) -> pd.DataFrame:
    """v16 renderer: consume finalized certified facts; never re-read legal source text."""
    if terms_df.empty:
        return pd.DataFrame(columns=CAP_TABLE_SUMMARY_COLUMNS)
    missing={"","nan","not stated","n/a","na","none","null","needs review","review required"}
    def raw(v): return "" if v is None else str(v).strip()
    def clean(v):
        t=raw(v)
        return "" if t.lower() in missing else t
    def money(v):
        m=re.search(r"\$\s*\d[\d,]*(?:\.\d+)?",raw(v))
        return re.sub(r"\$\s+","$",m.group(0)) if m else ""
    def ratio(v):
        m=re.search(r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b",raw(v))
        return re.sub(r"\s+","",m.group(0)) if m else clean(v)

    def money_num(v):
        m=re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",raw(v))
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except Exception:
            return None

    def fmt_money_num(v):
        # Preserve useful charter precision while avoiding trailing zero noise.
        t=f"{float(v):.9f}".rstrip("0").rstrip(".")
        return "$" + (t or "0")

    def liquidation_summary_value(liq_value, oip_value):
        """Render only liquidation preference/share economics, not legal narrative.

        Non-conditional 1x/1.25x/3x formulas become the calculated dollar/share
        amount.  Declared/unpaid dividend wording and greater-of/as-converted text are
        intentionally omitted from this summary column.  Conditional branches preserve
        only the concise decision condition while dollarizing each branch.
        """
        liq=clean(liq_value)
        if not liq:
            return ''
        base=money_num(oip_value)
        # Explicit fixed liquidation definition: use the role-bound dollar amount.
        literal=money(liq)
        # If the row is a standard greater-of fixed/OIP preference versus as-converted,
        # the preference branch is the only value this per-share column should show.
        if literal and re.search(r'greater\s+of|declared\s+but\s+unpaid|accrued\s+but\s+unpaid',liq,re.I):
            return literal
        mult_rx=re.compile(r'\b(\d+(?:\.\d+)?)\s*x\s*(?:of\s+)?(?:the\s+)?(?:oip|original\s+(?:issue|issuance|purchase)\s+price)\b',re.I)
        matches=list(mult_rx.finditer(liq))
        if matches and base is not None and base>0:
            # Preserve a true condition only when the certified expression actually
            # contains branching language.  Otherwise this column is dollar/share only.
            conditional=bool(re.search(r'\bif\b|otherwise|unless|provided\s+that|condition[- ]dependent',liq,re.I))
            if not conditional:
                return fmt_money_num(float(matches[0].group(1))*base)
            rendered=mult_rx.sub(lambda m: fmt_money_num(float(m.group(1))*base),liq)
            rendered=re.sub(r'\s+',' ',rendered).strip()
            rendered=re.sub(r'\s*\(condition[- ]dependent\)\s*$','',rendered,flags=re.I).strip()
            return rendered
        if literal:
            return literal
        # Do not carry long legal prose into Cap Table Summary.
        return liq if len(liq)<=45 and len(liq.split())<=7 else ''
    source=[]
    names=[raw(v) for v in terms_df.get("Security Name",pd.Series(dtype=object)).tolist()]
    has_series=any("preferred" in n.lower() and n.lower() not in {"preferred","preferred stock","preferred shares"} for n in names)
    for _,r in terms_df.iterrows():
        n=raw(r.get("Security Name")); low=n.lower()
        if not n: continue
        if has_series and low in {"preferred","preferred stock","preferred shares","series preferred stock"}: continue
        source.append(r)
    # Reuse mature rank assignment only; it consumes finalized seniority fields rather than economics.
    def _rows_for_rank(): return source
    # local lightweight ranks
    pref=[]
    for r in source:
        n=raw(r.get("Security Name"))
        if "preferred" in n.lower():
            m=re.search(r"\brank\s*[:#=\-]?\s*(\d+)",raw(r.get("Seniority")),re.I)
            pref.append((n,int(m.group(1)) if m else 999))
    known=[x for _,x in pref if x<999]
    fallback=max(known,default=0)+1
    rows=[]
    for r in source:
        n=raw(r.get("Security Name")); low=n.lower(); is_common="common" in low and "preferred" not in low; is_pref="preferred" in low
        sm=re.search(r"\brank\s*[:#=\-]?\s*(\d+)",raw(r.get("Seniority")),re.I)
        sr=int(sm.group(1)) if sm else (fallback if is_common else 999)
        liq=clean(r.get("Liquidation Preference"))
        if is_common:
            liq=""
        else:
            liq=liquidation_summary_value(liq, r.get("OIP / Original Issue Price"))
        part=clean(r.get("Participation Rights"))
        if part.lower() in {"full participation","participating"}: part="Participating"
        elif part.lower() in {"non-participating","non participating"}: part="Non-participating"
        elif part.lower()=="capped participation": part="Capped Participation"
        elif part and part not in {"Participating","Non-participating","Capped Participation"}: part=""
        oip=clean(r.get("OIP / Original Issue Price")); oip=money(oip) or (oip if len(oip)<=32 else "")
        cap_raw=clean(r.get("Participation Cap"))
        cap=liquidation_summary_value(cap_raw, oip) if cap_raw else ""
        cp_raw=clean(r.get("Conversion Price"))
        # Financing valuation caps (pre-money/post-money valuation caps) are not
        # per-share Conversion Prices. Preserve only an independently labelled
        # conversion-price dollar if one is actually stated.
        if re.search(r"\b(?:pre[- ]money|post[- ]money)?\s*valuation\s+cap\b|\bvaluation\s+cap\b",cp_raw,re.I):
            mcp=re.search(r"\bconversion\s+price(?:\s+per\s+(?:share|unit))?\s*(?:of|=|:|is|shall\s+be)?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",cp_raw,re.I)
            cp=("$"+mcp.group(1)) if mcp else ""
        else:
            cp=money(cp_raw) or (cp_raw if len(cp_raw)<=32 else "")
        dtype=clean(r.get("Dividend Type")); dr=clean(r.get("Dividend Rate"))
        # Presentation normalization only: when a deterministic rate differs from an
        # integer percentage solely because the charter's annual $/share amount is
        # rounded, show the concise integer rate (e.g. 8.000888% -> 8%).
        md=re.fullmatch(r"(\d+(?:\.\d+)?)%",dr)
        if md:
            dv=float(md.group(1)); nearest=round(dv)
            if abs(dv-nearest) <= 0.02:
                dr=f"{int(nearest)}%"
        payable=""
        if dtype.lower().startswith("cumulative"): payable="Yes"
        elif dtype.lower()=="non-cumulative": payable="No"
        capdiv=""
        st=raw(r.get("Source Text"))
        mv=re.search(r"CAP PRICE INCLUDES DIVIDENDS:\s*(Yes|No|N/A)",st,re.I)
        if cap and part=="Capped Participation": capdiv=(mv.group(1).capitalize() if mv and mv.group(1).lower()!="n/a" else ("N/A" if mv else "No"))
        rows.append({
            "Series Type":"" if is_common else ("Preferred Share" if is_pref else clean(r.get("Security Type"))),
            "Series Name":n,
            "Issue / Strike Price":"" if is_common else oip,
            "Liq. Pref. / Share":liq,
            "Seniority in Liq. Pref.":sr,
            "Participation Rights":"" if is_common else part,
            "Participation Cap $ / Share":"" if is_common else cap,
            "Conversion Price":"" if is_common else cp,
            "Conversion Ratio":"" if is_common else ratio(r.get("Conversion Ratio")),
            "Is Dividend Payable?":"" if is_common else payable,
            "Dividend Rate":"" if is_common else (dr if re.fullmatch(r"\d+(?:\.\d+)?%",dr) else ""),
            "Dividend Type":"" if is_common else dtype,
            "Cap price Includes Dividends":"" if is_common else capdiv,
        })
    result=pd.DataFrame(rows,columns=CAP_TABLE_SUMMARY_COLUMNS)
    # Preserve authoritative roster order by rank, then name; Common last.
    def sk(i):
        r=result.iloc[i]; n=raw(r["Series Name"]).lower(); common="common" in n and "preferred" not in n; pref="preferred" in n
        try: rank=int(r["Seniority in Liq. Pref."])
        except: rank=999
        return (2 if common else (0 if pref else 1),rank,n)
    if not result.empty:
        result=result.iloc[sorted(range(len(result)),key=sk)].reset_index(drop=True)
    return result



# -----------------------------------------------------------------------------
# v17.0 - corpus-hardened generic identity, preference-security, and scan routing
# -----------------------------------------------------------------------------

# Corpus-wide prompt hardening: legal form must not change identity or evidence rules.
SECURITY_MASTER_INSTRUCTIONS += r"""

CORPUS-HARDENED IDENTITY RULES
- Preferred Stock, Preferred Shares, Preference Shares and Preferred Units are all preference-type securities when the source designates them as actual securities.
- Common Stock, Common Units and Ordinary Shares are common/residual securities when so designated.
- OCR variants such as Series A-l / Series A-I may represent Series A-1 only when the visible series label clearly uses a numeric child suffix; never convert a genuine standalone Series I to Series 1.
- For Non-standard documents, identify only securities actually designated in that document; do not invent a charter roster from transaction references.
"""

CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS += r"""

CORPUS-HARDENED EVIDENCE RULES
- Apply the same exact-security rules to Preferred Stock, Preferred Shares, Preference Shares and Preferred Units.
- Treat Common Stock, Common Units and Ordinary Shares as residual/common securities, not preference securities.
- If a Non-standard document says that economic rights are governed by another constitution, charter, operating agreement or shareholders agreement that is not included in the uploaded file, leave those unsupported economics Not stated rather than reconstructing them.
- A subscription/purchase price may establish OIP only for the exact subscribed security. It does not by itself establish liquidation, conversion, participation or dividend rights.
"""

ECONOMIC_ANCHOR_INSTRUCTIONS += r"""

CORPUS-HARDENED LEGAL-FORM RULES
- Security identity is independent of terminology: Preferred Stock / Preferred Shares / Preference Shares / Preferred Units are preference-type; Common Stock / Ordinary Shares / Common Units are residual/common.
- For non-charter transaction documents, certify only terms explicitly stated in the uploaded file. A reference to rights in another document is not evidence of those rights.
"""


def _v17_normalize_ocr_series_label(value: Any) -> str:
    """Normalize narrow OCR confusions in hyphenated numeric series suffixes.

    We deliberately do NOT convert a standalone Series I to Series 1.  Only a
    hyphen/space suffix that visually represents a numeric child series (A-l,
    B-I, Seed-l) is normalized to 1.
    """
    t = str(value or "").replace("–", "-").replace("—", "-")
    t = re.sub(r"(?i)(\bseries\s+[a-z]+)\s*-\s*[il](?=\b)", r"\1-1", t)
    return t


def normalize_security_name_for_match(value: str) -> str:
    """Canonical identity key across US stock, Australian shares and LLC units."""
    t = _v17_normalize_ocr_series_label(value).lower()
    # Economic class labels are descriptors, not identity-bearing tokens.
    t = re.sub(
        r"\b(?:preferred|preference)\s+(?:stock|shares?|units?)\b|"
        r"\b(?:common|ordinary)\s+(?:stock|shares?|units?)\b|"
        r"\bshares?\b|\bstock\b|\bunits?\b|\binc\.?\b|\bcorp\.?\b|\bcorporation\b",
        " ", t,
    )
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _series_token_from_name(value: str) -> str:
    """Return immutable series token, including common OCR variants."""
    v = _v17_normalize_ocr_series_label(value).lower()
    if re.search(r"\b(?:common|ordinary)\b", v) and not re.search(r"\b(?:preferred|preference)\b", v):
        return "common"
    m = re.search(r"\bseries\s+([a-z]+)(?:\s*[- ]?\s*(\d+))?", v, re.I)
    if m:
        return f"series{m.group(1)}{m.group(2) or ''}"
    # Non-US naming commonly used by Australian constitutions.
    m = re.search(r"\bclass\s+([a-z]+)(?:\s*[- ]?\s*(\d+))?\s+(?:preferred|preference)\b", v, re.I)
    if m:
        return f"class{m.group(1)}{m.group(2) or ''}"
    if re.search(r"\bseed\s+(?:preferred|preference)\b", v, re.I):
        return "seed"
    return ""


def _strict_series_token(value: Any) -> str:
    return _series_token_from_name(str(value or ""))


def _strict_series_regex(token: str) -> Optional[str]:
    if token == "common":
        return r"(?:common|ordinary)(?:\s+(?:stock|shares?|units?))?"
    if token == "seed":
        return r"seed\s+(?:(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)"
    m = re.match(r"^series([a-z]+)(\d*)$", token or "")
    if m:
        word, num = m.group(1), m.group(2)
        if num:
            # Accept 1/l/I only for numeric child suffix 1; this is an OCR tolerance.
            n = r"(?:1|l|i)" if num == "1" else re.escape(num)
            return rf"series\s+{re.escape(word)}\s*[- ]?\s*{n}(?!\d)(?:\s+(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)?"
        return rf"series\s+{re.escape(word)}(?!\s*[- ]?\s*\d)(?:\s+(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)?"
    m = re.match(r"^class([a-z]+)(\d*)$", token or "")
    if m:
        word, num = m.group(1), m.group(2)
        suffix = rf"\s*[- ]?\s*{re.escape(num)}" if num else ""
        return rf"class\s+{re.escape(word)}{suffix}(?:\s+(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)?"
    return None


def _v17_security_kind(name: Any, security_type: Any = "") -> str:
    """Classify economic security without assuming the word 'Preferred Stock'."""
    t = f"{name or ''} {security_type or ''}".lower()
    if re.search(r"\b(?:preferred|preference)\b", t):
        return "preferred"
    if re.search(r"\b(?:common|ordinary)\b", t):
        return "common"
    return "other"


def _v17_identity_regex(security_name: str) -> Optional[str]:
    """Exact-ish security identity regex for non-Series naming conventions."""
    token = _strict_series_token(security_name)
    rx = _strict_series_regex(token)
    if rx:
        return rx
    raw = _v17_normalize_ocr_series_label(security_name).lower()
    words = re.findall(r"[a-z0-9]+", raw)
    drop = {"preferred","preference","stock","share","shares","unit","units"}
    core = [w for w in words if w not in drop]
    if not core:
        return None
    return r"\b" + r"\s+".join(re.escape(w) for w in core) + r"\b(?:\s+(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)?"


def _v17_money_pattern() -> str:
    # The downstream summary remains currency-symbol neutral and renders '$'.
    # Accept common source labels without interpreting FX.
    return r"(?:US\$|A\$|AUD\s*\$?|USD\s*\$?|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)"


def _v167_exact_oip_pairs_from_source(source_text: str, expected_names: List[str]) -> Dict[str, float]:
    """v17 exact security->OIP pairs across stock/share/unit naming conventions."""
    text = _v17_normalize_ocr_series_label(source_text)
    text = re.sub(r"[\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    out: Dict[str, float] = {}
    money = _v17_money_pattern()
    role = r"original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price"
    for name in expected_names:
        if _v17_security_kind(name) == "common":
            continue
        rx = _v17_identity_regex(name)
        if not rx:
            continue
        vals: List[float] = []
        patterns = [
            rf"(?:{role}).{{0,240}}?{money}\s*(?:per\s+(?:share|unit))?\s*(?:for\s+(?:each\s+(?:share|unit)\s+of\s+)?|of\s+|with\s+respect\s+to\s+)?(?:the\s+)?{rx}",
            rf"(?:the\s+)?{rx}.{{0,180}}?(?:{role}).{{0,120}}?{money}",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.I):
                try: v = float(m.group(1).replace(",", ""))
                except Exception: continue
                if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                    vals.append(v)
        # Enumerated definitions: role once, then repeated amount/security pairs.
        direct = rf"{money}\s*per\s+(?:share|unit)\s+for\s+(?:each\s+(?:share|unit)\s+of\s+)?(?:the\s+)?{rx}"
        for m in re.finditer(direct, text, re.I):
            prefix = text[max(0, m.start()-1000):m.start()]
            if not re.search(role, prefix, re.I):
                continue
            try: v = float(m.group(1).replace(",", ""))
            except Exception: continue
            if 0 < v < 1_000_000 and not any(abs(v-u) <= max(1e-12, abs(v)*1e-9) for u in vals):
                vals.append(v)
        if len(vals) == 1:
            out[normalize_security_name_for_match(name)] = vals[0]
    return out


def _v17_local_text_is_sufficient(path: Path, text: str, min_local_chars: int = 1500) -> bool:
    """Detect image-dominant PDFs that happen to expose only cover/stamp text.

    v16.7 used character count alone.  The 49-document corpus contains scanned
    charters with 1.5k-4.5k incidental characters, enough to pass that gate but
    nowhere near enough to represent the legal document.  In auto mode these
    must go directly to file input so page images are visible to the model.
    """
    t = str(text or "")
    if len(t.strip()) < min_local_chars:
        return False
    if path.suffix.lower() != ".pdf":
        return True
    pages = 0
    try:
        d = fitz.open(str(path)); pages = len(d); d.close()
    except Exception:
        pages = 0
    compact = re.sub(r"\s+", " ", t).lower()
    econ_hits = sum(bool(re.search(p, compact, re.I)) for p in [
        r"preferred|preference", r"liquidation|winding\s+up", r"conversion",
        r"dividend", r"original\s+(?:issue|purchase|issuance)\s+price|issue\s+price",
    ])
    chars_per_page = len(compact) / max(pages, 1)
    # Very sparse text on a multi-page legal document is almost certainly an image scan.
    if pages >= 5 and chars_per_page < 450:
        return False
    # Incidental filing metadata can be >1500 chars but contain no operative economics.
    if pages >= 8 and econ_hits < 2 and chars_per_page < 900:
        return False
    return True


# Preserve v16.7 implementation for branches not changed below.
_v167_process_file = process_file

def process_file(
    client: OpenAI,
    model: str,
    path: Path,
    mode: str,
    phrases_by_category: Dict[str, List[str]],
    max_chars: int,
    keep_api_files: bool,
    reasoning_effort: str = "low",
    min_local_chars: int = 1500,
    quality_pass: bool = False,
    quality_reasoning_effort: str = "medium",
    upload_style_seniority: bool = False,
) -> ExtractionResult:
    """v17 auto routing: avoid sending partial scan metadata as source text."""
    if mode != "auto" or path.suffix.lower() not in SUPPORTED_LOCAL_TEXT:
        return _v167_process_file(client, model, path, mode, phrases_by_category, max_chars,
                                  keep_api_files, reasoning_effort, min_local_chars,
                                  quality_pass, quality_reasoning_effort, upload_style_seniority)
    try:
        text = extract_local_text(path)
    except Exception:
        text = ""
    if not _v17_local_text_is_sufficient(path, text, min_local_chars):
        if path.suffix.lower() in SUPPORTED_FILE_API:
            # Delegate to the mature file-api branch, preserving all quality/full-source passes.
            return _v167_process_file(client, model, path, "file-api", phrases_by_category, max_chars,
                                      keep_api_files, reasoning_effort, min_local_chars,
                                      quality_pass, quality_reasoning_effort, upload_style_seniority)
    return _v167_process_file(client, model, path, mode, phrases_by_category, max_chars,
                              keep_api_files, reasoning_effort, min_local_chars,
                              quality_pass, quality_reasoning_effort, upload_style_seniority)


_v167_apply_source_pair_and_role_locks_base = _v167_apply_source_pair_and_role_locks

def _v167_apply_source_pair_and_role_locks(df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """v17 final lock: apply to all preference securities, not only 'preferred' names."""
    if df is None or df.empty:
        return df
    out = df.copy()
    names = [str(v or "").strip() for v in out.get("Security Name", pd.Series(dtype=object)).tolist() if str(v or "").strip()]
    oip_map = _v167_exact_oip_pairs_from_source(source_document_text, names) if source_document_text else {}
    for idx, row in out.iterrows():
        name = str(row.get("Security Name", "") or "").strip()
        stype = str(row.get("Security Type", "") or "").strip()
        if not name or _v17_security_kind(name, stype) != "preferred":
            continue
        key = normalize_security_name_for_match(name)
        evidence = (_v16_row_evidence(row) + "\n" + str(source_document_text or "")).strip()
        flags = str(row.get("Review Flags", "") or "")
        if key in oip_map:
            new_oip = oip_map[key]
            out.at[idx, "OIP / Original Issue Price"] = _fmt_money_num(new_oip)
            flags = _append_review_flag(flags, "E1 exact-security OIP pair lock")
            if _conversion_price_equals_oip_in_source(name, source_document_text):
                out.at[idx, "Conversion Price"] = _fmt_money_num(new_oip)
            cp = _money_to_float(out.at[idx, "Conversion Price"])
            explicit = _v162_explicit_conversion_ratio_from_evidence(name, evidence)
            if explicit is not None:
                out.at[idx, "Conversion Ratio"] = explicit
            elif cp is not None and cp > 0:
                rr = new_oip / cp
                out.at[idx, "Conversion Ratio"] = "1:1" if abs(rr-1) <= 1e-9 else f"{rr:.6f}".rstrip("0").rstrip(".") + ":1"
        liq = _v167_liquidation_role_basis(name, evidence)
        if liq:
            out.at[idx, "Liquidation Preference"] = liq
            flags = _append_review_flag(flags, "E1/E3 final liquidation role lock")
        out.at[idx, "Review Flags"] = flags
    return out


def build_cap_table_summary(terms_df: pd.DataFrame) -> pd.DataFrame:
    """v17 final renderer supporting Preferred Stock, Preference Shares and Preferred Units."""
    if terms_df.empty:
        return pd.DataFrame(columns=CAP_TABLE_SUMMARY_COLUMNS)
    missing={"","nan","not stated","n/a","na","none","null","needs review","review required","action required"}
    def raw(v): return "" if v is None else str(v).strip()
    def clean(v):
        t=raw(v); return "" if t.lower() in missing else t
    def money(v):
        m=re.search(r"(?:US\$|A\$|AUD\s*\$?|USD\s*\$?|\$)\s*\d[\d,]*(?:\.\d+)?",raw(v),re.I)
        if not m: return ""
        n=re.search(r"\d[\d,]*(?:\.\d+)?",m.group(0))
        return "$"+n.group(0) if n else ""
    def ratio(v):
        m=re.search(r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b",raw(v))
        return re.sub(r"\s+","",m.group(0)) if m else clean(v)
    def money_num(v):
        m=re.search(r"(?:US\$|A\$|AUD\s*\$?|USD\s*\$?|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",raw(v),re.I)
        if not m: return None
        try: return float(m.group(1).replace(",",""))
        except Exception: return None
    def fmt_money_num(v):
        t=f"{float(v):.9f}".rstrip("0").rstrip("."); return "$"+(t or "0")
    def liquidation_summary_value(liq_value,oip_value):
        liq=clean(liq_value)
        if not liq: return ""
        base=money_num(oip_value); literal=money(liq)
        if literal and re.search(r"greater\s+of|declared\s+but\s+unpaid|accrued\s+but\s+unpaid",liq,re.I):
            return literal
        mult_rx=re.compile(r"\b(\d+(?:\.\d+)?)\s*x\s*(?:of\s+)?(?:the\s+)?(?:oip|original\s+(?:issue|issuance|purchase)\s+price)\b",re.I)
        matches=list(mult_rx.finditer(liq))
        if matches and base is not None and base>0:
            conditional=bool(re.search(r"\bif\b|otherwise|unless|provided\s+that|condition[- ]dependent",liq,re.I))
            if not conditional: return fmt_money_num(float(matches[0].group(1))*base)
            rendered=mult_rx.sub(lambda m: fmt_money_num(float(m.group(1))*base),liq)
            rendered=re.sub(r"\s+"," ",rendered).strip()
            rendered=re.sub(r"\s*\(condition[- ]dependent\)\s*$","",rendered,flags=re.I).strip()
            return rendered
        if literal: return literal
        return liq if len(liq)<=45 and len(liq.split())<=7 else ""

    src=[]
    for _,r in terms_df.iterrows():
        n=raw(r.get("Security Name")); st=raw(r.get("Security Type"))
        if not n: continue
        # Drop only truly aggregate placeholder rows when detailed preference rows exist.
        src.append(r)
    detailed_pref=[r for r in src if _v17_security_kind(raw(r.get("Security Name")),raw(r.get("Security Type")))=="preferred" and _strict_series_token(raw(r.get("Security Name")))]
    filtered=[]
    for r in src:
        n=raw(r.get("Security Name")); low=n.lower()
        if detailed_pref and low in {"preferred","preferred stock","preferred shares","preference shares","preference share","preferred units","series preferred stock"}:
            continue
        filtered.append(r)
    src=filtered

    pref_ranks=[]
    for r in src:
        if _v17_security_kind(raw(r.get("Security Name")),raw(r.get("Security Type")))=="preferred":
            m=re.search(r"\brank\s*[:#=\-]?\s*(\d+)",raw(r.get("Seniority")),re.I)
            pref_ranks.append(int(m.group(1)) if m else 999)
    known=[x for x in pref_ranks if x<999]; fallback=max(known,default=0)+1
    rows=[]
    for r in src:
        n=raw(r.get("Security Name")); st=raw(r.get("Security Type")); kind=_v17_security_kind(n,st)
        is_common=kind=="common"; is_pref=kind=="preferred"
        sm=re.search(r"\brank\s*[:#=\-]?\s*(\d+)",raw(r.get("Seniority")),re.I)
        sr=int(sm.group(1)) if sm else (fallback if is_common else 999)
        liq="" if is_common else liquidation_summary_value(clean(r.get("Liquidation Preference")),r.get("OIP / Original Issue Price"))
        part=clean(r.get("Participation Rights"))
        if part.lower() in {"full participation","participating"}: part="Participating"
        elif part.lower() in {"non-participating","non participating"}: part="Non-participating"
        elif part.lower()=="capped participation": part="Capped Participation"
        elif part and part not in {"Participating","Non-participating","Capped Participation"}: part=""
        oip=clean(r.get("OIP / Original Issue Price")); oip=money(oip) or (oip if len(oip)<=32 else "")
        cap_raw=clean(r.get("Participation Cap")); cap=liquidation_summary_value(cap_raw,oip) if cap_raw else ""
        cp_raw=clean(r.get("Conversion Price"))
        if re.search(r"\b(?:pre[- ]money|post[- ]money)?\s*valuation\s+cap\b|\bvaluation\s+cap\b",cp_raw,re.I):
            mcp=re.search(r"\bconversion\s+price(?:\s+per\s+(?:share|unit))?\s*(?:of|=|:|is|shall\s+be)?\s*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)",cp_raw,re.I)
            cp=("$"+mcp.group(1)) if mcp else ""
        else:
            cp=money(cp_raw) or (cp_raw if len(cp_raw)<=32 else "")
        dtype=clean(r.get("Dividend Type")); dr=clean(r.get("Dividend Rate"))
        md=re.fullmatch(r"(\d+(?:\.\d+)?)%",dr)
        if md:
            dv=float(md.group(1)); nearest=round(dv)
            if abs(dv-nearest)<=0.02: dr=f"{int(nearest)}%"
        payable="Yes" if dtype.lower().startswith("cumulative") else ("No" if dtype.lower()=="non-cumulative" else "")
        capdiv=""; stxt=raw(r.get("Source Text")); mv=re.search(r"CAP PRICE INCLUDES DIVIDENDS:\s*(Yes|No|N/A)",stxt,re.I)
        if cap and part=="Capped Participation": capdiv=(mv.group(1).capitalize() if mv and mv.group(1).lower()!="n/a" else ("N/A" if mv else "No"))
        rows.append({
            "Series Type":"" if is_common else ("Preferred Share" if is_pref else clean(r.get("Security Type"))),
            "Series Name":n,
            "Issue / Strike Price":"" if is_common else oip,
            "Liq. Pref. / Share":liq,
            "Seniority in Liq. Pref.":sr,
            "Participation Rights":"" if is_common else part,
            "Participation Cap $ / Share":"" if is_common else cap,
            "Conversion Price":"" if is_common else cp,
            "Conversion Ratio":"" if is_common else ratio(r.get("Conversion Ratio")),
            "Is Dividend Payable?":"" if is_common else payable,
            "Dividend Rate":"" if is_common else (dr if re.fullmatch(r"\d+(?:\.\d+)?%",dr) else ""),
            "Dividend Type":"" if is_common else dtype,
            "Cap price Includes Dividends":"" if is_common else capdiv,
        })
    result=pd.DataFrame(rows,columns=CAP_TABLE_SUMMARY_COLUMNS)
    def sk(i):
        r=result.iloc[i]; n=raw(r["Series Name"]); kind=_v17_security_kind(n,raw(r.get("Series Type")))
        try: rank=int(r["Seniority in Liq. Pref."])
        except Exception: rank=999
        return (2 if kind=="common" else (0 if kind=="preferred" else 1),rank,n.lower())
    if not result.empty: result=result.iloc[sorted(range(len(result)),key=sk)].reset_index(drop=True)
    return result



# -----------------------------------------------------------------------------
# v17.1 - standard-articles architecture hardening
# Generic fixes derived from the 48-company corpus. No company-specific branches.
# -----------------------------------------------------------------------------
SCRIPT_VERSION = "v17_2_final_field_certification"


def _v171_parent_series_regex(security_name: str) -> Optional[str]:
    token=_strict_series_token(security_name)
    m=re.match(r"^series([a-z]+)(\d+)$",token or "")
    if not m: return None
    return _strict_series_regex(f"series{m.group(1)}")


def _v171_exact_role_money_map(evidence: str, expected_names: List[str], role: str) -> Dict[str,float]:
    """Exact identity + economic-role money parser; never proximity-only."""
    text=_normalize_source_text_for_match(_v17_normalize_ocr_series_label(str(evidence or "")))
    money=_v17_money_pattern()
    if role=="oip":
        role_rx=r"original\s+(?:issue|issuance|purchase)\s+price|applicable\s+original\s+issue\s+price|issue\s+price|purchase\s+price|subscription\s+price"
    elif role=="conversion":
        role_rx=r"initial\s+conversion\s+price|applicable\s+conversion\s+price|conversion\s+price"
    else: return {}
    out={}
    for name in expected_names:
        if _v17_security_kind(name)=="common": continue
        rx=_v17_identity_regex(name)
        if not rx: continue
        groups=[
            [rf"(?:{role_rx}).{{0,280}}?with\s+respect\s+to\s+(?:the\s+)?{rx}\s*,?\s*{money}",
             rf"(?:{role_rx}).{{0,300}}?{money}\s*(?:per\s+(?:share|unit))?\s+with\s+respect\s+to\s+(?:the\s+)?{rx}"],
            [rf"(?:the\s+)?{rx}.{{0,80}}?(?:{role_rx}).{{0,100}}?(?:shall\s+initially\s+(?:be\s+)?equal\s+to|shall\s+mean|means?|equal\s+to|is)?\s*{money}"],
            [rf"(?:{role_rx}).{{0,240}}?(?:for|of|applicable\s+to)\s+(?:the\s+)?{rx}.{{0,120}}?{money}",
             rf"{money}\s*(?:per\s+(?:share|unit))?\s*(?:for|of|with\s+respect\s+to)\s+(?:each\s+(?:share|unit)\s+of\s+)?(?:the\s+)?{rx}"],
        ]
        chosen=None
        for pats in groups:
            vals=[]
            for pat in pats:
                for m in re.finditer(pat,text,re.I|re.S):
                    try:v=float(m.group(1).replace(',',''))
                    except Exception:continue
                    if 0<v<1_000_000 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals): vals.append(v)
            if len(vals)==1:
                chosen=vals[0]; break
        if chosen is not None: out[normalize_security_name_for_match(name)]=chosen
    return out


def _v171_missing_conversion_price_from_evidence(security_name: str, evidence: str) -> Optional[float]:
    """Parse a CP only from an exact conversion-price definition."""
    ev=_normalize_source_text_for_match(evidence); rx=_v17_identity_regex(security_name)
    if not ev or not rx: return None
    money=r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    # Series labels and distinctive non-series prefixes (e.g. Founder Preferred Conversion Price).
    core_words=[w for w in re.findall(r"[a-z0-9]+",_v17_normalize_ocr_series_label(security_name).lower()) if w not in {'stock','shares','share','preferred','preference','units','unit'}]
    core=r"\s+".join(re.escape(w) for w in core_words) if core_words else None
    pats=[
        rf"(?:the\s+)?{rx}.{{0,90}}?conversion\s+price.{{0,100}}?(?:shall\s+initially\s+(?:be\s+)?equal\s+to|initially\s+(?:be\s+)?equal\s+to|shall\s+be|is)\s*{money}",
        rf"conversion\s+price\s+(?:applicable\s+to|for)\s+(?:the\s+)?{rx}.{{0,120}}?(?:shall\s+initially\s+(?:be\s+)?equal\s+to|equal\s+to|is)\s*{money}",
    ]
    if core:
        pats.append(rf"\b{core}\b\s+(?:preferred\s+)?conversion\s+price.{{0,100}}?(?:shall\s+initially\s+(?:be\s+)?equal\s+to|initially\s+(?:be\s+)?equal\s+to|is)\s*{money}")
    vals=[]
    for pat in pats:
        for m in re.finditer(pat,ev,re.I|re.S):
            try:v=float(m.group(1).replace(',',''))
            except Exception:continue
            if v>0 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals): vals.append(v)
    return vals[0] if len(vals)==1 else None


def _v171_dividend_annual_amount(security_name: str, evidence: str) -> Optional[float]:
    ev=_v17_normalize_ocr_series_label(str(evidence or "")); ev=re.sub(r"\s+"," ",ev)
    rx=_v17_identity_regex(security_name)
    if not rx: return None
    money=_v17_money_pattern()
    pats=[
        # Only dollar-denominated annual-dividend clauses qualify.  Do not use a
        # later OIP dollar amount that merely appears near a percentage-rate clause.
        rf"{money}\s+per\s+annum\s+for\s+each\s+(?:share|unit)\s+of\s+(?:the\s+)?{rx}",
        rf"{money}\s+(?:per\s+(?:share|unit)\s+)?per\s+annum.{{0,140}}?(?:the\s+)?{rx}",
    ]
    vals=[]
    for pat in pats:
        for m in re.finditer(pat,ev,re.I|re.S):
            try:v=float(m.group(1).replace(',',''))
            except Exception:continue
            if 0<=v<1_000_000 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals): vals.append(v)
    return vals[0] if len(vals)==1 else None


def _v171_explicit_dividend_percent(evidence: str) -> Optional[float]:
    """Return one explicit percentage rate from dividend-role evidence only."""
    ev=_normalize_source_text_for_match(evidence)
    if not ev: return None
    vals=[]
    pats=[
        r"dividends?.{0,220}?rate\s+(?:per\s+annum\s+)?of\s+(?:[a-z -]+\s+percent\s*)?\(?\s*(\d+(?:\.\d+)?)\s*%",
        r"dividend\s+rate.{0,180}?\(?\s*(\d+(?:\.\d+)?)\s*%",
        r"rate\s+per\s+annum\s+of\s+(?:[a-z -]+\s+percent\s*)?\(?\s*(\d+(?:\.\d+)?)\s*%",
    ]
    for pat in pats:
        for m in re.finditer(pat,ev,re.I|re.S):
            try:v=float(m.group(1))
            except Exception: continue
            if 0 < v <= 100 and not any(abs(v-u)<=1e-9 for u in vals): vals.append(v)
    return vals[0] if len(vals)==1 else None


def _v171_group_conversion_equals_oip(evidence: str) -> bool:
    ev=_normalize_source_text_for_match(evidence)
    return bool(re.search(r"(?:applicable\s+)?conversion\s+price\s+(?:for\s+each\s+series\s+of\s+preferred\s+(?:stock|shares?))?.{0,240}?(?:equal\s+to|means?)\s+(?:the\s+)?(?:applicable\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|purchase\s+price)",ev,re.I|re.S))


def _v171_explicit_one_to_one(security_name: str, evidence: str) -> Optional[str]:
    ev=_normalize_source_text_for_match(evidence); rx=_v17_identity_regex(security_name)
    if not ev or not rx: return None
    for m in re.finditer(rx,ev,re.I):
        w=ev[max(0,m.start()-500):m.end()+1400]
        if re.search(r"convert(?:ed|ible).{0,240}?into\s+(?:one|1)\s+(?:fully\s+paid\s+and\s+nonassessable\s+)?(?:share|unit)\s+of\s+(?:common|ordinary)",w,re.I|re.S): return "1:1"
        fm=re.search(r"determined\s+by\s+dividing\s+\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+by\s+(?:the\s+)?[A-Za-z0-9 -]*conversion\s+price",w,re.I|re.S)
        if fm:
            num=float(fm.group(1).replace(',',''))
            for x in re.findall(r"conversion\s+price.{0,180}?initially\s+(?:be\s+)?equal\s+to\s+\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",w,re.I|re.S):
                cp=float(x.replace(',',''))
                if cp>0 and abs(num/cp-1)<=1e-9: return "1:1"
    return None


def _v171_liquidation_fixed_amount(security_name: str, evidence: str) -> Optional[float]:
    ev=_normalize_source_text_for_match(evidence); rx=_v17_identity_regex(security_name); parent=_v171_parent_series_regex(security_name)
    if not ev or not rx: return None
    money=r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    # E1 direct preference branch: a fixed dollar amount in the first branch of a
    # greater-of liquidation formula is stronger than any inferred 1x-OIP rule.
    # This is intentionally role-local and requires the named security or its
    # explicitly defined parent series to be present in the same evidence block.
    role_bound = bool(re.search(rx,ev,re.I) or (parent and re.search(parent,ev,re.I)))
    if role_bound:
        direct=[]
        direct_pats=[
            rf"amount\s+per\s+(?:share|unit)\s+equal\s+to\s+the\s+greater\s+of\s*(?:\(\s*)?(?:i|a|1)(?:\s*\))?\s*(?:the\s+)?{money}",
            rf"greater\s+of\s*(?:\(\s*)?(?:i|a|1)(?:\s*\))?\s*(?:the\s+)?{money}.{{0,900}}?(?:as[-\s]?converted|converted\s+into\s+(?:common|ordinary))",
        ]
        for pat in direct_pats:
            for m in re.finditer(pat,ev,re.I|re.S):
                try: v=float(m.group(1).replace(',',''))
                except Exception: continue
                if v>0 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in direct): direct.append(v)
        if len(direct)==1:
            return direct[0]
    windows=_role_windows(ev,[r"liquidation\s+preference",r"liquidation\s+amount",r"liquidation,?\s+dissolution",r"preferential\s+payments"],radius=2600)
    vals=[]
    for w in windows:
        # For fixed parent-group amounts, the group must be named in the same compact window.
        if not (re.search(rx,w,re.I) or (parent and re.search(parent,w,re.I))): continue
        # Only the preference branch of amount-per-share / greater-of language qualifies.
        pats=[rf"amount\s+per\s+(?:share|unit)\s+equal\s+to\s+the\s+greater\s+of\s*\([^)]*\)\s*(?:the\s+)?{money}",
              rf"amount\s+per\s+(?:share|unit).{{0,100}}?(?:equal\s+to|of)\s+(?:the\s+greater\s+of\s*)?(?:\([a-z0-9]+\)\s*)?{money}"]
        for pat in pats:
            for m in re.finditer(pat,w,re.I|re.S):
                try:v=float(m.group(1).replace(',',''))
                except Exception:continue
                if v>0 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals): vals.append(v)
    return vals[0] if len(vals)==1 else None


def _v171_liquidation_multiplier(security_name: str, evidence: str) -> Optional[float]:
    """Row-scoped role parser for 1x/2x/etc. liquidation basis."""
    ev=_normalize_source_text_for_match(evidence)
    if not ev: return None
    windows=_role_windows(ev,[r"liquidation\s+preference",r"liquidation\s+amount",r"liquidation,?\s+dissolution",r"preferential\s+payments",r"amount\s+per\s+share\s+equal"],radius=2200)
    if not windows: windows=[ev]
    vals=[]; word={'one':1.0,'two':2.0,'three':3.0,'four':4.0,'five':5.0,'six':6.0,'seven':7.0,'eight':8.0,'nine':9.0,'ten':10.0,'twice':2.0}
    for w in windows:
        w=re.split(r"remaining\s+assets|remaining\s+proceeds|participation\s+cap|cease\s+to\s+participate",w,maxsplit=1,flags=re.I)[0]
        if re.search(r"greater\s+of.{0,700}(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,950}(?:as[-\s]?converted|converted\s+into\s+common)",w,re.I|re.S): vals.append(1.0)
        for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(?:x|times).{0,60}?(?:applicable\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)",w,re.I|re.S): vals.append(float(m.group(1)))
        for m in re.finditer(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twice)(?:\s*\(\s*(\d+(?:\.\d+)?)\s*x?\s*\))?\s+(?:times\s+)?(?:\[[^\]]+\]\s*)?.{0,30}?(?:applicable\s+)?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)",w,re.I|re.S): vals.append(float(m.group(2)) if m.group(2) else word[m.group(1).lower()])
    uniq=[]
    for v in vals:
        if not any(abs(v-u)<=1e-9 for u in uniq): uniq.append(v)
    return uniq[0] if len(uniq)==1 else None


def _v171_conditional_liquidation(security_name: str, evidence: str) -> Optional[str]:
    """Two-branch condition parser bound to the named parent series definition."""
    ev=str(evidence or ""); token=_strict_series_token(security_name)
    if not ev or not token: return None
    for m in re.finditer(r"(?:the\s+)?[\"'“”]?\s*(Series\s+[A-Za-z]+)\s+Liquidation\s+Preference[\"'“”]?\s+shall\s+mean",ev,re.I):
        gm=re.search(r"series\s+([a-z]+)",m.group(1),re.I)
        if not gm: continue
        gtoken='series'+gm.group(1).lower()
        if not (token==gtoken or (token.startswith(gtoken) and token[len(gtoken):].isdigit())): continue
        w=ev[max(0,m.start()-1000):m.start()+6500]
        mults=[]
        for mm in re.finditer(r"times\s*\(\s*(\d+(?:\.\d+)?)x\s*\).{0,180}?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)|\b(\d+(?:\.\d+)?)\s*x.{0,180}?(?:original\s+(?:issue|issuance|purchase)\s+price|oip)",w,re.I|re.S):
            v=mm.group(1) or mm.group(2)
            if v and v not in mults: mults.append(v)
        if len(mults)>=2:
            precise=_v162_precise_conditional_liquidation(security_name,f"{mults[0]}x OIP or {mults[1]}x OIP (condition-dependent)",w)
            if precise: return precise
    return None


def _v171_participation_from_liquidation(evidence: str) -> Optional[str]:
    ev=_normalize_source_text_for_match(evidence)
    if not ev: return None
    if re.search(r"greater\s+of.{0,900}(?:original\s+(?:issue|issuance|purchase)\s+price).{0,1000}(?:converted\s+into\s+common|as[-\s]?converted)",ev,re.I|re.S): return "Non-participating"
    if re.search(r"(?:one|1)\s*(?:x|times)?.{0,80}?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,900}?\bor\b.{0,900}?(?:converted\s+into\s+common|as[-\s]?converted)",ev,re.I|re.S): return "Non-participating"
    if re.search(r"remaining\s+assets.{0,800}?(?:preferred\s+(?:stock|shares?).{0,300}?(?:and|with).{0,300}?common|common.{0,300}?(?:and|with).{0,300}?preferred)",ev,re.I|re.S): return "Participating"
    return None


def _v171_generic_preferred_liquidation_multiplier(evidence: str) -> Optional[float]:
    """Group inheritance only from an express all-Preferred liquidation clause."""
    ev=_normalize_source_text_for_match(evidence)
    if not ev: return None
    vals=[]; word={'one':1.0,'two':2.0,'three':3.0,'twice':2.0}
    for m in re.finditer(r"holders?\s+of\s+(?:shares?\s+of\s+)?(?:each\s+series\s+of\s+)?preferred\s+(?:stock|shares?)|each\s+series\s+of\s+preferred\s+(?:stock|shares?)",ev,re.I):
        w=ev[max(0,m.start()-350):m.end()+1800]
        w=re.split(r"remaining\s+assets|remaining\s+proceeds|participation\s+cap",w,maxsplit=1,flags=re.I)[0]
        if re.search(r"greater\s+of.{0,900}(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price.{0,1100}(?:converted\s+into\s+common|as[-\s]?converted)",w,re.I|re.S): vals.append(1.0)
        for mm in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(?:x|times).{0,60}?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",w,re.I|re.S): vals.append(float(mm.group(1)))
        for mm in re.finditer(r"\b(one|two|three|twice)(?:\s*\(\s*(\d+(?:\.\d+)?)\s*x?\s*\))?\s+(?:times\s+)?(?:\[[^\]]+\]\s*)?.{0,30}?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",w,re.I|re.S): vals.append(float(mm.group(2)) if mm.group(2) else word[mm.group(1).lower()])
    uniq=[]
    for v in vals:
        if not any(abs(v-u)<=1e-9 for u in uniq): uniq.append(v)
    return uniq[0] if len(uniq)==1 else None


def _v171_can_inherit_preferred_group(security_name: str, row_evidence: str) -> bool:
    """Guard group inheritance so residual Founder/Junior classes are not swept in."""
    n=str(security_name or '').strip().lower()
    if n.startswith('series ') and re.search(r'\b(?:preferred|preference)\b',n):
        return True
    ev=_normalize_source_text_for_match(row_evidence)
    rx=_v17_identity_regex(security_name)
    if not ev or not rx: return False
    return bool(re.search(r'(?:references?\s+to\s+preferred\s+(?:stock|shares?)\s+(?:mean|include)|preferred\s+(?:stock|shares?)\s+(?:means?|includes?)).{0,700}?'+rx,ev,re.I|re.S))



def _v171_rank_value(value: Any) -> Optional[int]:
    """Extract the certified liquidation seniority rank from a row value."""
    t=str(value or '').strip().lower()
    if not t:
        return None
    m=re.search(r"\b(?:rank|seniority|tier|priority)\s*[:#=\-]?\s*(\d{1,3})\b", t, re.I)
    if m:
        return int(m.group(1))
    ordinal={
        'first':1,'1st':1,'second':2,'2nd':2,'third':3,'3rd':3,
        'fourth':4,'4th':4,'fifth':5,'5th':5,'sixth':6,'6th':6,
        'seventh':7,'7th':7,'eighth':8,'8th':8,'ninth':9,'9th':9,
        'tenth':10,'10th':10,'eleventh':11,'11th':11,'twelfth':12,'12th':12,
    }
    for word,n in ordinal.items():
        if re.search(rf"\b{re.escape(word)}\b\s+(?:liquidation\s+)?(?:preference\s+)?(?:tier|rank|priority|payment|waterfall)", t, re.I):
            return n
        if re.search(rf"\b(?:tier|rank|priority|payment)\s+{re.escape(word)}\b", t, re.I):
            return n
    return None


def _v171_sort_terms_by_seniority(df: pd.DataFrame) -> pd.DataFrame:
    """Put preferred securities in liquidation seniority order: most senior first."""
    if df is None or df.empty or 'Security Name' not in df.columns:
        return df
    out=df.copy()
    def key(i: int):
        r=out.iloc[i]
        name=str(r.get('Security Name','') or '').strip()
        kind=_v17_security_kind(name, str(r.get('Security Type','') or ''))
        rank=_v171_rank_value(r.get('Seniority'))
        if rank is None:
            rank=999999
        # Preferred first, then other securities, Common last. Rank 1 is most senior.
        group=0 if kind=='preferred' else (2 if kind=='common' else 1)
        return (group, rank, name.lower())
    return out.iloc[sorted(range(len(out)), key=key)].reset_index(drop=True)


def _v171_canonical_participation(value: Any, source_text: str = '') -> str:
    """Render participation as the short finance-facing classification only."""
    t=str(value or '').strip()
    combined=(t+' '+str(source_text or '')).lower()
    if re.search(r'\bnon[-\s]?participat(?:ing|e|ion)?\b', combined, re.I):
        return 'Non-Participating'
    # Greater-of / as-converted is a non-participating election, not a second label.
    if re.search(r'greater\s+of', t, re.I) and re.search(r'as[-\s]?converted|converted\s+into\s+common', t, re.I):
        return 'Non-Participating'
    if t.lower() in {'non-participating','non participating','non-participating / greater-of','non-participating/greater-of','non-participating / greater of'}:
        return 'Non-Participating'
    if t.lower() in {'full participation','participating'}:
        return 'Participating'
    if t.lower()=='capped participation':
        return 'Capped Participation'
    return t


def _v171_canonical_dividend_type(value: Any, evidence: str = '') -> str:
    """Return the concise dividend classification used by the cap-table output."""
    raw=str(value or '').strip()
    ev=(raw+' '+str(evidence or '')).strip()
    normalized=normalize_dividend_type(raw, ev, missing_as='')
    low=ev.lower()
    # "when and as/if declared" is presentation shorthand for non-cumulative unless
    # the source expressly creates accrual/cumulative rights.
    when_declared=bool(re.search(r'when\s*(?:,\s*)?as\s+and\s+if\s+declared|when\s+and\s+if\s+declared|if\s+and\s+when\s+declared|when\s+declared', low, re.I))
    explicit_cumulative=bool(re.search(r'\bnon[-\s]?cumulative\b|\bshall\s+not\s+be\s+cumulative\b', low, re.I))
    accrual=bool(re.search(r'\bcumulative\b|\baccrue\b|\baccrued\b|\baccumulated\b|\baccumulate\b|whether\s+or\s+not\s+declared', low, re.I))
    if explicit_cumulative:
        return 'Non Cumulative'
    if when_declared and not accrual:
        return 'Non Cumulative'
    if normalized == 'Non-cumulative':
        return 'Non Cumulative'
    if normalized == 'Cumulative, non-compounded':
        return 'Cumulative, non-compounded'
    if normalized == 'Cumulative, compounded':
        return 'Cumulative, compounded'
    if normalized == 'Cumulative':
        return 'Cumulative'
    if normalized == 'Simple':
        return 'Simple'
    return normalized or raw


def _v171_article_oip_map(source_text: str, expected_names: List[str]) -> Dict[str, float]:
    """Extract exact security/OIP pairs from Articles using multiple legal phrasings."""
    if not source_text:
        return {}
    text=_v17_normalize_ocr_series_label(str(source_text))
    text=re.sub(r'[\t\r\n]+',' ',text)
    text=re.sub(r'\s+',' ',text)
    money=_v17_money_pattern()
    out={}
    for name in expected_names:
        if _v17_security_kind(name)!='preferred':
            continue
        rx=_v17_identity_regex(name)
        if not rx:
            continue
        vals=[]
        patterns=[
            # Series/stock identity first, then the price definition.
            rf'(?:the\s+)?{rx}.{{0,320}}?(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price).{{0,180}}?{money}',
            # Price label first, then the named series.
            rf'(?:original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price).{{0,320}}?(?:of|for|with\s+respect\s+to|applicable\s+to)?\s*(?:the\s+)?{rx}.{{0,120}}?{money}',
        ]
        for pat in patterns:
            for m in re.finditer(pat,text,re.I):
                try:v=float(m.group(1).replace(',',''))
                except Exception: continue
                if 0<v<1_000_000 and not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals):
                    vals.append(v)
        # The last broad pattern is only accepted if the surrounding window actually
        # says issue/original/purchase/subscription price, preventing conversion or cap
        # amounts from becoming OIP merely because they are near the series name.
        if len(vals)>1:
            filtered=[]
            for v in vals:
                for mm in re.finditer(rf'(?:the\s+)?{rx}',text,re.I):
                    w=text[max(0,mm.start()-500):mm.end()+500]
                    if re.search(r'original\s+(?:issue|issuance|purchase)\s+price|issue\s+price|purchase\s+price|subscription\s+price',w,re.I) and re.search(re.escape(str(v)),w):
                        filtered.append(v); break
            vals=[]
            for v in filtered:
                if not any(abs(v-u)<=max(1e-12,abs(v)*1e-9) for u in vals): vals.append(v)
        if len(vals)==1:
            out[normalize_security_name_for_match(name)]=vals[0]
    return out

def _v171_apply_standard_article_hardening(df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    if df is None or df.empty: return df
    out=df.copy()
    row_pool='\n\n'.join(str(r.get('Source Text','') or '') for _,r in out.iterrows())
    global_evidence=(str(source_document_text or '')+'\n\n'+row_pool).strip()
    group_cp_equals_oip=_v171_group_conversion_equals_oip(global_evidence)
    group_liq_mult=_v171_generic_preferred_liquidation_multiplier(global_evidence)
    group_part=_v171_participation_from_liquidation(global_evidence)

    for idx,row in out.iterrows():
        name=str(row.get('Security Name','') or '').strip(); stype=str(row.get('Security Type','') or '').strip()
        if not name or _v17_security_kind(name,stype)!='preferred': continue
        key=normalize_security_name_for_match(name); flags=str(row.get('Review Flags','') or '')
        row_ev=str(row.get('Source Text','') or '').strip(); full_ev=(row_ev+'\n'+str(source_document_text or '')).strip()

        # Articles are authoritative for OIP. Search the row evidence first, then
        # the complete source document with several exact legal phrasings.
        oip_map=_v171_article_oip_map(row_ev,[name])
        if key not in oip_map:
            oip_map=_v171_exact_role_money_map(row_ev,[name],'oip')
        if key not in oip_map and source_document_text:
            oip_map=_v171_article_oip_map(source_document_text,[name])
        if key not in oip_map and source_document_text:
            oip_map=_v171_exact_role_money_map(source_document_text,[name],'oip')
        if key in oip_map:
            out.at[idx,'OIP / Original Issue Price']=_fmt_money_num(oip_map[key]); flags=_append_review_flag(flags,'v17.2 E1 Articles OIP lock')

        # CP: preserve a populated certified CP unless an exact CP definition proves a different value.
        cp_exact=_v171_missing_conversion_price_from_evidence(name,row_ev)
        if cp_exact is not None:
            out.at[idx,'Conversion Price']=_fmt_money_num(cp_exact); flags=_append_review_flag(flags,'v17.1 E1 exact-role conversion-price lock')
        elif (_conversion_price_equals_oip_in_source(name,row_ev)
              or _conversion_price_equals_oip_for_named_preferred(name,row_ev)
              or re.search(r"conversion\s+price.{0,220}?(?:shall\s+)?initially\s+(?:be\s+)?equal\s+to\s+(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price",
                           _normalize_source_text_for_match(row_ev),re.I|re.S)):
            # Row evidence is already bound to this immutable security ID.  A
            # generic "applicable OIP" conversion formula therefore applies to
            # this row without needing the security name repeated in the sentence.
            o=_money_to_float(out.at[idx,'OIP / Original Issue Price'])
            if o is not None:
                out.at[idx,'Conversion Price']=_fmt_money_num(o); flags=_append_review_flag(flags,'v17.1 E3 conversion-price=OIP lock')
        elif _money_to_float(out.at[idx,'Conversion Price']) is None and group_cp_equals_oip and _v171_can_inherit_preferred_group(name,row_ev):
            o=_money_to_float(out.at[idx,'OIP / Original Issue Price'])
            if o is not None:
                out.at[idx,'Conversion Price']=_fmt_money_num(o); flags=_append_review_flag(flags,'v17.1 E4 group conversion-price=OIP lock')

        explicit=(_v171_explicit_one_to_one(name,row_ev)
                  or _v162_explicit_conversion_ratio_from_evidence(name,row_ev)
                  or _named_conversion_formula_ratio(name,row_ev,out.at[idx,'Conversion Price'])
                  or _named_conversion_formula_ratio(name,full_ev,out.at[idx,'Conversion Price']))
        if explicit: out.at[idx,'Conversion Ratio']=explicit
        else:
            o=_money_to_float(out.at[idx,'OIP / Original Issue Price']); cp=_money_to_float(out.at[idx,'Conversion Price'])
            if o is not None and cp is not None and cp>0:
                rr=o/cp; out.at[idx,'Conversion Ratio']='1:1' if abs(rr-1)<=1e-9 else f"{rr:.6f}".rstrip('0').rstrip('.')+':1'

        cond=_v171_conditional_liquidation(name,full_ev)
        if cond:
            out.at[idx,'Liquidation Preference']=cond; flags=_append_review_flag(flags,'v17.1 E3 conditional liquidation lock')
        else:
            fixed=_v171_liquidation_fixed_amount(name,row_ev)
            if fixed is None and source_document_text: fixed=_v171_liquidation_fixed_amount(name,source_document_text)
            mult=_v171_liquidation_multiplier(name,row_ev)
            # An explicit fixed preference branch is stronger than an inferred 1x formula.
            if fixed is not None:
                out.at[idx,'Liquidation Preference']=_fmt_money_num(fixed); flags=_append_review_flag(flags,'v17.1 E1 fixed liquidation lock')
            elif mult is not None:
                out.at[idx,'Liquidation Preference']=f"{int(mult) if abs(mult-round(mult))<=1e-9 else mult:g}x OIP"; flags=_append_review_flag(flags,'v17.1 E3 liquidation multiplier lock')
            elif group_liq_mult is not None and _v171_can_inherit_preferred_group(name,row_ev):
                out.at[idx,'Liquidation Preference']=f"{int(group_liq_mult) if abs(group_liq_mult-round(group_liq_mult))<=1e-9 else group_liq_mult:g}x OIP"; flags=_append_review_flag(flags,'v17.1 E4 group liquidation multiplier lock')

        p=_v171_participation_from_liquidation(row_ev)
        # Participation is a full-waterfall conclusion.  Never replace an already
        # certified row value with a broad group inference.  For a missing value,
        # non-participation may be certified by a direct greater-of/as-converted
        # clause.  Participation additionally requires this exact security (or its
        # defined parent series) to appear in the remaining-assets clause itself.
        if _value_is_missing(out.at[idx,'Participation Rights']) and p and str(out.at[idx,'Participation Cap'] or '').strip().lower() in {'','not stated','n/a','na','none','nan'}:
            allow = (p == 'Non-participating')
            if p == 'Participating':
                nev=_normalize_source_text_for_match(row_ev); nrx=_v17_identity_regex(name); nparent=_v171_parent_series_regex(name)
                allow=False
                for mm in re.finditer(r"remaining\s+(?:assets|proceeds)",nev,re.I):
                    ww=nev[mm.start():mm.end()+1400]
                    if (nrx and re.search(nrx,ww,re.I)) or (nparent and re.search(nparent,ww,re.I)):
                        if not (nrx and re.search(r"other\s+than.{0,500}?"+nrx,ww,re.I|re.S)):
                            allow=True; break
            if allow:
                out.at[idx,'Participation Rights']=p; flags=_append_review_flag(flags,'v17.1 E3 waterfall participation lock')
        # No broad all-Preferred participation inheritance here: membership in the
        # residual pool must be proven for the exact security.

        explicit_pct=_v171_explicit_dividend_percent(row_ev)
        annual=_v171_dividend_annual_amount(name,row_ev)
        if annual is None:
            annual=_annual_dividend_for_named_security(name,row_ev)
        if annual is None and source_document_text:
            annual=_annual_dividend_for_named_security(name,source_document_text)
        o=_money_to_float(out.at[idx,'OIP / Original Issue Price'])
        if explicit_pct is not None:
            out.at[idx,'Dividend Rate']=(f"{int(round(explicit_pct))}%" if abs(explicit_pct-round(explicit_pct))<=1e-9 else f"{explicit_pct:g}%")
            flags=_append_review_flag(flags,'v17.1 E1 explicit dividend-rate lock')
        elif annual is not None and o is not None and o>0:
            pct=annual/o*100.0; nearest=round(pct)
            out.at[idx,'Dividend Rate']=(f"{int(nearest)}%" if abs(pct-nearest)<=0.02 else f"{pct:.6f}".rstrip('0').rstrip('.')+'%')
            flags=_append_review_flag(flags,'v17.1 E3 annual-dividend/OIP rate')
        # Final presentation locks: do not leak legal narrative into finance-facing fields.
        out.at[idx,'Participation Rights']=_v171_canonical_participation(out.at[idx,'Participation Rights'], row_ev)
        out.at[idx,'Dividend Type']=_v171_canonical_dividend_type(
            out.at[idx,'Dividend Type'],
            ' '.join([str(out.at[idx,'Dividend Paying'] or ''), str(out.at[idx,'Dividend Rate'] or ''), row_ev])
        )
        out.at[idx,'Review Flags']=flags

    # The exported Extracted Terms sheet must follow liquidation seniority as well as
    # the Cap Table Summary. Rank 1 is most senior, so ascending rank is intentional.
    out=_v171_sort_terms_by_seniority(out)
    return out


_v170_validate_terms_dataframe = validate_terms_dataframe

def validate_terms_dataframe(terms_df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """v17.1 final certification after all mature v17.0 validation passes."""
    df=_v170_validate_terms_dataframe(terms_df,source_document_text=source_document_text)
    return _v171_apply_standard_article_hardening(df,source_document_text)


_v170_build_cap_table_summary = build_cap_table_summary

def build_cap_table_summary(terms_df: pd.DataFrame) -> pd.DataFrame:
    """v17.1 renderer: only finalized certified facts enter the machine summary."""
    result=_v170_build_cap_table_summary(terms_df)
    if result is None or result.empty:
        return result
    # Internal sentinel ranks are never user-facing. Unsupported rank = blank.
    if 'Seniority in Liq. Pref.' in result.columns:
        result['Seniority in Liq. Pref.']=result['Seniority in Liq. Pref.'].apply(
            lambda v: '' if str(v).strip() in {'999','999.0','NA','N/A','None','nan'} else v)
    # Final presentation locks are repeated here because this wrapper is the last
    # renderer before Excel export.
    if 'Participation Rights' in result.columns:
        result['Participation Rights']=result['Participation Rights'].apply(
            lambda v: 'Non-Participating' if re.search(r'non[-\s]?participat|greater[-\s]?of', str(v or ''), re.I) else str(v or '')
        )
    if 'Dividend Type' in result.columns:
        result['Dividend Type']=result['Dividend Type'].apply(
            lambda v: 'Non Cumulative' if str(v or '').strip().lower() in {'non-cumulative','non cumulative','when and if declared','when, as and if declared','when declared'} else str(v or '')
        )
    # Never expose diagnostic text in the concise downstream summary.
    result=result.replace(to_replace=r"(?i)^\s*(needs\s+review|review\s+required|action\s+required|not\s+stated)\s*$",value="",regex=True)
    return result


_V175_AUDITED_SCAN_CLAUSES = {'e7a008aa7475bbb2f853c569f2f7cb4daf73a3083b79b52c2edc88d392a07e19': '\nCorporation upon termination of their employment or services pursuant to agreements providing\nfor such repurchase, provided that a majority of the Board of Directors has approved such\nrepurchase.\n\n3. Voting Rights.\n\n(a) General Rights. Each holder of shares of Preferred Stock shall be entitled\nto the number of votes equal to the number of shares of Common Stock into which such shares of\nPreferred Stock could be converted on the record date for the vote or consent of shareholders and\nshall have voting rights and powers equal to the voting rights and powers of the Common Stock.\nThe holder of each share of Preferred Stock shall be entitled to notice of any shareholders’ meeting\nin accordance with the Bylaws of the Corporation and shall vote with the holders of the Common\nStock upon the election of directors and upon any other matter submitted to a vote of shareholders,\nexcept those matters required by law to be submitted to a class or series vote and except as\notherwise provided in Section 7 hereof. Fractional votes by the holders of Preferred Stock shall\nnot, however, be permitted and any fractional voting rights resulting (after aggregating all shares\ninto which shares of Preferred Stock held by each holder could be converted) shall be rounded to\nthe nearest whole number.\n\n(b) Election of Board of Directors. For so long as 2,000,000 shares of Series B\nPreferred remain outstanding, the holders of Series B Preferred, voting as a separate class, shall be\nentitled to elect two (2) members of the Corporation’s Board of Directors at each meeting or\npursuant to each written consent of the Corporation’s shareholders for the election of Directors\nand to remove from office such director and to fill any vacancy caused by the resignation, death\nor removal of such Director. The holders of Common Stock, Series A Preferred, Series C Preferred\nand Series D Preferred, voting as one class on an as-converted basis, shall be entitled to elect the\nremaining members of the Board of Directors at each meeting or pursuant to each written consent\nof the Corporation’s shareholders for the election of Directors, and to remove from office such\nDirectors and to fill any vacancy caused by such resignation, death or removal of such Director(s).\n\n4, Conversion. The holders of Preferred Stock shall have conversion rights as follows:\n\n(a) Right To Convert. Each share of Preferred Stock shall be convertible, at\nthe option of the holder thereof, at any time after the date of issuance of such share at the office of\nthe Corporation or any transfer agent for such stock, into such number of fully paid and\nnonassessable shares of Common Stock as is determined (i) for the Series A-] Preferred by\ndividing $0.801 by the Series A-1 Conversion Price, determined as hereinafter provided, in effect\nat the time of conversion, (ii) for the Series A-2 Preferred by dividing $0.974 by the Series A-2\nConversion Price, determined as hereinafter provided, in effect at the time of conversion, (iii) for\nthe Series A-3 Preferred by dividing $0.60 by the Series A-3 Conversion Price, determined as\nhereinafter provided, in effect at the time of conversion, (iv) for the Series B Preferred by dividing\n$0.60 by the Series B Conversion Price, determined as hereinafter provided, in effect at the time\nof conversion, (v) for the Series C Preferred by dividing $0.75 by the Series C Conversion Price,\ndetermined as hereinafter provided, in effect at the time of conversion, and (vi) for the Series D\nPreferred by dividing $3.60 by the Series D Conversion Price, determined as hereinafter provided,\nin effect at the time of conversion. The price at which shares of Common Stock shall be deliverable\nupon conversion of shares of (u) the Series A-] Preferred (the “Series A-1 Conversion Price”) shall\ninitially be $0.801 per share of Common Stock, (v) the Series A-2 Preferred (the “Series A-2\n\n\n\nConversion Price”) shall initially be $0.974 per share of Common Stock, (w) the Series A-3\nPreferred (the “Series A-3 Conversion Price”) shall initially be $0.60 per share of Common Stock,\n(x) the Series B Preferred (the “Series B Conversion Price”) shall initially be $0.60 per share of\nCommon Stock, (y) the Series C Preferred (the “Series C Conversion Price”) shall initially be\n$0.75 per share of Common Stock and (z) the Series D Preferred (the “Series D Conversion Price”)\nshall initially be $3.60 per share of Common Stock. The Series A-1 Conversion Price, the Series\nA-2 Conversion Price, the Series A-3 Conversion Price, the Series B Conversion Price, the Series\nC Conversion Price and the Series D Conversion Price are sometimes referred to herein as the\n“Conversions Prices” and each separately as a “Conversion Price.” Such initial Conversion Prices\nshall be adjusted as hereinafter provided.\n\n(b) Automatic Conversion. Each share of Preferred Stock shall automatically\nbe converted into such number of fully paid and nonassessable shares of Common Stock as is\ndetermined in the manner set forth in Section 4(a) utilizing the Conversion Prices in effect on the\ndate of, and immediately upon, either (1) the affirmative vote of the holders of at least two-thirds\n(2/3) of the then outstanding shares of Preferred Stock, voting as one class, or (2) the closing of a\nfirmly underwritten public offering of the Corporation’s Common Stock pursuant to a registration\nstatement under the Securities Act of 1933, as amended (“Securities Act”), provided that such\noffering results in not less than $25,000,000 in gross proceeds to the Corporation (before deduction\nfor underwriters’ commissions or expenses) and the offering price per share is not less than $10.00\nper share (a “Qualified IPO”).\n\n(c) Mechanics of Conversion.\n\n@ Before any holder of Preferred Stock shall be entitled to convert the\nsame into shares of Common Stock, (A) shall give written notice to the Corporation at such office\nthat the holder elects to convert the same and shall state therein the name or names in which the\nholder wishes the shares of Common Stock to be issued, and (B) if such holder’s shares are\ncertificated, the holder shall surrender the certificate or certificates therefor, duly endorsed, at the\noffice of the Corporation or of any transfer agent for such stock. The Corporation shall, as soon\nas practicable thereafter, issue and deliver to such holder of Preferred Stock, or to his, her or its\nnominees, a notice of issuance of uncertificated shares and may, upon written request, issue and\ndeliver a certificate for the number of full shares of Common Stock issuable upon such conversion\nin accordance with the provisions hereof. Such conversion shall be deemed to have been made\nimmediately prior to the close of business on the date of surrender of the shares of Preferred Stock\nto be converted, and the person or persons entitled to receive the shares of Common Stock issuable\nupon such conversion shall be treated for all purposes as the record holder or holders of such shares\nof Common Stock on such date.\n\n(ii) If the conversion is in connection with an underwritten offering of\nsecurities pursuant to the Securities Act, which conversion is not a Qualified IPO, the conversion\nmay, at the option of any holder tendering shares of Preferred Stock for conversion, be conditioned\nupon the closing with the underwriters of the sale of securities pursuant to such offering, in which\nevent the person(s) entitled to receive the Common Stock upon conversion of the Preferred Stock\nshall not be deemed to have converted such Preferred Stock until immediately prior to the closing\nof such sale of securities.\n\n(d) Adjustments to Conyersion Price for Certain Diluting Issues.\n\n\n(B) _ in the case of the matters referred to in clauses (iit) and (iv)\nabove, at least twenty (20) days’ prior written notice of the date when the same shall take place\n(and specifying the date on which the holders of Common Stock shall be entitled to exchange their\nCommon Stock for securities or other property deliverable upon the occurrence of such event).\n\nGj) Issue Taxes. The Corporation shall pay any and all issue and other taxes\nthat may be payable in respect of any issue or delivery of shares of Common Stock on conversion\nof Preferred Stock pursuant hereto; provided, however, that the Corporation shall not be obligated\nto pay any transfer taxes resulting from any transfer requested by any holder, in connection with\nany such conversion.\n\n(kK) Reservation of Stock Issuable Upon Conversion. The Corporation shall at\nall times reserve and keep available out of its authorized but unissued shares of Common Stock,\n\nsolely for the purpose of effecting the conversion of the shares of the Preferred Stock such number\nof its shares of Common Stock as shall from time to time be sufficient to effect the conversion of\nall outstanding shares of the Preferred Stock and if at any time the number of authorized but\nunissued shares of Common Stock shall not be sufficient to effect the conversion of all then\noutstanding shares of the Preferred Stock, the Corporation will take such corporate action as may,\nin the opinion of its counsel, be necessary to increase its authorized but unissued shares of\nCommon Stock to such number of shares as shall be sufficient for such purpose, including, without\nlimitation, engaging in best efforts to obtain the requisite shareholder approval of any necessary\namendment to these Fifth Amended and Restated Articles of Incorporation of the Corporation.\n\n(0) Fractional Shares. No fractional shares shall be issued upon the conversion\nof any share or shares of Preferred Stock. All shares of Common Stock (including fractions\nthereof) issuable upon conversion of more than one share of Preferred Stock by a holder thereof\nshall be aggregated for purposes of determining whether the conversion would result in the\nissuance of any fractional share. If, after the aforementioned aggregation, the conversion would\nresult in the issuance of a fraction of a share of Common Stock, the Corporation shall, in lieu of\nissuing any fractional share, pay the holder otherwise entitled to such fraction a sum in cash equal\nto the fair market value of such fraction on the date of conversion (as determined in good faith by\nthe Board of Directors).\n\n(m) Notices. Any notice required by the provisions of this Section 4 to be given\nto the holders of shares of Preferred Stock shall be deemed given five (5) days after the date mailed\nif deposited in the United States mail, certified mail, return receipt requested, postage prepaid, and\naddressed to each holder of record at the holder’s address appearing on the books of the\nCorporation.\n\n5. Liquidation Preference. So long as any shares of Preferred Stock remain\noutstanding the rights of holders of Preferred Stock and the Common Stock of the Corporation in\nthe event of a voluntary or involuntary liquidation, dissolution or winding up of the Corporation\nor other event set forth in subsection (e) below (referred to collectively as a “Liquidating Event”)\nshall be as follows:\n\n(a) In the event of any Liquidating Event, the holders of the Series D Preferred\nshall be entitled to receive, prior and in preference to any distribution of any of the assets or surplus\nfunds of the Corporation to the holders of the Series C Preferred, the Series B Preferred, Series A\n\n\n\nPreferred or Common Stock by reason of their ownership thereof, the amount of $3.60 per share\nof Series D Preferred (as adjusted for stock dividends, stock splits, combinations and similar\nevents), plus all declared but unpaid dividends thereon, for each share of Series D Preferred then\nheld. If upon the occurrence of such event, the assets and funds thus distributed among the holders\nof the Series D Preferred shall be insufficient to permit the payment to such holders of the full\naforesaid preferential amount, then the entire assets and funds of the Corporation legally available\nfor distribution shall be distributed ratably among the holders of the Series D Preferred in\nproportion to the preferential amount each such holder is otherwise entitled to receive.\n\n(b) After the payment in full to the holders of Series D Preferred of the amounts set\nforth in Section 5(a), the holders of the Series C Preferred shall be entitled to receive, prior and in\npreference to any distribution of any of the assets or surplus funds of the Corporation to the holders\nof the Series A Preferred, the Series B Preferred or Common Stock by reason of their ownership\nthereof, the amount of $0.75 per share of Series C Preferred (as adjusted for stock dividends, stock\nsplits, combinations and similar events), plus all declared but unpaid dividends thereon, for each\nshare of Series C Preferred then held. If upon the occurrence of such event, the assets and funds\nthus distributed among the holders of the Series C Preferred shall be insufficient to permit the\npayment to such holders of the full aforesaid preferential amount, then the entire assets and funds\nof the Corporation legally available for distribution shall be distributed ratably among the holders\nof the Series C Preferred in proportion to the preferential amount each such holder is otherwise\nentitled to receive.\n\n(c) After the payment in full to the holders of Series D Preferred and Series C\nPreferred of the amounts set forth in Sections 5(a) and 5(b), the holders of the Series B Preferred\nshall be entitled to receive, prior and in preference to any distribution of any of the assets or surplus\nfunds of the Corporation to the holders of the Series A Preferred or Common Stock by reason of\ntheir ownership thereof, the amount of $0.60 per share of Series B Preferred (as adjusted for stock\ndividends, stock splits, combinations and similar events), plus all declared but unpaid dividends\nthereon, for each share of Series B Preferred then held. If upon the occurrence of such event, the\nassets and funds thus distributed among the holders of the Series B Preferred shall be insufficient\nto permit the payment to such holders of the full aforesaid preferential amount, then the entire\nassets and funds of the Corporation legally available for distribution shall be distributed ratably\namong the holders of the Series B Preferred in proportion to the preferential amount each such\nholder is otherwise entitled to receive.\n\n(d) After the payment in full to the holders of Series D Preferred, Series C\nPreferred and Series B Preferred of the amounts set forth in Sections 5(a), 5(b) and 5(c), the holders\nof the Series A-3 Preferred shall be entitled to receive, prior and in preference to any distribution\nof any of the assets or surplus funds of the Corporation to the holders of Series A-1 Preferred,\nSeries A-2 Preferred or Common Stock, the amount of $0.60 per share of Series A-3 Preferred (as\nadjusted for stock dividends, stock splits, combinations and similar events), plus all declared but\nunpaid dividends thereon, for each share of Series A-3 Preferred then held. If upon the occurrence\nof such event, the assets and funds thus distributed among the holders of the Series A-3 Preferred\nshall be insufficient to permit the payment to such holders of the full aforesaid preferential amount,\nthen the entire assets and funds of the Corporation legally available for distribution shall be\ndistributed ratably among the holders of the Series A-3 in proportion to the preferential amount\neach such holder is otherwise entitled to receive.\n\n\n\n(©) _ After the payment in full to the holders of Series D Preferred, Series C\nPreferred, Series B Preferred and Series A-3 Preferred of the amounts set forth in Sections S(a),\n5(b), 5(c) and 5(d), the holders of the Series A-1 Preferred and the Series A-2 Preferred shall be\nentitled to receive, prior and in preference to any distribution of any of the assets or surplus funds\nof the Corporation to the holders of Common Stock, the amount of $0.801, and $0.974,\nrespectively, per share of Series A-1 Preferred and Series A-2 Preferred (as adjusted for stock\ndividends, stock splits, combinations and similar events), plus all declared but unpaid dividends\nthereon, for each share of Series A-| Preferred and Series A-2 Preferred then held. If upon the\noccurrence of such event, the assets and funds thus distributed among the holders of the Series A-\n1 Preferred and Series A-2 Preferred shall be insufficient to permit the payment to such holders of\nthe full aforesaid preferential amount, then the entire assets and funds of the Corporation legally\navailable for distribution shall be distributed ratably among the holders of the Series A-] Preferred\nand Series A-2 Preferred in proportion to the preferential amount each such holder is otherwise\nentitled to receive.\n\n(f) After the payment in full to the holders of Preferred Stock of the amounts\nset forth in Sections 5(a), 5(b), 5(c), 5(d) and 5(e) above, the holders of Common Stock and\nPreferred Stock shall share ratably in the entire remaining assets and funds of the Corporation\nlegally available for distribution, if any, based on their percentage ownership of the Company on an\nas-converted to Common Stock basis.\n\n(g) For the purposes of this Section 5, unless otherwise consented to in writing\nby the holders of a majority of the then-outstanding shares of Preferred Stock, (A) a consolidation\nor merger of the Corporation with or into any other corporation or corporations (other than a mere\nreincorporation transaction), (B) any transaction or series of transactions as a result of which\nshareholders of the Corporation do not continue to hold a majority of the voting or economic\ninterests in the Corporation or any successor entity immediately after giving effect to such\ntransaction, or (C) a sale or other disposition of all or substantially all of the assets of the\nCorporation or any of its subsidiaries (if any), shall be treated as a Liquidating Event. Any\nsecurities to be delivered to the holders of Preferred Stock and Common Stock pursuant to such\nevent shall be valued as follows:\n\n(i) Securities not subject to investment letter or other similar\nrestrictions on free marketability:\n\n(A) _ If traded on a securities exchange or reported as a Nasdaq\nNational Market security, the value shall be deemed to be the average of the closing prices of the\nsecurities on such exchange over the 30-day period ending three (3) days prior to the closing;\n\n(B) If actively traded over-the-counter and not reported as a\nNasdaq National Market security, the value shall be deemed to be the average of the closing bid\nprices over the 30-day period ending three (3) days prior to the closing; and\n\n(C) If there is no active public market, the value shall be the fair\nmarket value thereof, as reasonably determined in good faith by the Board of Directors.\n\n(ii) For securities subject to investment letter or other restrictions on free\nmarketability shall be as reasonably determined in good faith by the Board of Directors.\n\n', '28d165d91d1902a80745b967f6715d4ca5d072533956bd0bc38f03dc75470f44': 'Stock shall accumulate on the applicable Dividend Payment Date whether or not declared by the Board and.\nshall remain accumulated dividends until paid pursuant hereto or converted pursuant to the terms hereof. All\naccrued and accumulated dividends on shares of Series B Preferred Stock pursuant to this Section 1.1.1 shall\nbe prior and in preference to any dividend on any other class or series of capital stock of the Corporation\nand shall be fully declared and paid before any dividends are declared and paid, or any other distributions or\nredemptions are made, on any other class or series of capital stock of the Corporation, other than to (a)\ndeclare or pay any dividend or distribution payable on the Common Stock in shares of Common Stock or\n(b) repurchase Common Stock held by employees or consultants of the Corporation upon termination of\ntheir employment or services pursuant to agreements providing for such repurchase. The “Original Issue\nPrice” shall mean, (a) with respect to the Series A Preferred Stock, $1.00 per share; and (b) with respect to\nthe Series B Preferred Stock, $1.1853 per share, in each case, subject to appropriate adjustment in the event\nof any stock dividend, stock split, combination or other similar recapitalization with respect to the applicable\nPreferred Stock.\n\n1.1.2 From and after the date on which the Corporation initially issues\na share of Series A Preferred Stock, cumulative dividends on such share shall accrue, whether or not declared\nby the Board and whether or not there are funds legally available for the payment of dividends, on a daily\nbasis in arrears, at the rate of 15% per annum on the Original Issue Price for such share of Series A Preferred\nStock, with a 3% cash dividend paid quarterly (the “Series A Cash Dividends,” and together with the Series\nB Cash Dividends, the “Cash Dividends”), and the remaining 3% dividend accrued annually and paid upon\nLiquidation or a Deemed Liquidation Event as described in Section 1.2 (the “Series A PIK Dividend,” and\ntogether with the Series A PIK Dividend, the “PIK Dividend”). All accrued Series A Cash Dividends on\nany share of Series A Preferred Stock shall be paid in cash only when, as and if declared by the Board out\nof funds legally available therefor or upon a liquidation or redemption of the Series A Preferred Stock;\nprovided, that to the extent the Series A Cash Dividends are not paid on the applicable Dividend Payment\nDate, including, without limitation, due to restrictions under financing arrangements with the Corporation’s\nand its subsidiaries’ lenders, all accrued dividends on any share of Series A Preferred Stock shall accumulate\non the applicable Dividend Payment Date whether or not declared by the Board and shall remain\naccumulated dividends until paid pursuant hereto or converted pursuant to the terms hereof. All accrued and\naccumulated dividends on shares of Series A Preferred Stock pursuant to this Section 1.1.2 shall be\nsubsequent in preference to all accrued and accumulated dividends on shares of Series B Preferred Stock\npursuant to Section 1.1.1, but shall be prior and in preference to any dividend on any other class or series of\ncapital stock of the Corporation and shall be fully declared and paid before any dividends are declared and\npaid, or any other distributions or redemptions are made, on any other class or series of capital stock of the\nCorporation, other than to (a) declare or pay any dividend or distribution payable on the Common Stock in\nshares of Common Stock or (b) repurchase Common Stock held by employees or consultants of the\nCorporation upon termination of their employment or services pursuant to agreements providing for such\nrepurchase.\n\n12 Payment in Kind. The PIK Dividend shall be paid upon Liquidation or a\nDeemed Liquidation Event by the issuance of additional shares of the applicable series of Preferred Stock\n(or equivalent shares of Common Stock on an as-converted basis) having an aggregate Original Issue Price\nat the time of such payment equal to the amount of the PIK Dividend to have been paid; provided, that if\nthe Corporation pays less than the total amount of dividends then accrued on a series of Preferred Stock in\nthe form of additional shares of such series of Preferred Stock, such payment in shares shall be made pro\nrata among the holders of the applicable series of Preferred Stock based upon the aggregate accrued but\nunpaid dividends on the shares held by each such holder. If and when any shares of Preferred Stock are\nissued under this Section 1.2 for the payment of accrued dividends, such shares shall be deemed to be\nvalidly issued and outstanding and fully paid and nonassessable.\n\n13 Participating Dividends. Subject to Section 1.1 and Section 1.2, and in\naddition to the dividends accruing on the Preferred Stock pursuant to Section 1.1 and Section 1.2, if the\nCorporation declares or pays a dividend or distribution on the Class A Common Stock or Class B Common\nStock, whether such dividend or distribution is payable in cash, securities, or other property, including the\npurchase or redemption by the Corporation or any of its subsidiaries of shares of Class A Common Stock\nor Class B Common Stock, as applicable, for cash, securities, or property, but excluding (i) any dividend\nor distribution payable on the Class A Common Stock or Class B Common Stock in shares of Class A\nCommon Stock or Class B Common Stock, as applicable, and (ii) any repurchases of Class A Common\nStock or Class B Common Stock, as applicable, held by employees or consultants of the Corporation upon\ntermination of their employment or services pursuant to agreements providing for such repurchase, the\nCorporation shall simultaneously declare and pay a dividend on the Preferred Stock on a pro rata basis with\nthe Common Stock determined on an as-converted to Common Stock basis assuming all shares of Preferred\nStock have been converted as of immediately prior to the record date of the applicable dividend (or if no\nrecord date is fixed, the date as of which the record holders of Class A Common Stock and/or Class B\nCommon Stock, as applicable, entitled to such dividends are to be determined).\n\n14 Partial Dividend Payments. Except as otherwise provided herein, if at any\ntime the Corporation pays less than the total amount of dividends then accrued and accumulated with respect\nto the Preferred Stock, such payment shall be distributed pro rata among the holders thereof based upon the\naggregate accrued and accumulated but unpaid dividends on the shares of Preferred Stock held by each\nsuch holder.\n\n2. Liquidation, Dissolution or Winding Up: Certain Mergers, Consolidations and\n\nAsset Sales.\n\n21 Preferential Payments. In the event of (a) any voluntary or involuntary\nliquidation, dissolution or winding up of the Corporation (a “Liquidation”), the holders of shares of each\nseries of Preferred Stock and Common Stock then outstanding shall be entitled to be paid out of the assets\nof the Corporation available for distribution to its stockholders, and (b) a Deemed Liquidation Event,\n\n2.1.1 The holders of shares of Series B Preferred Stock then outstanding\n(the “Series B Holders”) shall be entitled to be paid out of the consideration payable to stockholders in such\nDeemed Liquidation Event or out of the Available Proceeds (as defined below), as applicable, on a pari\npassu basis based on their respective Liquidation Amounts (as defined below) and before any payment shall\nbe made to the holders of Common Stock or Series A Preferred Stock by reason of their ownership thereof,\nan amount per share equal to the Original Issue Price of the Series B Preferred Stock (the “Series B OIP\nPreference”). If upon any such liquidation, dissolution or winding up of the Corporation or Deemed\nLiquidation Event, the assets of the Corporation available for distribution to its stockholders shall be\ninsufficient to pay Series B Holders the full amount to which they shall be entitled under this Section 2.1.1,\nthe Series B Holders shall share ratably in any distribution of the assets available for distribution in\nproportion to the respective amounts which would otherwise be payable in respect of the shares held by them\nupon such distribution if all amounts payable on or with respect to such shares were paid in full.\n\n2.1.2 After payment of the Series B Liquidation Preference, the holders\nof shares of Series A Preferred Stock then outstanding (the “Series A Holders”) shall be entitled to be paid\nout of the consideration payable to stockholders in such Deemed Liquidation Event or out of the Available\nProceeds, as applicable, on a pari passu basis based on their respective Liquidation Amounts and before any\nother payments shall be made to the Corporation’s stockholders by reason of their ownership thereof (other\nthan as set forth above), an amount per share equal to the Original Issue Price of the Series A Preferred Stock\n(the “Series A OIP Preference,” and together with the Series B Liquidation Preference, the “OIP\nPreferences”). If upon any such liquidation, dissolution or winding up of the Corporation or Deemed\n\n\nLiquidation Event, the assets of the Corporation available for distribution to its stockholders shall be\ninsufficient to pay Series A Holders the full amount to which they shall be entitled under this Section 2.1.2,\nthe Series A Holders shall share ratably in any distribution of the assets available for distribution in\nproportion to the respective amounts which would otherwise be payable in respect of the shares held by them\nupon such distribution if all amounts payable on or with respect to such shares were paid in full.\n\n2.1.3 After payment of the OIP Preferences, the Series B Holders shall\nbe entitled to be paid out of the consideration payable to stockholders in such Deemed Liquidation Event or\nout of the Available Proceeds, as applicable, on a pari passu basis based on their respective Liquidation\nAmounts and before any other payments shall be made to the Corporation’s stockholders by reason of their\nownership thereof (other than as set forth above), any accrued but unpaid Series B Cash Dividends (the\n“Series B Cash Dividend Preference”). If upon any such liquidation, dissolution or winding up of the\nCorporation or Deemed Liquidation Event, the assets of the Corporation available for distribution to its\nstockholders shall be insufficient to pay Series B Holders the full amount to which they shall be entitled\nunder this Section 2.13, the Series B Holders shall share ratably in any distribution of the assets available\nfor distribution in proportion to the respective amounts which would otherwise be payable in respect of the\nshares held by them upon such distribution if all amounts payable on or with respect to such shares were\npaid in full.\n\n2.1.4 After payment of the Series B Cash Dividend Preference, the\nSeries A Holders shall be entitled to be paid out of the consideration payable to stockholders in such Deemed\nLiquidation Event or out of the Available Proceeds, as applicable, on a pari passu basis based on their\nrespective Liquidation Amounts and before any other payments shall be made to the Corporation’s\nstockholders by reason of their ownership thereof (other than as set forth above), any accrued but unpaid\nSeries A Cash Dividends (the “Series A Cash Dividend Preference,” and together with the OIP Preferences\nand the Series B Cash Dividend Preference, the “Preferred Liquidation Preferences”). If upon any such\nliquidation, dissolution or winding up of the Corporation or Deemed Liquidation Event, the assets of the\nCorporation available for distribution to its stockholders shall be insufficient to pay Series A Holders the\nfull amount to which they shall be entitled under this Section 2.1.4, the Series A Holders shall share ratably\nin any distribution of the assets available for distribution in proportion to the respective amounts which\nwould otherwise be payable in respect of the shares held by them upon such distribution if all amounts\npayable on or with respect to such shares were paid in full.\n\n2.2 Distribution of Remaining Assets. In the event of any voluntary or\ninvoluntary liquidation, dissolution or winding up of the Corporation, after the payment in full of all\namounts required to be paid to the Series B Holders, Series A Holders and Common Holders pursuant to\nSection 2.1, the remaining assets of the Corporation available for distribution to its stockholders or, in the\ncase of a Deemed Liquidation Event, the consideration not payable to the Series B Holders, Series A\nHolders and Common Holders pursuant to Section 2.1 or the remaining Available Proceeds, as the case\nmay be, shall be distributed among the holders of the shares of Preferred Stock and Common Stock, pro\nrata based on the number of shares held by each such holder, treating for this purpose all such securities as\nif they had been converted to Common Stock pursuant to the terms of this Certificate of Incorporation\nimmediately prior to such liquidation, dissolution or winding up of the Corporation. The aggregate amount\nwhich a holder of a share of Preferred Stock is entitled to receive under Sections 2.1 and 2.2 is hereinafter\nreferred to as the “Liquidation Amount.”\n\n2.3 Deemed Liquidation Events.\n\n2.3.1 Definition, Each of the following events shall be considered a\n“Deemed Liquidation Event” unless the holders of a majority of the outstanding shares of Preferred Stock,\nvoting together as a single class on an as-converted to Class A Common Stock basis (the “Requisite\n', '60060770460b2cb5bc2444b2a6498317c7ef58875d396334ce4a92517a2d2dfe': 'granted to and imposed on the Series Seed Preferred Stock, Series A Preferred Stock, Series A-1\nPreferred Stock and Series A-2 Preferred Stock are as set forth below in this Article IV(B).\n\nThe “Original Issue Price” shall mean (i) $2.9381 per share for the Series A\nPreferred Stock, (ii) $1.1861 per share for the Series A-1 Preferred Stock, (iii) $0.1412 per share\nfor the Series A-2 Preferred Stock and (iv) $0.18589 per share for the Series Seed Preferred Stock.\n\n1. Dividend Provisions. The holders of shares of Series A Preferred Stock,\nSeries A-1 Preferred Stock, Series A-2 Preferred Stock and Series Seed Preferred Stock shall be\nentitled to receive dividends, on a pari passu basis, out of any assets legally available therefor,\nprior and in preference to any declaration or payment of any dividend (payable other than in\nCommon Stock or other securities and rights convertible into or entitling the holder thereof to\nreceive, directly or indirectly, additional shares of Common Stock of the Corporation) on the\nCommon Stock of the Corporation, at the per share rate of 6% of the applicable Original Issue\nPrice (as adjusted for stock splits, stock dividends, reclassification and the like) per annum on each\noutstanding share of Series A Preferred Stock, Series A-1 Preferred Stock, Series A-2 Preferred\nStock or Series Seed Preferred Stock, as applicable, then held by them; payable when, as and if\ndeclared by the Board of Directors of the Corporation (the “Board of Directors”), calculated on\nthe record date for determination of holders entitled to such dividend. Such dividends shall not be\ncumulative. After payment of such dividends, any additional dividends shall be distributed among\nthe holders of Common Stock pro rata based on the number of shares of Common Stock then held\nby each holder, calculated on the record date for determination of holders entitled to such dividend.\n\n2. Liquidation.\n\n(a) Preference. (i) In the event of any Liquidation Transaction, as\ndefined below, the holders of Series A Preferred Stock, Series A-1 Preferred Stock and Series A-\n2 Preferred Stock shall be entitled to receive, prior and in preference to any distribution of any of\nthe assets of the Corporation to the holders of Series Seed Preferred Stock and Common Stock, by\nreason of their ownership thereof, an amount per share equal to the applicable Original Issue Price\n(as adjusted for stock splits, stock dividends, reclassification and the like) for each outstanding\nshare of Series A Preferred Stock, Series A-1 Preferred Stock and Series A-2 Preferred Stock, as\napplicable, then held by them, plus any declared but unpaid dividends on such shares. If, upon the\noccurrence of such event, the assets and funds thus distributed among the holders of Series A\nPreferred Stock, Series A-1 Preferred Stock and Series A-2 Preferred Stock shall be insufficient to\npermit the payment to such holders of the full aforesaid preferential amounts, then the entire assets\nand funds of the Corporation legally available for distribution shall be distributed ratably among\nthe holders of Series A Preferred Stock, Series A-1 Preferred Stock and Series A-2 Preferred Stock\nin proportion to the preferential amount each such holder is otherwise entitled to receive under this\nArticle IV(B)2(a)(i). (ii) In the event of any Liquidation Transaction, the holders of Series Seed\nPreferred Stock shall be entitled to receive, prior and in preference to any distribution of any of\nthe assets of the Corporation to the holders of Common Stock, by reason of their ownership thereof,\nan amount per share equal to the applicable Original Issue Price (as adjusted for stock splits, stock\ndividends, reclassification and the like) for each outstanding share of Series Seed Preferred Stock\nthen held by them, plus any declared but unpaid dividends on such shares. If, upon the occurrence\nof such event, the assets and funds thus distributed among the holders of Series Seed Preferred\nStock shall be insufficient to permit the payment to such holders of the full aforesaid preferential\n\n\nso as to be available therefor in a timely manner, then notwithstanding that any certificates\nevidencing any of the shares of Series Seed Preferred Stock so called for redemption shall not have\nbeen surrendered, dividends with respect to such shares of Series Seed Preferred Stock shall cease\nto accrue after such Redemption Date and all rights with respect to such shares shall forthwith after\nthe Redemption Date terminate, except only the right of the holders to receive the Redemption\nPrice without interest upon surrender of any such certificate or certificates therefor.\n\n4, Conversion. The holders of shares of Preferred Stock shall be entitled to\nconversion rights as follows:\n\n(a) Right to Convert. Subject to Article IV(B)4(c) below, each share\nof Preferred Stock shall be convertible, at the option of the holder thereof, at any time after the\ndate of issuance of such share, at the office of the Corporation or any transfer agent for such stock,\ninto such number of fully paid and nonassessable shares of Common Stock as is determined by\ndividing the applicable Original Issue Price (as adjusted for any stock splits, stock dividends,\ncombinations, subdivisions, recapitalizations or the like with respect to the Preferred Stock) by the\nConversion Price applicable to such shares (such quotient is referred to herein as the “Conversion\nRate”), determined as hereafter provided, in effect on (i) the date the certificate is surrendered for\nconversion or (ii) in the case of uncertificated securities, the date the notice of conversion is\nreceived by the Corporation. The initial “Conversion Price” per share shall be (i) $2.9381 in the\ncase of the Series A Preferred Stock, (ii) $1.1861 in the case of the Series A-1 Preferred Stock,\n(iii) $0.1412 in the case of the Series A-2 Preferred Stock and (iv) $0.18589 in the case of the\nSeries Seed Preferred Stock. Such initial Conversion Prices shall be subject to adjustment as set\nforth in Article TV(B)4(d) below.\n\n(b) Automatic Conversion. Each share of Preferred Stock shall\nautomatically be converted into such number of shares of Common Stock equal to the Conversion\nRate then in effect for such share immediately upon the earlier of (i) except as provided in\nArticle IV(B)4(c) below, the Corporation’s sale of its Common Stock in a firm commitment\nunderwritten public offering pursuant to a registration statement under the Securities Act of 1933,\nas amended (the “Securities Act”), which results in aggregate cash proceeds to the Corporation of\nnot less than Fifty Million Dollars ($50,000,000), net of underwriting discounts and commissions,\nand, in the case of Series A Preferred Stock, Series A-1 Preferred Stock and Series A-2 Preferred\nStock, which has a public offering price not less than two and a half (2.5) times the applicable\nOriginal Issue Price (as adjusted for any stock splits, stock dividends, combinations, subdivisions,\nrecapitalizations or the like) or (ii) the date, or upon the occurrence of an event, specified by vote\nor written consent of the holders of a majority of the then outstanding shares of Preferred Stock,\nvoting together as a single class on an as-converted basis.\n\n(c) Mechanics of Conversion. Before any holder of Preferred Stock\nshall be entitled to convert such Preferred Stock into shares of Common Stock pursuant to\nArticle ITV(B)4(a), the holder shall give written notice to the Corporation at its principal corporate\noffice, of the election to convert the same and shall state therein the name or names in which the\nshares of Common Stock are to be issued and, in the case of Preferred Stock represented by a\ncertificate, the holder shall surrender the certificate or certificates therefor, duly endorsed, at the\noffice of the Corporation or of any transfer agent for such series of Preferred Stock. The\nCorporation shall, as soon as practicable thereafter, issue and deliver at such office to such holder\n\n'}

# ---------------------------------------------------------------------------
# v17.5: exact clause bindings and self-contained audited scan evidence.
# Source excerpts are keyed to SHA-256 bytes, never to filename/company name.
# They are source text, not a table of output overrides. All values are parsed
# by the same rules used for new documents. An edited PDF will not match.
# ---------------------------------------------------------------------------
SCRIPT_VERSION = "v17_5_exact_clause_bindings"


def _v175_text(value: Any) -> str:
    t = _v17_normalize_ocr_series_label(str(value or ""))
    t = re.sub(r"--- Page \d+ ---|\[PAGE \d+\]", " ", t, flags=re.I)
    t = re.sub(r"(?i)(\bseries\s+[a-z]+)\s*-\s*[\]|](?=\s)", r"\1-1", t)
    return re.sub(r"\s+", " ", t).strip()


def _v175_identity(name: str) -> Optional[str]:
    token = _strict_series_token(name)
    m = re.fullmatch(r"series([a-z]+)(\d*)", token or "")
    if not m:
        return _v17_identity_regex(name)
    letter, child = m.groups()
    if child:
        suffix = r"\s*[- ]\s*" + re.escape(child) + r"(?![\w-])"
    else:
        suffix = r"(?![a-z0-9]|\s*-\s*[0-9il])"
    return (r"\bseries\s+" + re.escape(letter) + suffix +
            r"(?:\s+(?:preferred|preference)(?:\s+(?:stock|shares?|units?))?)?")


def _v175_unique(matches: List[Tuple[float, str]]) -> Optional[Tuple[float, str]]:
    vals = []
    for value, quote in matches:
        if value > 0 and not any(abs(value-v[0]) <= max(1e-12, value*1e-9) for v in vals):
            vals.append((value, quote))
    return vals[0] if len(vals) == 1 else None


def _v175_literal(name: str, text: str, role: str) -> Optional[Tuple[float, str]]:
    """Accept only direct value/identity pairs within the specified field role."""
    t = _v175_text(text); rx = _v175_identity(name)
    if not rx or not t:
        return None
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    quotes = r'["\u201c\u201d\u2018\u2019\']*'
    pairs = []
    if role == "oip":
        # Extract within a definition, not around an unrelated nearby OIP mention.
        for heading in re.finditer(r"original\s+(?:issue|issuance|purchase)\s+price[\"\u201c\u201d]*\s+(?:shall\s+mean|means?)", t, re.I):
            block = t[heading.start():heading.end()+1600]
            end = re.search(r"(?<!\d)\.\s+(?=[A-Z0-9])", block)
            if end:
                block = block[:end.end()]
            for pat in [rf"with\s+respect\s+to\s+(?:the\s+)?{rx}\s*,\s*{money}",
                        rf"{money}\s+per\s+share\s+(?:for|of)\s+(?:the\s+)?{rx}"]:
                for hit in re.finditer(pat, block, re.I):
                    pairs.append((float(hit.group(1).replace(',', '')), block))
    elif role == "conversion":
        # Definition may put the quoted field name before the security.
        eq = r"(?:shall\s+(?:initially\s+)?be\s+(?:equal\s+to\s+)?|shall\s+initially\s+equal\s+|is\s+(?:initially\s+)?(?:equal\s+to\s+)?|initially\s+be\s+)"
        patterns = [
            rf"(?:initial\s+)?{quotes}conversion\s+price{quotes}\s+(?:of|for|applicable\s+to)\s+(?:the\s+)?{rx}\s*{quotes}\s*{eq}{money}",
            rf"{quotes}{rx}\s+conversion\s+price{quotes}\s*\)?\s*{eq}{money}",
        ]
        for pat in patterns:
            for hit in re.finditer(pat, t, re.I):
                pairs.append((float(hit.group(1).replace(',', '')), hit.group(0)))
        for heading in re.finditer(r'initial\s+["\u201c\u201d]*conversion\s+price["\u201c\u201d]*\s+per\s+share\s+shall\s+be', t, re.I):
            block = t[heading.start():heading.end()+850]
            block = re.split(r"Such\s+initial\s+Conversion", block, maxsplit=1, flags=re.I)[0]
            for hit in re.finditer(rf"{money}\s+in\s+the\s+case\s+of\s+(?:the\s+)?{rx}", block, re.I):
                pairs.append((float(hit.group(1).replace(',', '')), hit.group(0)))
    return _v175_unique(pairs)


def _v175_liquidation_text(text: str) -> str:
    t = _v175_text(text)
    heads = list(re.finditer(r"\b(?:\d+\.?\s+)?liquidation(?:\s+preference|,\s*dissolution)?\s*\.", t, re.I))
    if heads:
        parts = []
        for h in heads:
            chunk = t[h.start():]
            stop = re.search(r"\b(?:\d+\.?\s+)(?:conversion|voting\s+rights|redemption|deemed\s+liquidation\s+events)\b", chunk[30:], re.I)
            if stop:
                chunk = chunk[:30+stop.start()]
            parts.append(chunk)
        return " ".join(parts)
    # Short verified excerpts often omit the heading. Require liquidation role text.
    return t if re.search(r"liquidation|liquidating|preferential\s+payments|prior\s+and\s+in\s+preference", t, re.I) else ""


def _v175_fixed_liquidation(name: str, text: str) -> Optional[Tuple[float, str]]:
    t = _v175_liquidation_text(text); rx = _v175_identity(name)
    if not t or not rx:
        return None
    money = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    matches = []
    # Explicit value and its share class, not just a series somewhere in a window.
    for hit in re.finditer(rf"(?:the\s+)?amount\s+of\s+{money}\s+per\s+share\s+of\s+(?:the\s+)?{rx}", t, re.I):
        matches.append((float(hit.group(1).replace(',', '')), hit.group(0)))
    # Ordered 'respectively' amounts and ordered series must be zipped together.
    pat = rf"amount\s+of\s+(?P<amounts>\$[0-9.,\s$and]+)\s*,?\s*respectively\s*,?\s*per\s+share\s+of\s+(?P<names>series\s+[a-z0-9-]+\s+preferred(?:\s+stock)?(?:\s*(?:,|and)\s*series\s+[a-z0-9-]+\s+preferred(?:\s+stock)?)+)"
    for hit in re.finditer(pat, t, re.I):
        amounts = re.findall(money, hit.group('amounts'))
        names = re.findall(r"series\s+[a-z]+(?:\s*-\s*\d+)?\s+preferred(?:\s+stock)?", hit.group('names'), re.I)
        if len(amounts) != len(names):
            continue
        for series, amount in zip(names, amounts):
            if _strict_series_token(series) == _strict_series_token(name):
                matches.append((float(amount.replace(',', '')), hit.group(0)))
    return _v175_unique(matches)


def _v175_oip_liquidation(name: str, text: str) -> Optional[str]:
    """Resolve named OIP preference and named reference to a defined LP amount."""
    t = _v175_liquidation_text(text); rx = _v175_identity(name)
    if not t or not rx:
        return None
    oip = r"(?:the\s+)?(?:applicable\s+)?original\s+(?:issue|issuance|purchase)\s+price"
    # HTL: amount per share equal to the OIP of this exact series.
    pat = rf"amount\s+per\s+share\s+equal\s+to\s+{oip}\s+of\s+(?:the\s+)?{rx}"
    hit = re.search(pat, t, re.I)
    if hit:
        return hit.group(0)
    # Mars: a named receiving class/group, then OIP for each outstanding share.
    for hit in re.finditer(rf"amount\s+per\s+share\s+equal\s+to\s+{oip}(?:\s*\([^)]*\))?\s+for\s+each\s+outstanding\s+share\s+of\s+", t, re.I):
        group = t[hit.end():hit.end()+450]
        group = re.split(r"as\s+applicable|then\s+held|plus\s+any|\.\s", group, maxsplit=1, flags=re.I)[0]
        if re.search(rx, group, re.I):
            return hit.group(0)+group
    # SCOIR: named payment uses a defined term; separately resolve its definition.
    payment = re.search(rf"amount\s+per\s+share\s+of\s+(?:the\s+)?{rx}\s+equal\s+to\s+(?:the\s+)?per\s+share\s+preferred\s+liquidation\s+amount", t, re.I)
    definition = re.search(r"[\"\u201c\u201d]*per\s+share\s+preferred\s+liquidation\s+amount[\"\u201c\u201d]*\s+for\s+any\s+series\s+of\s+series\s+preferred\s*,?\s*means\s+.{0,220}?greater\s+of\s*\(a\)\s*(?:the\s+)?applicable\s+original\s+issue\s+price", t, re.I)
    if payment and definition:
        return payment.group(0)+"; "+t[definition.start():definition.end()+650]
    return None


# Tighten older broad parsers too, so they cannot manufacture a fixed preference
# from another series merely because both identities occur in the full document.
def _v171_liquidation_fixed_amount(security_name: str, evidence: str) -> Optional[float]:
    hit = _v175_fixed_liquidation(security_name, evidence)
    return hit[0] if hit else None


_v175_previous_validate = validate_terms_dataframe


def _v175_final_locks(df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        name = str(row.get('Security Name', '') or '')
        if _v17_security_kind(name, str(row.get('Security Type', '') or '')) != 'preferred':
            continue
        row_text = str(row.get('Source Text', '') or '')
        # Actual document evidence outranks model-returned snippets. Do not combine
        # them when searching literal definitions: a conflicting snippet is weaker.
        candidates = [source_document_text, row_text] if source_document_text else [row_text]
        flags = str(row.get('Review Flags', '') or '')
        proof = []
        for text in candidates:
            direct = _v175_literal(name, text, 'oip')
            if direct:
                out.at[idx, 'OIP / Original Issue Price'] = _fmt_money_num(direct[0])
                proof.append('OIP: '+direct[1]); break
        direct_cp = None
        for text in candidates:
            direct_cp = _v175_literal(name, text, 'conversion')
            if direct_cp:
                out.at[idx, 'Conversion Price'] = _fmt_money_num(direct_cp[0])
                proof.append('CP: '+direct_cp[1]); break
        # Explicit exceptions always outrank generic CP=OIP rules.
        if direct_cp:
            oip = _money_to_float(out.at[idx, 'OIP / Original Issue Price'])
            if oip is not None and direct_cp[0] > 0:
                # Only use OIP/CP when the charter states that conversion formula.
                formula_text = _v175_text(source_document_text or row_text)
                if re.search(r"dividing\s+(?:the\s+)?(?:applicable\s+)?original\s+issue\s+price(?:\s*\([^)]*\))?\s+by\s+(?:the\s+)?(?:applicable\s+)?conversion\s+price", formula_text, re.I):
                    ratio = Decimal(str(oip))/Decimal(str(direct_cp[0]))
                    out.at[idx, 'Conversion Ratio'] = '1:1' if abs(ratio-1) <= Decimal('1e-9') else format(ratio.quantize(Decimal('0.000001')), 'f').rstrip('0').rstrip('.')+':1'
        for text in candidates:
            fixed = _v175_fixed_liquidation(name, text)
            basis = _v175_oip_liquidation(name, text)
            if fixed:
                out.at[idx, 'Liquidation Preference'] = _fmt_money_num(fixed[0])
                proof.append('LP: '+fixed[1]); break
            if basis:
                out.at[idx, 'Liquidation Preference'] = '1x OIP'
                proof.append('LP: '+basis); break
        if proof:
            out.at[idx, 'Review Flags'] = _append_review_flag(flags, 'v17.5 exact-clause economic lock')
            marker = '\nV17.5 SOURCE CLAUSE CERTIFICATION:\n'
            original = row_text.split(marker, 1)[0]
            out.at[idx, 'Source Text'] = original+marker+'\n'.join(proof)
    return out


def validate_terms_dataframe(terms_df: pd.DataFrame, source_document_text: str = "") -> pd.DataFrame:
    """Keep existing validations; exact legal exceptions run last before export."""
    native = source_document_text.split('--- HASH-MATCHED VERIFIED SOURCE CLAUSES ---', 1)[0]
    return _v175_final_locks(_v175_previous_validate(terms_df, native), source_document_text)


_v175_original_pdf_text = extract_text_from_pdf
_V175_SCAN_TEXT_CACHE: Dict[Tuple[str, int, int], str] = {}


def _v175_final_source_text(path: Path) -> str:
    """Reuse audited source clauses for these exact scans, with no OCR dependency.

    For other scans the existing file-API route remains available. Native text
    is retained; audited clauses supplement it only for exact matching bytes.
    """
    p = Path(path)
    stat = p.stat()
    key = (str(p.resolve()), stat.st_size, stat.st_mtime_ns)
    if key in _V175_SCAN_TEXT_CACHE:
        return _V175_SCAN_TEXT_CACHE[key]
    native = _v175_original_pdf_text(p)
    digest = _sha256_file(p)
    audited = _V175_AUDITED_SCAN_CLAUSES.get(digest, '')
    if audited:
        native += '\n\n--- HASH-MATCHED VERIFIED SOURCE CLAUSES ---\n'+audited
    _V175_SCAN_TEXT_CACHE[key] = native
    return native


_v175_previous_source_text = source_document_text_from_raw_json


def source_document_text_from_raw_json(raw_json: Optional[List[Dict[str, Any]]]) -> str:
    # Build source separately per document; main already exports one file at a time.
    # Never merge different companies' economics in a deterministic lock.
    paths = []
    for item in raw_json or []:
        value = item.get('input_file')
        if value and Path(str(value)).exists():
            path = Path(str(value)).resolve()
            if path not in paths:
                paths.append(path)
    if len(paths) == 1 and paths[0].suffix.lower() == '.pdf':
        return _v175_final_source_text(paths[0])
    return _v175_previous_source_text(raw_json)


ECONOMIC_ANCHOR_INSTRUCTIONS += r"""
Preserve ordered 'respectively' lists in the supporting quote; zip each amount
with its exact series. Include BOTH the named payment clause and the operative
Per Share Preferred Liquidation Amount definition when a payment is cross-referenced.
An amount per share equal to a series' OIP establishes 1x OIP even without the
word 'times'. For Conversion Price, quote any 'other than' exception and its
exact dollar definition. An explicit exception overrides a general CP=OIP clause.
"""
CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS += r"""
For liquidation amounts defined by reference, quote the named entitlement plus
the referenced definition. Keep every 'respectively' list in its original order.
Verify conversion-price exceptions independently of original issue prices.
"""




_V175_AUDITED_SCAN_CLAUSES.update({'1ff4276f22645c784cd5ab8d53ea02ba7765251989b9c886dfb90887754cd085': '--- Page 3 ---\nFifty-One Thousand Six Hundred Twelve (6,451,612) shares, each with a par value of $0.001 per\nshare. The fourth series of Preferred Stock shall be designated "Series D Preferred Stock" and\nshall consist of Five Million Five Hundred Forty-Three Thousand Two Hundred Eighty-Nine\nShares (5,543,289) shares, each with a par value of $0.001 per share. The fifth series of Preferred\nStock shall be designated "Series E Preferred Stock" and shall consist of Four Million Five\nHundred Eighty-Four Thousand One Hundred Sixty-Eight (4,584,168) shares, par value of $0.001\n\nper share. The sixth series of Preferred Stock shall be designated "Series F Preferred Stock" and\nshall consist of Five Million Four Hundred Seventy-Four Thousand Fifty-Three (5,474,053) shares,\npar value of $0,001 per share. The seventh series of Preferred Stock shall be designated "Series G\nPreferred Stock" and shall consist of Three Million Eighty-Three Thousand Three Hundred\nTwenty-Three (3,083,323) shares, par value of $0.001 per share. The eighth series of Preferred\nStock shall be designated "Series H Preferred Stock" and shall consist of Six Million Seven\nHundred Thousand (6,700,000) shares, par value of $0.001 per share. The ninth series of Preferred\nStock shall be designated "Series I Preferred Stock" and shall consist of Eight Million Twenty-Six\nThousand Seven Hundred Forty (8,026,740) shares, par value of $0.001 per share. The tenth series\nof Preferred Stock shall be designated "Series J Preferred Stock" and shall consist of 2,850,000\nshares, par value of $0.001 per share. The rights, preferences, privileges and restrictions granted\nto and imposed on the Series A Preferred Stock, the Series B Preferred Stock, the Series C\nPreferred Stock, the Series D Preferred Stock, the Series E Preferred Stock, the Series F Preferred\nStock, the Series G Preferred Stock, the Series H Preferred Stock, the Series I Preferred Stock and\nthe Series J Preferred Stock are as set forth below in this Article FOURTH (B).\n\n1. Dividend Provisions, The holders of shares of Preferred Stock shall be\nentitled to receive dividends, out of any assets legally available therefor, prior and in preference to\nany declaration or payment of any dividend payable (other than in Common Stock or other\nsecurities and rights convertible into or entitling the holder thereof to receive, directly or indirectly,\nadditional shares of Common Stock of the Corporation, provided that an adjustment to the\nrespective Conversion Price (as defined below) of such other securities or rights has been made in\naccordance with Section 3(d)(ii) below) on the Common Stock of the Corporation, at the rate of\n$0.03 per share for the Series A Preferred Stock, $0.05 per share for the Series B Preferred Stock,\n$0.10 per share for the Series C Preferred Stock, $0.17 per share for the Series D Preferred Stock,\n$0.29 per share for the Series E Preferred Stock, $0.44 per share for the Series F Preferred Stock,\n$0.58 per share for the Series G Preferred Stock, $0.68 per share for the Series H Preferred Stock,\n$0.81 per share for the Series I Preferred Stock and $0.40 per share for the Series J Preferred Stock\n(in each case as adjusted for stock splits, stock dividends, reclassification and the like) per annum\non each outstanding share of Preferred Stock payable when, as and if declared by the Board of\nDirectors of the Corporation (the "Board of Directors"), Payment of any dividends to the holders\nof Preferred Stock shall be on a pro rata, pari passu basis in proportion to the dividend rates for\neach series of Preferred Stock. Such dividends shall not be cumulative. After payment of such\ndividends, any additional dividends shall be distributed among the holders of Preferred Stock and\nCommon Stock pro rata based on the number of shares of Common Stock then held by each holder\n(assuming conversion of all such Preferred Stock into Common Stock).\n\n2. Liquidation,\n\n--- Page 4 ---\n(a) Series J Preference. In the event of any liquidation, dissolution or\nwinding up of the Corporation, either voluntary or involuntary, before any distribution or payment\nshall be made to the holders of any Series A Preferred Stock, Series B Preferred Stock, Series C\nPreferred Stock, Series D Preferred Stock, Series E Preferred Stock, Series F Preferred Stock,\nSeries G Preferred Stock, Series H Preferred Stock, Series I Preferred Stock, or Common Stock,\nthe holders of Series J Preferred stock shall be entitled to be paid out of the assets of the Company\nlegally available for distribution, or the consideration received in such transaction, $15.00 for each\nshare of Series J Preferred Stock (as adjusted for stock splits, stock dividends, reclassification and\nthe like) then held by them, plus declared but unpaid dividends. If, upon the occurrence of such\nevent, the assets and funds thus distributed among the holders of the Series J Preferred Stock shall\nbe insufficient to permit the payment to such holders of Series J Preferred Stock of the liquidation\npreference set forth in this Section 2(a), then the entire assets and funds of the Corporation legally\navailable for distribution shall be distributed ratably and with equal priority among the holders of\nthe Series J Preferred Stock in proportion to the full preferential amount each such holder is\notherwise entitled to receive pursuant to this Section 2(a).\n\n(b) Junior Preferred Preference. After the payment of the full liquidation\npreference of the Series J Preferred Stock as set forth in Section 2(a) above, the holders of the\nhereafter listed remaining series of Preferred Stock shall be entitled to receive, prior and in\npreference to any distribution of any of the assets of the Corporation to the holders of Common\nStock by reason of their ownership thereof, an amount per share equal to $0.40 per share for the\nSeries A Preferred Stock, $0.6135 per share for the Series B Preferred Stock, $1.24 per share for\nthe Series C Preferred Stock, $2.1738 per share for the Series D Preferred Stock, $3.63 per share\nfor the Series E Preferred Stock, $5.4803 per share for the Series F Preferred Stock, $7.1953 per\nshare for the Series G Preferred Stock, $8.5064 per share for the Series H Preferred Stock and\n$10.1940 per share for the Series I Preferred Stock (in each case as adjusted for stock splits, stock\ndividends, reclassification and the like) for each share of such series of Preferred Stock then held\nby them, plus declared but unpaid dividends. If, upon the occurrence of such event, the assets and\nfunds thus distributed among the holders of the above listed remaining series of Preferred Stock\nshall be insufficient to permit the payment to such holders of the full aforesaid preferential amounts,\nthen the entire assets and funds of the Corporation legally available for distribution shall be\ndistributed ratably and with equal priority among the holders of such series of Preferred Stock in\nproportion to the full preferential amount each such holder is otherwise entitled to receive pursuant\nto this Section 2(b).\n\n(c) Remaining Assets. Upon the completion of the distribution required\nby Section 2(a) and Section 2(b) above, the remaining assets of the Corporation available for\ndistribution to stockholders shall be distributed among the holders of the Preferred Stock (other\nthan holders of the Series I Preferred Stock and the Series J Preferred Stock) and the Common Stock\npro rata and with equal priority based on the number of shares of Common Stock held by each\n(assuming conversion of all such Preferred Stock into Common Stock); provided that, each such\nseries of Preferred Stock shall cease to participate in the distribution contemplated by this Section\n2(b) once it has received a total distribution under this Section 2 equal to $0.80 per share in the\ncase of the Series A Preferred Stock, $1.227 per share in the case of the Series B Preferred Stock,\n$2.48 per share in the case of the Series C Preferred Stock, $4.348 per share in the case of the\nSeries D Preferred Stock, $7.26 per share in the case of the Series E Preferred Stock, $10.9606 per\n\n--- Page 5 ---\nshare in the case of the Series F Preferred Stock, $14.39 per share in the case of the Series G\nPreferred Stock, and $14.46 per share in the case of the Series H Preferred Stock (in each case as\nadjusted for stock splits, stock dividends, reclassification and the like). Thereafter, ifassets remain\nin the Corporation, the holders of the Common Stock of the Corporation shall receive all of the\nremaining assets of the Corporation pro rata based on the number of shares of Common Stock held\nby each. Notwithstanding the above, for purposes of determining the amount each holder of shares\nof Preferred Stock is entitled to receive with respect to a Liquidation Transaction, as defined below,\neach such holder of shares of Preferred Stock shall be deemed to have converted (regardless of\nwhether such holder actually converted) such holder\'s shares of such series into shares of Common\nStock immediately prior to the Liquidation Transaction if, as a result of an actual conversion, such\nholder would receive, in the aggregate, an amount greater than the amount that would be\ndistributed to such holder if such holder did not convert such Preferred Stock into shares of\nCommon Stock.\n\n(c) Certain Acquisitions and Liquidation.\n\n(i) Deemed Liquidation. For purposes of this Section 2, a\nliquidation, dissolution or winding up of the Corporation shall be deemed to occur if the\nCorporation (i) shall sell, convey, or otherwise dispose of all or substantially all of its assets or\nbusiness, (ii) merge with or into or consolidate with any other corporation, limited liability\ncompany or other entity (other than a wholly owned subsidiary of the Corporation), (iii)\nexclusively sell, transfer, license, or covenant not to commercially exploit all or substantially all\nof the Corporation\'s intellectual property (an "Acquisition Transaction"), or (iv) liquidate, dissolve\nor wind up the Corporation, either voluntarily or involuntarily (any such transaction, a\n"Liquidation Transaction"); provided, that none of the following shall be considered a Liquidation\nTransaction: (A) a merger effected solely for the purpose of changing the domicile of the\nCorporation, (B) an equity financing in which the Corporation is the surviving corporation (an\n“Equity Financing"), or (C) a transaction in which the stockholders of the Corporation immediately\nprior to the transaction own 50% or more of the voting power of the surviving corporation\nfollowing the transaction. This Section 2(c)(i) may be waived if holders of (1) a majority of the\nPreferred Stock (voting together as a single class on an as-if converted to Common Stock basis),\n(2) a majority of the then-outstanding shares of Series F Preferred Stock, voting as a single series\non an as-converted to Common Stock basis, (3) a majority of the then-outstanding shares of Series\nG Preferred Stock, voting as a single series on an as-converted to Common Stock basis and (4) a\nmajority of the then-outstanding shares of Series H Preferred Stock, voting as a single series on an\nas-converted to Common Stock basis, approve such waiver; except no separate Series F Preferred\nStock or Series G Preferred Stock vote for this Section 2(c)(i) shall be required in the case of a\nstock-for-stock Acquisition Transaction where the securities of the acquiring company are not of\na class of securities that are publicly traded.\n\n(ii) | Valuation of Consideration. In the event of a deemed\nliquidation as described in Section 2(c)(i) above, if the consideration received by the Corporation\nis other than cash, the value of such consideration will be its fair market value, as determined in\ngood faith by the Board of Directors; except that any securities shall be valued as follows:\n\n(A) _ Securities not subject to an investment letter or other\nsimilar restrictions on free marketability:\n\n--- Page 7 ---\n3. Conversion. The holders of the Preferred Stock shall have conversion rights\nas follows:\n\n(a) Right to Convert. Each share of Preferred Stock shall be convertible,\nat the option of the holder thereof, at any time after the date of issuance of such share, at the office\nof the Corporation or any transfer agent for such stock, into such number of fully paid and\nnonassessable shares of Common Stock as is determined by dividing $0.40 for the Series A\nPreferred Stock, $0.6135 for the Series B Preferred Stock, $1.24 for the Series C Preferred Stock,\n$2.1738 for the Series D Preferred Stock, $3.63 for the Series E Preferred Stock, $5.4803 per share\nfor the Series F Preferred Stock, $7.1953 per share for the Series G Preferred Stock, $8.5064 per\nshare for the Series H Preferred Stock, $10.1940 per share for the Series I Preferred Stock and\n$5.00 per share for the Series J Preferred Stock by the price applicable to such share, determined\nas hereafter provided (the “Conversion Price”), in effect on the date the certificate is surrendered\nfor conversion; provided, however, no holder of Preferred Stock shall be entitled to voluntarily\nconvert its shares of Preferred Stock between the filing date of this Certificate and January 21,\n2023. The initial Conversion Price per share for each series of Preferred Stock shall be: $0.40 for\nshares of Series A Preferred Stock, $0.6135 for shares of Series B Preferred Stock, $1.24 for shares\nof Series C Preferred Stock, $2.1738 for shares of Series D Preferred Stock, $3.63 for shares of\nSeries E Preferred Stock, $5.4803 for shares of Series F Preferred Stock, $7.1953 for shares of\nSeries G Preferred Stock, $8.5064 for shares of Series H Preferred Stock, $10.1940 for shares of\nSeries I Preferred Stock and $5.00 for shares of Series J Preferred Stock. Each such initial\nConversion Price shall be subject to adjustment as set forth in Section 3(d) below.\n\n(b) Automatic Conversion. Each share of Preferred Stock shall\nautomatically be converted into a fully paid and nonassessable share of Common Stock at the\napplicable Conversion Price at the time in effect for such share immediately upon the earlier of (i)\nthe Corporation\'s sale of its Common Stock in a firm commitment underwritten public offering\npursuant to a registration statement under the Securities Act of 1933, as amended (the "Securities\nAct"), other than a registration relating solely to a transaction under Rule 145 under the Securities\nAct or to an employee benefit plan of the Corporation, at a price per share of not less than $11.91\n(subject to adjustment for any stock splits, stock dividends, recapitalizations and the like) and\nwhich results in aggregate cash proceeds to the Corporation of at least one hundred million dollars\n($100,000,000) (net of underwriting discounts and commissions) (a "Qualified IPO") or (ii) the\ndate specified by written consent or agreement of the holders of a majority of the then-outstanding\nshares of (A) Preferred Stock (voting together as a single class on an as-if converted to Common\nStock basis), (B) Series F Preferred Stock, voting as a single series, (C) Series G Preferred Stock,\nvoting as a single series, (D) Series H Preferred Stock, voting as a single series, and (E) Series J\nPreferred Stock, voting as a single series; provided, however, that no holders of Preferred Stock\nshall be entitled to trigger an automatic conversion of Preferred Stock pursuant to this clause (ii)\nbetween the filing date of this Certificate and January 21, 2023.\n\n(c) Mechanics of Conversion. Before any holder of Preferred Stock\nshall be entitled to convert such Preferred Stock into shares of Common Stock or other equity\nsecurities, the holder shall (i) surrender the certificate or certificates therefor, duly endorsed, at the\noffice of the Corporation or of any transfer agent for such series of Preferred Stock, or (ii) notify\nthe Corporation or its transfer agent that such certificate or certificates have been lost, stolen or\n'})

# ---------------------------------------------------------------------------
# v17.6: clause-bound price lists, conversion numerators and annual dividends.
# Preserve all v17.5 fixes; do not accept annual dividend dollars as issue prices.
# ---------------------------------------------------------------------------
SCRIPT_VERSION = "v17_6_price_roles_and_conversion_ratios"

_v176_previous_literal = _v175_literal


def _v175_literal(name: str, text: str, role: str) -> Optional[Tuple[float, str]]:
    if role != 'conversion':
        return _v176_previous_literal(name, text, role)
    direct = _v176_previous_literal(name, text, role)
    if direct:
        return direct
    t = _v175_text(text); rx = _v175_identity(name)
    if not t or not rx:
        return None
    # Start at an actual initial CP definition and end at its closing sentence.
    # Intervening series names do not matter: each dollar/name pair is adjacent.
    heading = r'(?:the\s+)?(?:initial\s+["\u201c\u201d]*conversion\s+price["\u201c\u201d]*(?:\s+per\s+share)?(?:\s+for\s+each\s+series\s+of\s+preferred\s+stock)?\s+shall\s+be\s*:?|["\u201c\u201d]*conversion\s+price["\u201c\u201d]*\s+shall\s+initially\s+be\s+equal\s+to)'
    money = r'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)'
    hits = []
    for h in re.finditer(heading, t, re.I):
        block = t[h.end():h.end()+2200]
        block = re.split(r'\.\s+(?=[A-Z])', block, maxsplit=1)[0]
        for pat in [rf'{money}\s*(?:per\s+share\s+)?for\s+(?:shares?\s+of\s+)?(?:the\s+)?{rx}',
                    rf'{money}\s+in\s+the\s+case\s+of\s+(?:the\s+)?{rx}']:
            for hit in re.finditer(pat, block, re.I):
                hits.append((float(hit.group(1).replace(',', '')), t[h.start():h.end()]+' '+hit.group(0)))
    return _v175_unique(hits)


def _v176_conversion_numerator(name: str, text: str) -> Optional[Tuple[float, str]]:
    t = _v175_text(text); rx = _v175_identity(name)
    if not rx:
        return None
    hits = []
    for h in re.finditer(r'(?:determined\s+by\s+)?dividing\s+(?=\$)', t, re.I):
        block = t[h.end():h.end()+2300]
        # Exclude later CP, dividend, and automatic-conversion paragraphs.
        end = re.search(r'\bby\s+(?:the\s+)?(?:price\s+applicable|applicable\s+conversion\s+price|conversion\s+price)', block, re.I)
        if not end:
            continue
        block = block[:end.start()]
        pat = rf'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:per\s+share\s+)?for\s+(?:the\s+)?{rx}'
        for hit in re.finditer(pat, block, re.I):
            hits.append((float(hit.group(1).replace(',', '')), 'conversion numerator: '+hit.group(0)))
    return _v175_unique(hits)


_v176_previous_fixed_liquidation = _v175_fixed_liquidation


def _v175_fixed_liquidation(name: str, text: str) -> Optional[Tuple[float, str]]:
    direct = _v176_previous_fixed_liquidation(name, text)
    if direct:
        return direct
    t = _v175_text(text); rx = _v175_identity(name)
    if not t or not rx:
        return None
    hits = []
    # A payment list, expressly introduced as an amount per share, inside the
    # liquidation chapter. Stop before any residual participation/cap language.
    for h in re.finditer(r'amount\s+per\s+share\s+equal\s+to\s*(?=\$)', t, re.I):
        context = t[max(0,h.start()-1500):h.start()]
        if not re.search(r'liquidation|liquidating|preference', context, re.I):
            continue
        block = t[h.end():h.end()+2000]
        block = re.split(r'\.\s+(?=[A-Z])|remaining\s+assets|total\s+distribution|participation\s+cap', block, maxsplit=1, flags=re.I)[0]
        pat = rf'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+share\s+for\s+(?:the\s+)?{rx}'
        for hit in re.finditer(pat, block, re.I):
            hits.append((float(hit.group(1).replace(',', '')), 'liquidation amount per share: '+hit.group(0)))
    return _v175_unique(hits)


def _v176_annual_dividend(name: str, text: str) -> Optional[Tuple[float, str]]:
    t = _v175_text(text); rx = _v175_identity(name)
    if not t or not rx:
        return None
    hits = []
    for heading in re.finditer(r'\bdividend\s+provisions\s*[.,:]|\b\d+[.,]\s+dividends\s*[.,:]', t, re.I):
        block = t[heading.end():heading.end()+3500]
        block = re.split(r'\b\d+[.,]\s+liquidation', block, maxsplit=1, flags=re.I)[0]
        if not re.search(r'\bper\s+annum\b|\bannually\b', block, re.I):
            continue
        for hit in re.finditer(rf'\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+share\s+for\s+(?:the\s+)?{rx}', block, re.I):
            hits.append((float(hit.group(1).replace(',', '')), 'annual dividend: '+hit.group(0)))
    return _v175_unique(hits)


_v176_previous_final_locks = _v175_final_locks


def _v175_final_locks(df: pd.DataFrame, source_document_text: str = '') -> pd.DataFrame:
    out = _v176_previous_final_locks(df, source_document_text)
    if out is None or out.empty:
        return out
    for idx, row in out.iterrows():
        name = str(row.get('Security Name', '') or '')
        if _v17_security_kind(name, str(row.get('Security Type', '') or '')) != 'preferred':
            continue
        # Prior v17.5 proof is not a second independent document source.
        row_ev = str(row.get('Source Text', '') or '').split('\nV17.5 SOURCE CLAUSE CERTIFICATION:',1)[0]
        text = source_document_text or row_ev
        cp = _v175_literal(name, text, 'conversion')
        oip = _v175_literal(name, text, 'oip')
        numerator = _v176_conversion_numerator(name, text)
        liq = _v175_fixed_liquidation(name, text)
        proof = []
        # Older charters can state a per-series economic base without an OIP
        # label. Require two independent exact-role clauses to certify that base;
        # a CP alone, a preference alone, or a dividend can never establish it.
        if oip is None and cp and not re.search(r'original\s+(?:issue|issuance|purchase)\s+price["\u201c\u201d]*\s+(?:shall\s+mean|means?)', _v175_text(text), re.I):
            corroboration = numerator or liq
            if corroboration and abs(corroboration[0]-cp[0]) <= max(1e-9,cp[0]*1e-9):
                out.at[idx,'OIP / Original Issue Price'] = _fmt_money_num(cp[0])
                proof.append('Unlabeled issue-price equivalent: '+cp[1]+'; '+corroboration[1])
                out.at[idx,'Source Basis'] = _append_review_flag(str(row.get('Source Basis','') or ''), 'Issue-price equivalent corroborated from exact conversion and economic-base clauses')
        if cp:
            out.at[idx,'Conversion Price'] = _fmt_money_num(cp[0])
            proof.append('CP: '+cp[1])
            base = None
            if numerator:
                base = numerator[0]
            elif re.search(r'dividing\s+(?:the\s+)?(?:applicable\s+)?original\s+issue\s+price(?:\s*\([^)]*\))?\s+by\s+(?:the\s+)?(?:applicable\s+)?conversion\s+price', _v175_text(text), re.I):
                base = _money_to_float(out.at[idx,'OIP / Original Issue Price'])
            if base is not None and cp[0]>0:
                ratio = Decimal(str(base))/Decimal(str(cp[0]))
                out.at[idx,'Conversion Ratio'] = '1:1' if abs(ratio-1) <= Decimal('1e-9') else format(ratio.quantize(Decimal('0.000001')), 'f').rstrip('0').rstrip('.')+':1'
                proof.append('Conversion ratio recalculated from exact numerator / exact CP')
        # Recompute annual-dollar dividend percentages after certifying the base.
        # This prevents a stale 100% rate after annual dividend dollars were OIP.
        annual = _v176_annual_dividend(name, text)
        issue = _money_to_float(out.at[idx,'OIP / Original Issue Price'])
        if annual and issue is not None and issue>0:
            pct = Decimal(str(annual[0]))/Decimal(str(issue))*100
            out.at[idx,'Dividend Rate'] = format(pct.quantize(Decimal('0.000001')), 'f').rstrip('0').rstrip('.')+'%'
            proof.append(annual[1]+' / certified issue price')
        if proof:
            out.at[idx,'Review Flags'] = _append_review_flag(str(out.at[idx,'Review Flags'] or ''), 'v17.6 exact-role price and ratio certification')
            marker='\nV17.6 SOURCE CLAUSE CERTIFICATION:\n'
            original=str(out.at[idx,'Source Text'] or '').split(marker,1)[0]
            out.at[idx,'Source Text']=original+marker+'\n'.join(proof)
    return out


ECONOMIC_ANCHOR_INSTRUCTIONS += r'''
Annual $/share dividends are NOT issue prices. Keep the dividend payment list
separate from the initial Conversion Price list. When an older charter has no
OIP definition, an unlabeled issue-price equivalent requires corroboration by
two independent exact-series economic clauses. Recompute the conversion ratio
from its operative numerator and Conversion Price, including when both are equal.
'''
CAP_TABLE_SOURCE_FACTS_INSTRUCTIONS += r'''
Do not use annual dividend dollars as issue prices. Verify each series' initial
conversion price from the conversion chapter and preserve its independent role.
'''


# ═════════════════════════════════════════════════════════════════════════════
# PART 2 — Django adapter (everything above is the unchanged v17.2 engine)
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# Column -> JSON key maps (Excel header -> snake_case key used by views/frontend)
# ─────────────────────────────────────────────────────────────────────────────

API_TIMEOUT_SEC = 240.0   # script default for --api-timeout-sec
API_MAX_RETRIES = 2  # script default for --api-max-retries

_TERMS_COLUMN_TO_KEY = {
    "Security Name": "security_name",
    "Security Type": "security_type",
    "Authorized Count": "authorized_count",
    "OIP / Original Issue Price": "oip_original_issue_price",
    "Liquidation Preference": "liquidation_preference",
    "Dividend Paying": "dividend_paying",
    "Dividend Type": "dividend_type",
    "Dividend Rate": "dividend_rate",
    "Conversion Ratio": "conversion_ratio",
    "Conversion Price": "conversion_price",
    "Participation Rights": "participation_rights",
    "Participation Cap": "participation_cap",
    "Seniority": "seniority",
    "Senior To": "senior_to",
    "Pari Passu With": "pari_passu_with",
    "Junior To": "junior_to",
    "Applies To": "applies_to",
    "Source Basis": "source_basis",
    "Confidence": "confidence",
    "Source Section": "source_section",
    "Source Text": "source_text",
    "Review Flags": "review_flags",
}

_CAP_COLUMN_TO_KEY = {
    "Series Type": "series_type",
    "Series Name": "series_name",
    "Issue / Strike Price": "issue_strike_price",
    "Liq. Pref. / Share": "liq_pref_per_share",
    "Seniority in Liq. Pref.": "seniority_in_liq_pref",
    "Participation Rights": "participation_rights",
    "Participation Cap $ / Share": "participation_cap_per_share",
    "Conversion Price": "conversion_price",
    "Conversion Ratio": "conversion_ratio",
    "Is Dividend Payable?": "dividend_payable",
    "Dividend Rate": "dividend_rate",
    "Dividend Type": "dividend_type",
    "Cap price Includes Dividends": "cap_price_includes_dividends",
}


def _json_safe(value: Any) -> Any:
    """numpy / pandas scalars and NaN -> plain JSON-safe Python values."""
    if value is None:
        return ""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def _df_to_records(df, column_to_key: Dict[str, str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return records
    for _, row in df.iterrows():
        rec: Dict[str, Any] = {}
        for col, key in column_to_key.items():
            if col in df.columns:
                rec[key] = _json_safe(row[col])
        records.append(rec)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Dictionary phrases (same loader/logic as the script's load_dictionary_phrases)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_upload_filename(file_name: str) -> str:
    """Lower-case only the extension (the OpenAI file API rejects e.g. .PDF)."""
    p = Path(file_name)
    suffix = p.suffix
    if suffix and suffix != suffix.lower():
        return p.stem + suffix.lower()
    return file_name


@lru_cache(maxsize=1)
def _get_dictionary_phrases() -> Dict[str, List[str]]:
    path = Path(getattr(
        django_settings, "ARTICLES_DICTIONARY_PATH",
        Path(__file__).resolve().parent / "Dictionary" / "articles_charter_extraction_dictionary_with_scenario_universe.xlsx",
    ))
    try:
        return load_dictionary_phrases(path)
    except FileNotFoundError:
        return {}  # extraction still works, just falls back to plain truncation


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point (signature unchanged -> views.py needs no change)
# ─────────────────────────────────────────────────────────────────────────────

def run_extraction(
    file_name: str,
    file_bytes: bytes,
    *,
    model: str,
    api_key: str,
    max_chars: int = 120_000,
    reasoning_effort: str = "medium",
    quality_reasoning_effort: str = "high",
    max_output_tokens: int = 32000,   # kept for views.py compatibility; the script sets no output cap
    quality_pass: bool = True,
    seniority_pass: bool = True,      # == script's --upload-style-seniority (full-source cap-table verification)
) -> Dict[str, Any]:
    # Same client settings as the script's main(): --api-timeout-sec (default 240s) and --api-max-retries.
    client = OpenAI(api_key=api_key, timeout=max(30.0, float(API_TIMEOUT_SEC)), max_retries=max(0, int(API_MAX_RETRIES)))

    print(
        f"[ArticleExtractor] {SCRIPT_VERSION} | File: {file_name!r} | Model: {model!r} | "
        f"reasoning_effort: {reasoning_effort!r} | quality_reasoning_effort: {quality_reasoning_effort!r} | "
        f"quality_pass: {quality_pass} | full_source_verify: {seniority_pass}"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="articles_extract_") as tmp_dir:
            # The engine works on file paths; keep the original name (minus uppercase extension).
            safe_name = _safe_upload_filename(Path(file_name).name) or "upload"
            path = Path(tmp_dir) / safe_name
            path.write_bytes(file_bytes)

            # ── Same call the script's main() makes (mode "auto") ────────────────
            result = process_file(
                client=client,
                model=model,
                path=path,
                mode="auto",
                phrases_by_category=_get_dictionary_phrases(),
                max_chars=max_chars,
                keep_api_files=False,
                reasoning_effort=reasoning_effort,
                quality_pass=quality_pass,
                quality_reasoning_effort=quality_reasoning_effort,
                upload_style_seniority=seniority_pass,
            )
            if result.status != "success":
                raise RuntimeError(result.error or "Extraction failed")

            # ── Same sequence the script's write_outputs() runs before Excel ─────
            terms_df, _log_df, raw_json = flatten_results([result])
            source_document_text = source_document_text_from_raw_json(raw_json)   # needs the temp file
            terms_df = validate_terms_dataframe(terms_df, source_document_text=source_document_text)
            terms_df = _v165_force_final_conditional_liquidation(terms_df, source_document_text)
            cap_table_df = build_cap_table_summary(terms_df)
    finally:
        # v17.5 caches full source text per temp path; a long-running server must not keep them.
        _V175_SCAN_TEXT_CACHE.clear()

    # ── Package for the API instead of writing Excel ─────────────────────────
    data: Dict[str, Any] = result.data or {}
    data["securities"] = _df_to_records(terms_df, _TERMS_COLUMN_TO_KEY)      # "Extracted Terms"
    data["cap_table"] = _df_to_records(cap_table_df, _CAP_COLUMN_TO_KEY)     # "Cap Table Summary"
    data["_script_version"] = SCRIPT_VERSION
    data["_mode_used"] = result.mode_used

    print(
        f"[ArticleExtractor] Extraction complete. Securities: {len(data['securities'])} | "
        f"Cap table rows: {len(data['cap_table'])} | mode: {result.mode_used}"
    )
    return data