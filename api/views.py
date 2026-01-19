from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics, filters
from django.db.models import Count
from rest_framework.parsers import MultiPartParser, FormParser
from django.db.models.functions import Coalesce
from django_filters.rest_framework import DjangoFilterBackend
from decimal import Decimal, InvalidOperation
from django.db.models import Q, Prefetch
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination

from .models import Company, FinancialRecord
from .serializers import DashboardSummarySerializer, CompanySerializer, FinancialRecordSerializer, CompareRequestSerializer
from .utils_master_sheet import process_master_screening_v2
import re
import openai
from django.conf import settings
from .utils.openai_helpers import call_openai_compare
from rest_framework.response import Response
import json
from rest_framework import status
from rest_framework import serializers, status
from django.db.models import Count
from django.db.models.functions import Lower, Trim
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from django.core.cache import cache
import itertools
import hashlib
from django.db import connection
from nltk.stem.porter import PorterStemmer
from django.db.models import Q
import html
import unicodedata
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
import math
import datetime

_stemmer = PorterStemmer()

class ExcelUploadAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, format=None):
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({'error': 'No file provided in field "file".'}, status=status.HTTP_400_BAD_REQUEST)

        # call importer - do not attempt snapshot update (set False)
        summary = process_master_screening_v2(
            uploaded_file,
            update_snapshot=False,
            uploaded_by=request.user if hasattr(request, 'user') else None,
            save_file_to_job=True
        )
        if 'error' in summary:
            return Response(summary, status=status.HTTP_400_BAD_REQUEST)
        return Response(summary, status=status.HTTP_200_OK)


def _distinct_non_null_count(queryset, field_name):
    """
    Return distinct count for field_name excluding NULL/empty strings.
    """
    return queryset.exclude(**{f"{field_name}__isnull": True}).exclude(**{f"{field_name}": ""}).values(field_name).distinct().count()

class DashboardSummaryAPIView(APIView):
    """
    GET -> returns total_companies, total_countries, total_sectors, total_industries
    """
    def get(self, request, *args, **kwargs):
        qs = Company.objects.all()
        total_companies = qs.count()
        total_countries = _distinct_non_null_count(qs, 'headquarters_country_region')
        total_sectors = _distinct_non_null_count(qs, 'primary_sector')
        total_industries = _distinct_non_null_count(qs, 'primary_industry')

        data = {
            'total_companies': total_companies,
            'total_countries': total_countries,
            'total_sectors': total_sectors,
            'total_industries': total_industries
        }
        serializer = DashboardSummarySerializer(data)
        return Response(serializer.data)


class CountryListAPIView(APIView):
    """
    GET -> returns list of countries with company_count,
           total_countries, and total_companies
    """

    MAIN_COUNTRIES = {"United States", "Canada", "Australia", "United Kingdom"}

    def get(self, request, *args, **kwargs):
        # Exclude null/blank entries
        qs = Company.objects.exclude(headquarters_country_region__isnull=True).exclude(headquarters_country_region__exact="")

        # Total companies (after filter)
        total_companies = qs.count()

        # Group by country with company count
        country_counts = qs.values('headquarters_country_region') \
            .annotate(company_count=Count('id')) \
            .order_by('-company_count', 'headquarters_country_region')

        # Prepare main countries and others
        main_countries = []
        others_count = 0

        for row in country_counts:
            country = row['headquarters_country_region']
            count = row['company_count']

            if country in self.MAIN_COUNTRIES:
                main_countries.append({'name': country, 'company_count': count})
            else:
                others_count += count

        # Sort main countries in desired order
        country_order = ["United States", "Canada", "Australia", "United Kingdom"]
        sorted_main = sorted(main_countries, key=lambda x: country_order.index(x['name']))

        # Add "Others"
        if others_count > 0:
            sorted_main.append({'name': "Others", 'company_count': others_count})

        total_countries = len(country_counts)

        return Response({
            'total_companies': total_companies,
            'total_countries': total_countries,
            'countries': sorted_main
        })


class SectorListAPIView(APIView):
    """
    GET -> returns list of primary_sector with company_count, 
           total_sectors, and total_companies
    """
    def get(self, request, *args, **kwargs):
        # Filter out entries with null/empty primary_sector
        qs = Company.objects.exclude(primary_sector__isnull=True).exclude(primary_sector__exact="")

        # Total companies with valid primary_sector
        total_companies = qs.count()

        # Group by sector with counts
        sector_counts = qs.values('primary_sector') \
            .annotate(company_count=Count('id')) \
            .order_by('-company_count', 'primary_sector')

        results = [{'name': row['primary_sector'], 'company_count': row['company_count']} for row in sector_counts]

        return Response({
            'total_companies': total_companies,
            'total_sectors': len(results),
            'sectors': results
        })


class IndustryListAPIView(APIView):
    """
    GET -> returns list of primary_industry with company_count,
           total_industries, and total_companies
    """
    def get(self, request, *args, **kwargs):
        all_companies = Company.objects.all()
        total_companies = all_companies.count()

        # Only companies with a valid primary_industry
        industry_qs = all_companies.exclude(primary_industry__isnull=True).exclude(primary_industry__exact="")

        # Group by industry
        industry_counts = industry_qs.values('primary_industry') \
            .annotate(company_count=Count('id')) \
            .order_by('-company_count', 'primary_industry')

        results = [{'name': row['primary_industry'], 'company_count': row['company_count']} for row in industry_counts]

        return Response({
            'total_companies': total_companies,
            'total_industries': len(results),
            'industries': results
        })

# class StandardResultsSetPagination(PageNumberPagination):
#     page_size = 50
#     page_size_query_param = 'page_size'
#     max_page_size = 500

# def _get_decimal(value):
#     if value is None or value == '':
#         return None
#     try:
#         return Decimal(str(value))
#     except (InvalidOperation, ValueError):
#         return None

def _get_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

def _db_regex_for_exact_phrase(phrase: str) -> str:
    """
    Postgres regex for an exact, whole-word phrase match.
    Uses \m ... \M word boundaries around the full phrase.
    """
    # Normalize like we do for text, but only collapse spaces; keep letters as-is
    norm = _normalize_text_for_matching(phrase)
    # Escape phrase for regex; allow single spaces only (since we collapsed)
    return r"\m" + re.escape(norm) + r"\M"

def _py_regex_for_exact_phrase(phrase: str) -> re.Pattern:
    """
    Python regex for an exact, whole-word phrase match (case-insensitive).
    """
    norm = _normalize_text_for_matching(phrase)
    # \b around the entire phrase to enforce whole-word boundaries on both ends
    pat = r"\b" + re.escape(norm) + r"\b"
    return re.compile(pat, flags=re.IGNORECASE)

def _text_contains_exact_phrase(text: str, phrase: str) -> bool:
    """
    Exact phrase anywhere in the text (not sentence-scoped).
    Word boundaries enforced on both ends, case-insensitive.
    """
    if not text or not phrase:
        return False
    text_n = _normalize_text_for_matching(text)
    rgx = _py_regex_for_exact_phrase(phrase)
    return bool(rgx.search(text_n))

# helper to build a Q for a list of terms with AND/OR between those terms
def _build_q_for_terms(terms, condition='OR', field='business_description'):
    """
    terms: list[str] (words or quoted phrases)
    condition: 'AND' | 'OR' | 'SAME_SENTENCE'
      - SAME_SENTENCE => require all words to appear within the same sentence
    field: model field name
    Returns: Q object or None
    """
    if not terms:
        return None

    terms = [t.strip() for t in terms if t and t.strip()]
    if not terms:
        return None

    db_engine = (connection.settings_dict.get('ENGINE', '') or '').lower()
    is_postgres = 'postgres' in db_engine or 'psycopg2' in db_engine
    is_mysql = 'mysql' in db_engine or 'mariadb' in db_engine

    if condition == 'SAME_SENTENCE':
        MAX_SPAN = 300
        MAX_LOOKAHEAD_WORDS = 5

        if len(terms) == 1:
            return Q(**{f"{field}__icontains": terms[0]})

        if len(terms) == 2:
            a, b = re.escape(terms[0]), re.escape(terms[1])
            pattern = rf'(\b{a}\b[^\.\?\!\n\r]{{0,{MAX_SPAN}}}\b{b}\b)|(\b{b}\b[^\.\?\!\n\r]{{0,{MAX_SPAN}}}\b{a}\b)'
            if is_postgres:
                return Q(**{f"{field}__iregex": pattern})
            elif is_mysql:
                try:
                    return Q(**{f"{field}__regex": pattern})
                except Exception:
                    return Q(**{f"{field}__icontains": terms[0]}) & Q(**{f"{field}__icontains": terms[1]})
            else:
                try:
                    return Q(**{f"{field}__iregex": pattern})
                except Exception:
                    return Q(**{f"{field}__icontains": terms[0]}) & Q(**{f"{field}__icontains": terms[1]})

        if 2 < len(terms) <= MAX_LOOKAHEAD_WORDS:
            escaped = [re.escape(w) for w in terms]
            lookaheads = ''.join(r'(?=[^\.\?\!\n\r]{0,' + str(MAX_SPAN) + r'}\b' + w + r'\b)' for w in escaped)
            pattern = r'(' + lookaheads + r'[^\.\?\!\n\r]{0,' + str(MAX_SPAN) + r'})'
            if is_postgres:
                return Q(**{f"{field}__iregex": pattern})
            elif is_mysql:
                try:
                    return Q(**{f"{field}__regex": pattern})
                except Exception:
                    q_total = None
                    for term in terms:
                        sub_q = Q(**{f"{field}__icontains": term})
                        q_total = sub_q if q_total is None else (q_total & sub_q)
                    return q_total
            else:
                q_total = None
                for term in terms:
                    sub_q = Q(**{f"{field}__icontains": term})
                    q_total = sub_q if q_total is None else (q_total & sub_q)
                return q_total

        # fallback for too many words
        q_total = None
        for term in terms:
            sub_q = Q(**{f"{field}__icontains": term})
            q_total = sub_q if q_total is None else (q_total & sub_q)
        return q_total

    # Normal AND/OR/phrase handling
    q_total = None
    for term in terms:
        if not term:
            continue
        is_quoted = (len(term) >= 2) and (term[0] == term[-1] and term[0] in ("'", '"'))
        if is_quoted:
            t = term[1:-1].strip()
            if not t:
                continue
            sub_q = Q(**{f"{field}__icontains": t})
        else:
            sub_q = Q(**{f"{field}__icontains": term})

        if q_total is None:
            q_total = sub_q
        else:
            if condition == 'AND':
                q_total &= sub_q
            else:
                q_total |= sub_q
    return q_total

_stemmer = PorterStemmer()

def _normalize_text_for_matching(s: str) -> str:
    """Normalize unicode, decode HTML entities, replace NBSP and weird spaces, and trim."""
    if not s:
        return ""
    # Unicode normalization
    s = unicodedata.normalize('NFKC', s)
    # HTML entity decode
    s = html.unescape(s)
    # Replace non-breaking spaces and other odd spaces with normal space
    s = re.sub(r'[\u00A0\u2000-\u200B\u202F\u205F\u3000]', ' ', s)
    # Collapse repeated whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _sentence_matches_phrase(description: str, words: list[str]) -> bool:
    """
    Return True if ALL words (after stemming) are present within the same sentence
    of `description`. Sentences are split on `. ? !` or newline. Uses normalization.
    """
    if not description or not words:
        return False

    # normalize description first
    description = _normalize_text_for_matching(description)

    # prepare stems for query words
    q_stems = [ _stemmer.stem(w.lower()) for w in words if w and w.strip() ]
    if not q_stems:
        return False

    # split into sentences (note: we keep punctuation boundaries)
    sentences = re.split(r'(?<=[\.\?\!])\s+|\r?\n+', description)
    for s in sentences:
        # robust tokenization: letters/numbers plus apostrophes inside words
        raw_tokens = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", s.lower())
        # split hyphenated tokens
        tokens = []
        for t in raw_tokens:
            if '-' in t:
                tokens.extend([p for p in re.split(r'[-]', t) if p])
            else:
                tokens.append(t)
        if not tokens:
            continue
        s_stems = set(_stemmer.stem(tok) for tok in tokens)
        if all(qs in s_stems for qs in q_stems):
            return True
    return False

def _stem_prefilter_q_for_words(field, words):
    """
    Build a Q that matches rows where each word (by stem) appears somewhere.
    For each word we create a regex like r'\b<stem>\w*\b' so 'management' -> stem 'manag' matches 'manage','management','managing'.
    For multiple words we AND the per-word Qs.
    """
    db_engine = (connection.settings_dict.get('ENGINE', '') or '').lower()
    is_postgres = 'postgres' in db_engine or 'psycopg2' in db_engine
    is_mysql = 'mysql' in db_engine or 'mariadb' in db_engine

    q_total = None
    for w in words:
        if not w or not w.strip():
            continue
        stem = _stemmer.stem(w.lower())
        # safe regex: word boundary, stem, then zero-or-more word chars (matches variations)
        pattern = rf'\b{re.escape(stem)}\w*\b'

        # prefer DB regexes (case-insensitive). Postgres: __iregex. MySQL: __regex (collation may control case).
        sub_q = None
        if is_postgres:
            sub_q = Q(**{f"{field}__iregex": pattern})
        elif is_mysql:
            # Use (?i) for case-insensitive if supported, otherwise rely on collation
            try:
                sub_q = Q(**{f"{field}__regex": '(?i)' + pattern})
            except Exception:
                sub_q = Q(**{f"{field}__regex": pattern})
        else:
            # fallback: use icontains on the stem (less precise but works on sqlite or weird backends)
            sub_q = Q(**{f"{field}__icontains": stem})

        q_total = sub_q if q_total is None else (q_total & sub_q)

    return q_total

def _cache_key_for_compare(company_id_or_desc: str, compare_desc: str) -> str:
    key_src = f"{company_id_or_desc}||{compare_desc}"
    return "ai_cmp:" + hashlib.sha256(key_src.encode('utf-8')).hexdigest()

# wrapper to call the AI with safe handling
def _safe_call_openai_compare(company_desc: str, compare_desc: str):
    """
    Calls call_openai_compare and returns dict or None on failure.
    Keeps wrapper generic — exceptions are swallowed and return None.
    """
    try:
        out = call_openai_compare(company_desc, compare_desc)
        if not isinstance(out, dict):
            return None
        return {
            "similarity": out.get("similarity"),
            "rationale": out.get("rationale"),
        }
    except Exception:
        # You should log exception in production
        return None

def _prefix_for_word(w: str) -> str:
    """
    Prefer a morphological stem via Porter; fallback to the 60% rule.
    Guarantees >=4 chars when possible for precision.
    """
    w = (w or "").strip().lower()
    if not w:
        return ""
    stem = _porter_stem(w)
    # safety: some very short words stem tiny; fall back to heuristic
    base = stem if len(stem) >= 3 else w
    plen = max(4, int(math.ceil(len(base) * 0.6)))
    return base[:plen]

def _db_regex_for_word_variant(prefix: str) -> str:
    """
    Postgres regex using word boundaries. Prefix is already a stemmed base.
    Matches: \m{base}\w*\M
    """
    return r"\m" + re.escape(prefix) + r"\w*\M"

def _py_regex_for_word_variant(prefix: str) -> re.Pattern:
    """
    Python regex for word variants with word boundaries.
    """
    pat = r"\b" + re.escape(prefix) + r"\w*\b"
    return re.compile(pat, flags=re.IGNORECASE)

def _safe_phrase_prefilter(field: str, words: list[str]) -> Q:
    """
    PREFILTER used to narrow queryset before precise same-sentence check.
    - On Postgres: AND-chain of iregex on variant-friendly prefixes.
    - On other DBs: AND-chain of icontains on the prefix (best-effort).
    """
    words = [w for w in (words or []) if w.strip()]
    if not words:
        return Q()  # no-op

    if connection.vendor == "postgresql":
        q = None
        for w in words:
            prefix = _prefix_for_word(w)
            if not prefix:
                continue
            pattern = _db_regex_for_word_variant(prefix)
            sub = Q(**{f"{field}__iregex": pattern})
            q = sub if q is None else (q & sub)
        return q or Q()
    else:
        # Best-effort portable fallback: AND across prefix icontains
        q = None
        for w in words:
            prefix = _prefix_for_word(w)
            if not prefix:
                continue
            sub = Q(**{f"{field}__icontains": prefix})
            q = sub if q is None else (q & sub)
        return q or Q()

def _sentence_matches_phrase_fuzzy(text: str, words: list[str]) -> bool:
    if not text or not words:
        return False

    # Normalize once for robust matching across unicode/nbsp/entities
    text = _normalize_text_for_matching(text)

    # Split into sentences
    sentences = re.split(r'(?<=[\.\!\?\u2026])\s+', text)

    # Compile once per word (stem-aware prefix)
    regexes = []
    for w in words:
        prefix = _prefix_for_word(w)
        if not prefix:
            return False
        regexes.append(_py_regex_for_word_variant(prefix))

    for sent in sentences:
        if all(rgx.search(sent) for rgx in regexes):
            return True
    return False

_vowels = set("aeiou")

def _is_consonant(word, i):
    ch = word[i]
    if ch in _vowels: 
        return False
    if ch == 'y':
        return i == 0 or not _is_consonant(word, i-1)
    return True

def _m(word):
    # measure of VC sequences
    m = 0; i = 0; L = len(word)
    while i < L:
        while i < L and _is_consonant(word, i): i += 1
        if i >= L: break
        while i < L and not _is_consonant(word, i): i += 1
        m += 1
    return m

def _vowel_in_stem(word):
    return any(not _is_consonant(word, i) for i in range(len(word)))

def _ends(word, sfx):
    return word.endswith(sfx)

def _setto(word, sfx):
    return word[: -len(sfx[0])] + sfx[1]

def _cvc(word):
    if len(word) < 3: return False
    c1 = _is_consonant(word, -1)
    v  = not _is_consonant(word, -2)
    c2 = _is_consonant(word, -3)
    if not (c2 and v and c1): return False
    return word[-1] not in "wxy"

def _step1ab(w):
    # Step 1a
    if _ends(w, "sses"): w = w[:-2]        # sses -> ss
    elif _ends(w, "ies"): w = w[:-2]       # ies -> i
    elif _ends(w, "ss"): pass              # ss -> ss
    elif _ends(w, "s"): w = w[:-1]         # s -> ""
    # Step 1b
    flag = False
    if _ends(w, "eed"):
        if _m(w[:-3]) > 0:
            w = w[:-1]  # eed -> ee
    elif (_ends(w, "ed") and _vowel_in_stem(w[:-2])):
        w = w[:-2]; flag = True
    elif (_ends(w, "ing") and _vowel_in_stem(w[:-3])):
        w = w[:-3]; flag = True
    if flag:
        if _ends(w, "at") or _ends(w, "bl") or _ends(w, "iz"):
            w += "e"
        elif len(w) >= 2 and w[-1] == w[-2] and w[-1] not in "lsz":
            w = w[:-1]
        elif _m(w) == 1 and _cvc(w):
            w += "e"
    return w

def _step1c(w):
    if _ends(w, "y") and _vowel_in_stem(w[:-1]):
        return w[:-1] + "i"
    return w

def _step2(w):
    reps = {
        "ational":"ate","tional":"tion","enci":"ence","anci":"ance","izer":"ize",
        "abli":"able","alli":"al","entli":"ent","eli":"e","ousli":"ous","ization":"ize",
        "ation":"ate","ator":"ate","alism":"al","iveness":"ive","fulness":"ful","ousness":"ous",
        "aliti":"al","iviti":"ive","biliti":"ble","logi":"log"
    }
    for k,v in reps.items():
        if _ends(w, k) and _m(w[:-len(k)])>0:
            return w[:-len(k)]+v
    return w

def _step3(w):
    reps = {
        "icate":"ic","ative":"","alize":"al","iciti":"ic","ical":"ic","ful":"","ness":""
    }
    for k,v in reps.items():
        if _ends(w, k) and _m(w[:-len(k)])>0:
            return w[:-len(k)]+v
    return w

def _step4(w):
    sfxes = ["al","ance","ence","er","ic","able","ible","ant","ement","ment","ent",
             "sion","tion","ou","ism","ate","iti","ous","ive","ize"]
    for k in sfxes:
        if _ends(w, k):
            base = w[:-len(k)]
            if (k in ("sion","tion") and _m(base)>1) or (k not in ("sion","tion") and _m(base)>1):
                return base
    return w

def _step5(w):
    if _ends(w, "e"):
        base = w[:-1]
        if _m(base)>1 or (_m(base)==1 and not _cvc(base)):
            w = base
    if _m(w)>1 and _ends(w, "ll"):
        w = w[:-1]
    return w

def _porter_stem(word: str) -> str:
    w = (word or "").strip().lower()
    if len(w) <= 2: return w
    w = _step1ab(w)
    w = _step1c(w)
    w = _step2(w)
    w = _step3(w)
    w = _step4(w)
    w = _step5(w)
    return w

def _get_date(raw):
    """
    Return datetime.date or None.
    Accepts:
      - '08-03-2000', '08/03/2000' (day-first),
      - '2000-03-08',
      - Excel serial numbers (int/float),
      - pandas-friendly strings if pandas present.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Excel serial numbers
    try:
        # int-like or float-like
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            val = float(s)
            if not math.isnan(val):
                base = datetime.date(1899, 12, 30)  # Windows base (handles 1900 bug)
                return base + datetime.timedelta(days=int(val))
    except Exception:
        pass

    # Try pandas if available (handles many cases); parse as day-first
    try:
        import pandas as pd
        ts = pd.to_datetime(s, dayfirst=True, errors='coerce')
        if pd.notna(ts):
            return ts.date()
    except Exception:
        pass

    # Common explicit fallbacks
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            continue

    return None

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 200
    page_size_query_param = 'page_size'
    max_page_size = 500

class CompanyListAPIView(generics.ListAPIView):
    serializer_class = CompanySerializer
    pagination_class = StandardResultsSetPagination

    # @method_decorator(cache_page(60 * 2))
    # def get(self, request, *args, **kwargs):
    #     return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs_companies = Company.objects.all()

        # Company-level filters
        raw_countries = self.request.GET.getlist('headquarters_country_region') or self.request.GET.getlist('country')
        raw_sectors = self.request.GET.getlist('primary_sector')
        raw_industries = self.request.GET.getlist('primary_industry')


        if not raw_countries:
            single = self.request.GET.get('headquarters_country_region') or self.request.GET.get('country')
            if single:
                # split on comma/semicolon/pipe and strip whitespace
                raw_countries = [c.strip() for c in re.split(r'[;,|]+', single) if c.strip()]

        # Now raw_countries is a list like ['India', 'USA']
        if raw_countries:
            q_country = Q()
            for c in raw_countries:
                # case-insensitive exact match
                q_country |= Q(headquarters_country_region__iexact=c)
            qs_companies = qs_companies.filter(q_country)
        if not raw_sectors:
            single = self.request.GET.get('primary_sector')
            if single:
                raw_sectors = [s.strip() for s in re.split(r'[;,|]+', single) if s.strip()]

        if not raw_industries:
            single = self.request.GET.get('primary_industry')
            if single:
                raw_industries = [i.strip() for i in re.split(r'[;,|]+', single) if i.strip()]

        if raw_sectors:
            q_sector = Q()
            for s in raw_sectors:
                q_sector |= Q(primary_sector__icontains=s)
            qs_companies = qs_companies.filter(q_sector)

        if raw_industries:
            q_industry = Q()
            for i in raw_industries:
                q_industry |= Q(primary_industry__icontains=i)
            qs_companies = qs_companies.filter(q_industry)

        fpd_min = _get_date(self.request.GET.get('first_pricing_date_min'))
        fpd_max = _get_date(self.request.GET.get('first_pricing_date_max'))
        if fpd_min is not None:
            qs_companies = qs_companies.filter(first_pricing_date__gte=fpd_min)
        if fpd_max is not None:
            qs_companies = qs_companies.filter(first_pricing_date__lte=fpd_max)

        # Financial filters target the 'latest' period
        fr_q = Q(period='latest')

        ev_rev_min = _get_decimal(self.request.GET.get('ev_revenu_min'))
        ev_rev_max = _get_decimal(self.request.GET.get('ev_revenu_max'))
        if ev_rev_min is not None:
            fr_q &= Q(ev_revenu__gte=ev_rev_min)
        if ev_rev_max is not None:
            fr_q &= Q(ev_revenu__lte=ev_rev_max)
            
        ev_ebitda_min = _get_decimal(self.request.GET.get('ev_ebitda_min'))
        ev_ebitda_max = _get_decimal(self.request.GET.get('ev_ebitda_max'))
        if ev_ebitda_min is not None:
            fr_q &= Q(ev_ebitda__gte=ev_ebitda_min)
        if ev_ebitda_max is not None:
            fr_q &= Q(ev_ebitda__lte=ev_ebitda_max)


        total_rev_min = _get_decimal(self.request.GET.get('total_revenue_min'))
        total_rev_max = _get_decimal(self.request.GET.get('total_revenue_max'))
        if total_rev_min is not None:
            fr_q &= Q(total_revenue__gte=total_rev_min)
        if total_rev_max is not None:
            fr_q &= Q(total_revenue__lte=total_rev_max)

        entval_min = _get_decimal(self.request.GET.get('enterprise_value_min'))
        entval_max = _get_decimal(self.request.GET.get('enterprise_value_max'))
        if entval_min is not None:
            fr_q &= Q(enterprise_value__gte=entval_min)
        if entval_max is not None:
            fr_q &= Q(enterprise_value__lte=entval_max)

        any_fin_filter = any([
            ev_rev_min, ev_rev_max,
            ev_ebitda_min, ev_ebitda_max,
            total_rev_min, total_rev_max,
            entval_min, entval_max
        ])

        # Always order so the first prefetched record is the one we want to expose
        latest_qs = FinancialRecord.objects.filter(fr_q).order_by('-created_at')

        if any_fin_filter:
            # Filter companies by those with a matching latest record
            company_ids = latest_qs.values_list('company_id', flat=True)
            qs_companies = qs_companies.filter(id__in=company_ids).prefetch_related(
                Prefetch('records', queryset=latest_qs, to_attr='matched_records')
            )
        else:
            qs_companies = qs_companies.prefetch_related(
                Prefetch(
                    'records',
                    queryset=FinancialRecord.objects.filter(period='latest').order_by('-created_at'),
                    to_attr='matched_records'
                )
            )

        # ---------------------------
        # Keyword handling (new)
        # Supports:
        #  - repeated `keywords` params with parallel repeated `keyword_condition` params (index-paired)
        #    Example (Postman Params): keyword=a&keyword=b & keyword_condition=AND & keyword_condition=OR
        #  - repeated `keyword_group` params of form "term1,term2|AND" - safer/explicit
        #    Example: ?keyword_group=Harshil,Mehta|AND&keyword_group=Python,Developer|OR
        #  - fallback legacy single `keywords` param (csv) + keyword_condition
        # final combination of groups uses `group_operator` (default AND)
        # ---------------------------

        group_objects = []  # each entry: {"type": "SAME_SENTENCE" or "OTHER", "words": [...], "q": Q}

        # helper to create a simple Q for a list of words (AND icontains)
        def _q_and_icontains(field, words):
            q = None
            for w in words:
                sub = Q(**{f"{field}__icontains": w})
                q = sub if q is None else (q & sub)
            return q

        def _add_group_from_raw(raw_val, combine_with_prev=None):
            raw_val = raw_val.strip()
            if not raw_val:
                return

            # NEW: detect a single, double-quoted phrase => EXACT_PHRASE group
            # Accept " phrase " with surrounding quotes only if both ends are quoted
            if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] == '"':
                phrase = raw_val[1:-1].strip()
                if phrase:
                    # Prefilter: quick icontains (works on any DB); on Postgres prefer word-boundary regex
                    if connection.vendor == "postgresql":
                        pattern = _db_regex_for_exact_phrase(phrase)
                        q_pref = Q(**{'business_description__iregex': pattern})
                    else:
                        q_pref = Q(**{'business_description__icontains': phrase})
                    group_objects.append({
                        "type": "EXACT_PHRASE",
                        "phrase": phrase,
                        "q": q_pref,
                        "combine_with_prev": combine_with_prev
                    })
                return

            # If it's a single space-delimited phrase (no list separators), treat as SAME_SENTENCE fuzzy group
            if ' ' in raw_val and ',' not in raw_val and ';' not in raw_val and '|' not in raw_val:
                words = [w.strip() for w in re.split(r'\s+', raw_val) if w.strip()]
                if words:
                    q_loose = _safe_phrase_prefilter('business_description', words)
                    group_objects.append({
                        "type": "SAME_SENTENCE",
                        "words": words,
                        "q": q_loose,
                        "combine_with_prev": combine_with_prev
                    })
                return

            # Otherwise it might be a CSV/semicolon/pipe list; split and handle individually
            parts = [p.strip() for p in re.split(r'[;,|]+', raw_val) if p.strip()]
            for p in parts:
                if len(p) >= 2 and p[0] == p[-1] == '"':
                    phrase = p[1:-1].strip()
                    if phrase:
                        if connection.vendor == "postgresql":
                            pattern = _db_regex_for_exact_phrase(phrase)
                            q_pref = Q(**{'business_description__iregex': pattern})
                        else:
                            q_pref = Q(**{'business_description__icontains': phrase})
                        group_objects.append({
                            "type": "EXACT_PHRASE",
                            "phrase": phrase,
                            "q": q_pref,
                            "combine_with_prev": combine_with_prev
                        })
                elif ' ' in p:
                    words = [w.strip() for w in re.split(r'\s+', p) if w.strip()]
                    if words:
                        q_loose = _safe_phrase_prefilter('business_description', words)
                        group_objects.append({
                            "type": "SAME_SENTENCE",
                            "words": words,
                            "q": q_loose,
                            "combine_with_prev": combine_with_prev
                        })
                else:
                    group_objects.append({
                        "type": "OTHER",
                        "words": [p],
                        "q": Q(**{'business_description__icontains': p}),
                        "combine_with_prev": combine_with_prev
                    })

        kw_cond_list = [c.strip().upper() for c in self.request.GET.getlist('keyword_condition') if c.strip()]
        if not kw_cond_list:
            kw_cond_list = [c.strip().upper() for c in self.request.GET.getlist('keyword_conditions') if c.strip()]

        kw_list = self.request.GET.getlist('keywords') or self.request.GET.getlist('keyword')
        for idx, raw in enumerate(kw_list):
            if not raw:
                continue
            combine_with_prev = kw_cond_list[idx] if idx < len(kw_cond_list) else None
            for chunk in [c.strip() for c in re.split(r'[;,]+', raw) if c.strip()]:
                _add_group_from_raw(chunk, combine_with_prev=combine_with_prev)

        for raw_group in self.request.GET.getlist('keyword_group'):
            if not raw_group:
                continue
            combine_with_prev = None
            if '|' in raw_group:
                terms_part, cond_part = raw_group.rsplit('|', 1)
                combine_with_prev = cond_part.strip().upper() or None
            else:
                terms_part = raw_group
            for chunk in [c.strip() for c in re.split(r'[;,]+', terms_part) if c.strip()] :
                _add_group_from_raw(chunk, combine_with_prev=combine_with_prev)

        if not group_objects:
            legacy = self.request.GET.get('keywords') or self.request.GET.get('keyword')
            if legacy:
                for chunk in [c.strip() for c in re.split(r'[;,]+', legacy) if c.strip()] :
                    _add_group_from_raw(chunk, combine_with_prev=None)

        if not group_objects:
            final_keyword_q = None
        else:
            # Unified pipeline for AND/OR:
            # 1) Prefilter (OR of all groups' quick DB filters) to shrink candidates
            prefilter_q = None
            for g in group_objects:
                if g.get('q') is None:
                    continue
                prefilter_q = g['q'] if prefilter_q is None else (prefilter_q | g['q'])

            candidate_qs = qs_companies.filter(prefilter_q) if prefilter_q is not None else qs_companies

            # 2) Materialize ids for verification groups
            #    - SAME_SENTENCE: fuzzy same-sentence word presence (existing behavior) — used for AND *and* OR
            #    - EXACT_PHRASE: strict whole-phrase match (no stemming) — used for AND *and* OR
            same_sentence_groups = []
            exact_phrase_groups = []
            for g in group_objects:
                if g['type'] == 'SAME_SENTENCE':
                    same_sentence_groups.append(g)
                    g['_index'] = len(same_sentence_groups) - 1  # record index
                elif g['type'] == 'EXACT_PHRASE':
                    exact_phrase_groups.append(g)
                    g['_index'] = len(exact_phrase_groups) - 1

            same_id_sets = [set() for _ in same_sentence_groups] if same_sentence_groups else []
            exact_id_sets = [set() for _ in exact_phrase_groups] if exact_phrase_groups else []

            if same_sentence_groups or exact_phrase_groups:
                for comp in candidate_qs.only('id', 'business_description'):
                    desc = comp.business_description or ""
                    # SAME_SENTENCE verification
                    for i, g in enumerate(same_sentence_groups):
                        if _sentence_matches_phrase_fuzzy(desc, g['words']):
                            same_id_sets[i].add(comp.id)
                    # EXACT_PHRASE verification
                    for i, g in enumerate(exact_phrase_groups):
                        if _text_contains_exact_phrase(desc, g['phrase']):
                            exact_id_sets[i].add(comp.id)

            # 3) Build final Q by combining groups in order, honoring AND/OR
            global_group_operator = (self.request.GET.get('group_operator') or 'OR').strip().upper()
            final_keyword_q = None
            for g in group_objects:
                if g['type'] == 'SAME_SENTENCE':
                    ids = same_id_sets[g['_index']] if same_id_sets else set()
                    q_part = Q(id__in=list(ids)) if ids else Q(id__in=[])
                elif g['type'] == 'EXACT_PHRASE':
                    ids = exact_id_sets[g['_index']] if exact_id_sets else set()
                    q_part = Q(id__in=list(ids)) if ids else Q(id__in=[])
                else:
                    q_part = g['q']

                if final_keyword_q is None:
                    final_keyword_q = q_part
                else:
                    op = (g.get('combine_with_prev') or global_group_operator or 'OR')
                    if op == 'AND':
                        final_keyword_q &= q_part
                    else:
                        final_keyword_q |= q_part

            if final_keyword_q is not None:
                qs_companies = qs_companies.filter(final_keyword_q)

        return qs_companies

    def _parse_extra_companies(self, request):
        """
        Returns list of dicts: [{"name": "...", "description": "..."}, ...]
        Accepts:
         - extra_company_name + extra_company_description (single pair)
         - extra_companies = JSON list string
        """
        extras = []
        # single-pair
        name = request.GET.get('extra_company_name')
        desc = request.GET.get('extra_company_description')
        if name or desc:
            extras.append({"name": name or "", "description": desc or ""})

        # JSON list
        raw = request.GET.get('extra_companies')
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        extras.append({
                            "name": item.get("name", "") or "",
                            "description": item.get("description", "") or ""
                        })
            except Exception:
                # ignore malformed JSON - caller will get no extra entries
                pass
        return extras

    def list(self, request, *args, **kwargs):
        compare_desc = request.GET.get('compare_description')  # user's 3-4 lines to compare
        # parse potential ad-hoc companies to compare
        extra_companies = self._parse_extra_companies(request)

        queryset = self.get_queryset()
        # --- compute counts for the full filtered queryset (before pagination) ---
        country_count = queryset.values_list('headquarters_country_region', flat=True) \
            .exclude(headquarters_country_region__isnull=True) \
            .exclude(headquarters_country_region__exact="") \
            .distinct().count()

        sector_count = queryset.values_list('primary_sector', flat=True) \
            .exclude(primary_sector__isnull=True) \
            .exclude(primary_sector__exact="") \
            .distinct().count()

        industry_count = queryset.values_list('primary_industry', flat=True) \
            .exclude(primary_industry__isnull=True) \
            .exclude(primary_industry__exact="") \
            .distinct().count()

        counts_payload = {
            "countries": country_count,
            "sectors": sector_count,
            "industries": industry_count,
        }

        page = self.paginate_queryset(queryset)
        # configuration you can tune
        MAX_WORKERS = 6           # number of concurrent model calls
        PER_FUTURE_TIMEOUT = 10   # seconds per model call
        CACHE_TTL = 60 * 60 * 6   # 6 hours cache TTL

        def _populate_ai_for_list(items):
            """
            items: iterable of company instances (page or queryset)
            Attaches _ai_similarity and _ai_rationale to each company object.
            Uses cache + ThreadPoolExecutor + per-call fallback.
            This version ensures we process all items by materializing to list and batching.
            """
            # Materialize to list to avoid lazy-QuerySet partial iteration issues
            items_list = list(items)
            if not items_list:
                return

            # tuning knobs
            BATCH_SIZE = 100         # process this many companies per thread-batch (tuneable)
            MAX_WORKERS = 6          # threads per batch
            PER_FUTURE_TIMEOUT = 10  # seconds per model call
            CACHE_TTL = 60 * 60 * 6  # 6 hours

            def _safe_call(company_desc, compare_desc):
                try:
                    out = call_openai_compare(company_desc, compare_desc)
                    if not isinstance(out, dict):
                        return None
                    return {"similarity": out.get("similarity"), "rationale": out.get("rationale")}
                except Exception:
                    return None

            compare_desc_local = compare_desc  # from outer scope in list(); safe capture

            # process in batches
            for i in range(0, len(items_list), BATCH_SIZE):
                batch = items_list[i:i + BATCH_SIZE]
                pending = []

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    # first check cache for each comp and schedule only those missing
                    for comp in batch:
                        comp._ai_similarity = None
                        comp._ai_rationale = None

                        key_id = getattr(comp, "id", None) or (comp.business_description or "")
                        ck = _cache_key_for_compare(str(key_id), compare_desc_local)
                        cached = cache.get(ck) if compare_desc_local else None
                        if cached is not None:
                            comp._ai_similarity = cached.get("similarity")
                            comp._ai_rationale = cached.get("rationale")
                            continue

                        # schedule call
                        future = ex.submit(_safe_call, comp.business_description or "", compare_desc_local)
                        pending.append((comp, future, ck))

                    # collect results for this batch
                    for comp, fut, ck in pending:
                        try:
                            res = fut.result(timeout=PER_FUTURE_TIMEOUT)
                        except Exception:
                            res = None

                        if res:
                            comp._ai_similarity = res.get("similarity")
                            comp._ai_rationale = res.get("rationale")
                            try:
                                cache.set(ck, {"similarity": comp._ai_similarity, "rationale": comp._ai_rationale}, CACHE_TTL)
                            except Exception:
                                pass
                        else:
                            comp._ai_similarity = None
                            comp._ai_rationale = None

        if page is not None:
            # attach latest record reference for serializer
            for comp in page:
                matched = getattr(comp, 'matched_records', None)
                comp._latest_record = matched[0] if matched else None

            # call AI (cached + parallel) only if compare_desc provided
            if compare_desc:
                _populate_ai_for_list(page)

            serializer = self.get_serializer(page, many=True)
            data = serializer.data

            # Inject AI fields into serialized data (so they are visible in API)
            if compare_desc:
                for idx, comp_obj in enumerate(page):
                    data[idx]["business_model_similarity"] = getattr(comp_obj, "_ai_similarity", None)
                    data[idx]["ai_rationale"] = getattr(comp_obj, "_ai_rationale", None)

            # Build extra comparisons for ad-hoc companies (if provided)
            extra_results = []
            if compare_desc and extra_companies:
                # these are ad-hoc entries; we can compute them synchronously but with same safe wrapper
                for extra in extra_companies:
                    comp_desc = extra.get("description") or ""
                    ai_out = _safe_call_openai_compare(comp_desc, compare_desc)
                    extra_results.append({
                        "name": extra.get("name") or None,
                        "description": comp_desc,
                        "business_model_similarity": ai_out.get("similarity") if ai_out else None,
                        "ai_rationale": ai_out.get("rationale") if ai_out else None
                    })

            # get the paginated response and then augment it
            resp = self.get_paginated_response(data)
            resp.data['counts'] = counts_payload
            if extra_results:
                resp.data['extra_comparisons'] = extra_results
            return resp

        # non-paginated path (same logic)
        for comp in queryset:
            matched = getattr(comp, 'matched_records', None)
            comp._latest_record = matched[0] if matched else None

        if compare_desc:
            _populate_ai_for_list(queryset)

        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data

        if compare_desc:
            for idx, comp_obj in enumerate(queryset):
                data[idx]["business_model_similarity"] = getattr(comp_obj, "_ai_similarity", None)
                data[idx]["ai_rationale"] = getattr(comp_obj, "_ai_rationale", None)

        # extra comparisons for non-paginated
        extra_results = []
        if compare_desc and extra_companies:
            for extra in extra_companies:
                comp_desc = extra.get("description") or ""
                ai_out = _safe_call_openai_compare(comp_desc, compare_desc)
                extra_results.append({
                    "name": extra.get("name") or None,
                    "description": comp_desc,
                    "business_model_similarity": ai_out.get("similarity") if ai_out else None,
                    "ai_rationale": ai_out.get("rationale") if ai_out else None
                })

        final_payload = {
            "results": data,
        }
        if extra_results:
            final_payload["extra_comparisons"] = extra_results
        return Response(final_payload, status=status.HTTP_200_OK)


    # def list(self, request, *args, **kwargs):
    #     queryset = self.get_queryset()
    #     page = self.paginate_queryset(queryset)
    #     if page is not None:
    #         for comp in page:
    #             matched = getattr(comp, 'matched_records', None)
    #             comp._latest_record = matched[0] if matched else None
    #         serializer = self.get_serializer(page, many=True)
    #         return self.get_paginated_response(serializer.data)

    #     for comp in queryset:
    #         matched = getattr(comp, 'matched_records', None)
    #         comp._latest_record = matched[0] if matched else None
    #     serializer = self.get_serializer(queryset, many=True)
    #     return Response(serializer.data)






MAX_DESC_CHARS = getattr(settings, "COMPARE_MAX_DESC_CHARS", 1200)
class CompareAPIView(APIView):
    """
    POST /api/companies/compare/
    Body: {"compare_description": "...", "companies": [{name, description}, ...]}
    Returns: list of results with similarity + rationale
    """

    def post(self, request, *args, **kwargs):
        serializer = CompareRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        compare_desc = serializer.validated_data["compare_description"]
        companies = serializer.validated_data.get("companies", [])

        results = []
        # Iterate sequentially (synchronous). call_openai_compare already handles exceptions.
        for comp in companies:
            name = comp.get("name") or None
            desc = (comp.get("description") or "")[:MAX_DESC_CHARS]  # trim for safety
            ai_out = call_openai_compare(desc, compare_desc)
            results.append({
                "name": name,
                "description": desc,
                "business_model_similarity": ai_out.get("similarity"),
                "ai_rationale": ai_out.get("rationale")
            })

        payload = {
            "compare_description": compare_desc,
            "results": results,
            "meta": {"company_count": len(results)}
        }
        return Response(payload, status=status.HTTP_200_OK)




















































# Below code Company showing 17437

# class IndustryListAPIView(APIView):
#     """
#     GET -> returns list of primary_industry with company_count, and total_industries
#     """
#     def get(self, request, *args, **kwargs):
#         # Filter valid industries
#         qs = Company.objects.exclude(primary_industry__isnull=True).exclude(primary_industry__exact="")

#         # Count total companies with a valid industry
#         total_companies = qs.count()

#         # Group by industry
#         industry_counts = qs.values('primary_industry') \
#             .annotate(company_count=Count('id')) \
#             .order_by('-company_count', 'primary_industry')

#         results = [{'name': row['primary_industry'], 'company_count': row['company_count']} for row in industry_counts]

#         return Response({
#             'total_companies': total_companies,
#             'total_industries': len(results),
#             'industries': results
#         })


# OPTIONAL: Drilldown endpoints to list actual companies inside a country / sector / industry
# class CompaniesByCountryAPIView(generics.ListAPIView):
#     serializer_class = None  # we'll return simple JSON; or replace with CompanySerializer
#     # If you have a CompanySerializer, set serializer_class = CompanySerializer
#     def get(self, request, country_name, *args, **kwargs):
#         qs = Company.objects.filter(country__iexact=country_name).order_by('name')
#         # If you have CompanySerializer, uncomment below and return serialized companies
#         serializer = CompanySerializer(qs, many=True)
#         return Response({
#             'country': country_name,
#             'company_count': qs.count(),
#             'companies': serializer.data
#         })


# class CompaniesBySectorAPIView(generics.ListAPIView):
#     def get(self, request, sector_name, *args, **kwargs):
#         qs = Company.objects.filter(primary_sector__iexact=sector_name).order_by('name')
#         from .serializers import CompanySerializer
#         serializer = CompanySerializer(qs, many=True)
#         return Response({
#             'sector': sector_name,
#             'company_count': qs.count(),
#             'companies': serializer.data
#         })


# class CompaniesByIndustryAPIView(generics.ListAPIView):
#     def get(self, request, industry_name, *args, **kwargs):
#         qs = Company.objects.filter(primary_industry__iexact=industry_name).order_by('name')
#         from .serializers import CompanySerializer
#         serializer = CompanySerializer(qs, many=True)
#         return Response({
#             'industry': industry_name,
#             'company_count': qs.count(),
#             'companies': serializer.data
#         })


# The rest of your list/detail views remain same but ensure serializer mapping includes new fields
# class CompanyListAPIView(generics.ListAPIView):
#     queryset = Company.objects.all().order_by('name')
#     serializer_class = CompanySerializer
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#     filterset_fields = ['company_id', 'name', 'exchange_ticker', 'country']
#     search_fields = ['name', 'company_id', 'exchange_ticker']
#     ordering_fields = ['name', 'latest_total_revenue', 'latest_market_cap']


# class CompanyDetailAPIView(generics.RetrieveAPIView):
#     queryset = Company.objects.all()
#     serializer_class = CompanySerializer
#     lookup_field = 'pk'


# class FinancialRecordListAPIView(generics.ListAPIView):
#     queryset = FinancialRecord.objects.select_related('company').all().order_by('-created_at')
#     serializer_class = FinancialRecordSerializer
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#     filterset_fields = ['period', 'company__company_id', 'company__name', 'company__exchange_ticker']
#     search_fields = ['company__name', 'company__company_id', 'company__exchange_ticker']
#     ordering_fields = ['created_at', 'market_cap', 'total_revenue']
