from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from django.db.models import Count

from .models import Transaction, UploadJob
from .serializers import DashboardSummarySerializer
from .utils_transaction_sheet import process_transaction_excel
from rest_framework import generics
from django.db.models import Q
from rest_framework.pagination import PageNumberPagination
import re
from .serializers import TransactionSerializer
from django.db import connection
from api.utils.openai_helpers import call_openai_compare
from api.views import _cache_key_for_compare, _safe_call_openai_compare
import math
import html
import unicodedata

# IMPORT ALL YOUR EXISTING HELPERS (reuse same file)
# from api.views import (
#     _build_q_for_terms,
#     _safe_phrase_prefilter,
#     _sentence_matches_phrase_fuzzy,
#     _text_contains_exact_phrase,
#     _db_regex_for_exact_phrase
# )

import threading

class ExcelUploadAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get("file")

        if not file:
            return Response({"error": "No file provided"}, status=400)

        # Save job first
        job = UploadJob.objects.create(
            uploaded_by=request.user if request.user.is_authenticated else None,
            filename=file.name,
            summary={"status": "processing"},
            file=file
        )

        # Run in background thread
        threading.Thread(
            target=self.process_file,
            args=(job.id,)
        ).start()

        return Response({
            "message": "File uploaded. Processing started.",
            "job_id": job.id
        })

    def process_file(self, job_id):
        job = UploadJob.objects.get(id=job_id)

        result = process_transaction_excel(job.file, job.uploaded_by, save_file_to_job=False)

        job.summary = result
        job.save()
    

class DashboardSummaryAPIView(APIView):

    def get(self, request):
        qs = Transaction.objects.all()

        # Updated countries queryset (group + count)
        countries_qs = qs.exclude(geography__isnull=True)\
                         .exclude(geography="")\
                         .values('geography')\
                         .annotate(country_count=Count('id'))\
                         .order_by('country_count', 'geography')

        countries = [
            {
                "name": row["geography"],
                "company_count": row["country_count"]
            }
            for row in countries_qs
        ]

        # Existing industries queryset (unchanged)
        industries_qs = qs.exclude(
            Q(primary_industry__isnull=True) |
            Q(primary_industry__exact="") |
            Q(primary_industry__in=["-", "N/A", "None", "Unknown"])
        ).values('primary_industry')\
        .annotate(company_count=Count('id'))\
        .order_by('-company_count', 'primary_industry')

        industries = [
            {
                "name": row["primary_industry"],
                "company_count": row["company_count"]
            }
            for row in industries_qs
        ]

        data = {
            "total_transactions": qs.count(),
            "total_countries": countries_qs.count(),  # still works
            "total_industries": len(industries),
            "countries": countries,  # now includes counts
            "industries": industries,
        }

        return Response(data)
    
class GeographyListAPIView(APIView):
    """
    Returns all unique geographies.
    """

    def get(self, request):
        geographies = (
            Transaction.objects
            .exclude(geography__isnull=True)
            .exclude(geography__exact="")
            .values_list("geography", flat=True)
            .distinct()
            .order_by("geography")
        )

        return Response(
            {
                "count": len(geographies),
                "results": list(geographies)
            },
            status=status.HTTP_200_OK
        )    
    
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 200
    page_size_query_param = 'page_size'
    max_page_size = 500

from decimal import Decimal, InvalidOperation

def _get_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    
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

def _db_regex_for_exact_phrase(phrase: str) -> str:
    r"""
    Postgres regex for an exact, whole-word phrase match.
    Uses \m ... \M word boundaries around the full phrase.
    """
    # Normalize like we do for text, but only collapse spaces; keep letters as-is
    norm = _normalize_text_for_matching(phrase)
    # Escape phrase for regex; allow single spaces only (since we collapsed)
    return r"\m" + re.escape(norm) + r"\M"

def _prefix_for_word(w: str) -> str:
    """
    Use Porter stem directly for consistent matching.
    Avoid over-trimming which breaks matches.
    """
    w = (w or "").strip().lower()
    if not w:
        return ""

    stem = _porter_stem(w)

    # fallback if stem too small
    if len(stem) >= 3:
        return stem

    return w

def _db_regex_for_word_variant(prefix: str) -> str:
    r"""
    Postgres regex using word boundaries. Prefix is already a stemmed base.
    Matches: \m{base}\w*\M
    """
    return r"\m" + re.escape(prefix) + r"\w*\M"

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

def _py_regex_for_exact_phrase(phrase: str) -> re.Pattern:
    """
    Python regex for an exact, whole-word phrase match (case-insensitive).
    """
    norm = _normalize_text_for_matching(phrase)
    # \b around the entire phrase to enforce whole-word boundaries on both ends
    pat = r"\b" + re.escape(norm) + r"\b"
    return re.compile(pat, flags=re.IGNORECASE)

def _py_regex_for_word_variant(prefix: str) -> re.Pattern:
    pat = r"\b" + re.escape(prefix) + r"\w*\b"
    return re.compile(pat, flags=re.IGNORECASE)

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

def _ends(word, sfx):
    return word.endswith(sfx)

def _vowel_in_stem(word):
    return any(not _is_consonant(word, i) for i in range(len(word)))

def _cvc(word):
    if len(word) < 3: return False
    c1 = _is_consonant(word, -1)
    v  = not _is_consonant(word, -2)
    c2 = _is_consonant(word, -3)
    if not (c2 and v and c1): return False
    return word[-1] not in "wxy"

def _m(word):
    # measure of VC sequences
    m = 0; i = 0; L = len(word)
    while i < L:
        while i < L and _is_consonant(word, i): i += 1
        if i >= L: break
        while i < L and not _is_consonant(word, i): i += 1
        m += 1
    return m

def _is_consonant(word, i):
    ch = word[i]
    if ch in _vowels: 
        return False
    if ch == 'y':
        return i == 0 or not _is_consonant(word, i-1)
    return True

class TransactionScreeningAPIView(generics.ListAPIView):
    serializer_class = TransactionSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = Transaction.objects.all()

        # ----------------------------------
        # 1. GEOGRAPHY FILTER (COUNTRY)
        # ----------------------------------
        raw_geo = self.request.GET.getlist('geography')

        if not raw_geo:
            single = self.request.GET.get('geography')
            if single:
                raw_geo = [g.strip() for g in re.split(r'[;,|]+', single) if g.strip()]

        if raw_geo:
            q_geo = Q()
            for g in raw_geo:
                q_geo |= Q(geography__icontains=g)
            qs = qs.filter(q_geo)

        # ----------------------------------
        # 2. INDUSTRY FILTER (NEW)
        # ----------------------------------
        raw_industries = self.request.GET.getlist('primary_industry')

        if not raw_industries:
            single = self.request.GET.get('primary_industry')
            if single:
                raw_industries = [i.strip() for i in re.split(r'[;,|]+', single) if i.strip()]

        if len(raw_industries) == 1 and (',' in raw_industries[0] or ';' in raw_industries[0]):
            raw_industries = [i.strip() for i in re.split(r'[;,|]+', raw_industries[0]) if i.strip()]

        if raw_industries:
            q_industry = Q()
            for i in raw_industries:
                q_industry |= Q(primary_industry__icontains=i)
            qs = qs.filter(q_industry)


        # ----------------------------------
        # 3. FINANCIAL FILTERS (MIN / MAX)
        # ----------------------------------

        # ----------------------------------
        # DATE FILTER (M&A CLOSED DATE)
        # ----------------------------------
        from datetime import datetime

        def _get_date(val):
            if not val:
                return None
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except:
                return None

        date_min = _get_date(self.request.GET.get("ma_closed_date_min"))
        date_max = _get_date(self.request.GET.get("ma_closed_date_max"))

        if date_min:
            qs = qs.filter(ma_closed_date__gte=date_min)
        if date_max:
            qs = qs.filter(ma_closed_date__lte=date_max)


        # ----------------------------------
        # DECIMAL HELPER
        # ----------------------------------
        def _get_decimal(val):
            if not val:
                return None
            try:
                return Decimal(str(val))
            except:
                return None


        # ----------------------------------
        # TOTAL TRANSACTION VALUE (INR)
        # ----------------------------------
        inr_min = _get_decimal(self.request.GET.get("txn_value_inr_min"))
        inr_max = _get_decimal(self.request.GET.get("txn_value_inr_max"))

        if inr_min is not None:
            qs = qs.filter(total_transaction_value_inr__gte=inr_min)
        if inr_max is not None:
            qs = qs.filter(total_transaction_value_inr__lte=inr_max)


        # ----------------------------------
        # TOTAL TRANSACTION VALUE (USD)
        # ----------------------------------
        usd_min = _get_decimal(self.request.GET.get("txn_value_usd_min"))
        usd_max = _get_decimal(self.request.GET.get("txn_value_usd_max"))

        if usd_min is not None:
            qs = qs.filter(total_transaction_value_usd__gte=usd_min)
        if usd_max is not None:
            qs = qs.filter(total_transaction_value_usd__lte=usd_max)


        # ----------------------------------
        # IMPLIED EV (USD)
        # ----------------------------------
        ev_usd_min = _get_decimal(self.request.GET.get("implied_ev_usd_min"))
        ev_usd_max = _get_decimal(self.request.GET.get("implied_ev_usd_max"))

        if ev_usd_min is not None:
            qs = qs.filter(implied_ev_usd__gte=ev_usd_min)
        if ev_usd_max is not None:
            qs = qs.filter(implied_ev_usd__lte=ev_usd_max)


        # ----------------------------------
        # PERCENT SOUGHT
        # ----------------------------------
        percent_min = _get_decimal(self.request.GET.get("percent_sought_min"))
        percent_max = _get_decimal(self.request.GET.get("percent_sought_max"))

        if percent_min is not None:
            qs = qs.filter(percent_sought__gte=percent_min)
        if percent_max is not None:
            qs = qs.filter(percent_sought__lte=percent_max)


        # ----------------------------------
        # EV / REVENUE
        # ----------------------------------
        ev_rev_min = _get_decimal(self.request.GET.get("ev_revenue_min"))
        ev_rev_max = _get_decimal(self.request.GET.get("ev_revenue_max"))

        if ev_rev_min is not None:
            qs = qs.filter(ev_revenue__gte=ev_rev_min)
        if ev_rev_max is not None:
            qs = qs.filter(ev_revenue__lte=ev_rev_max)


        # ----------------------------------
        # EV / EBITDA
        # ----------------------------------
        ev_ebitda_min = _get_decimal(self.request.GET.get("ev_ebitda_min"))
        ev_ebitda_max = _get_decimal(self.request.GET.get("ev_ebitda_max"))

        if ev_ebitda_min is not None:
            qs = qs.filter(ev_ebitda__gte=ev_ebitda_min)
        if ev_ebitda_max is not None:
            qs = qs.filter(ev_ebitda__lte=ev_ebitda_max)


        # ----------------------------------
        # ACCOUNTING METHOD (DROPDOWN)
        # ----------------------------------
        accounting_method = self.request.GET.get("accounting_method")

        if accounting_method:
            qs = qs.filter(accounting_method__iexact=accounting_method)

        # ----------------------------------
        # 2. KEYWORD GROUP LOGIC (SAME AS GPC)
        # ----------------------------------
        group_objects = []

        def _add_group_from_raw(raw_val, combine_with_prev=None):
            raw_val = raw_val.strip()
            if not raw_val:
                return

            # EXACT PHRASE
            if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] == '"':
                phrase = raw_val[1:-1].strip()
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
                return

            # SAME SENTENCE
            if ' ' in raw_val and ',' not in raw_val:
                words = raw_val.split()
                q_loose = _safe_phrase_prefilter('business_description', words)

                group_objects.append({
                    "type": "SAME_SENTENCE",
                    "words": words,
                    "q": q_loose,
                    "combine_with_prev": combine_with_prev
                })
                return

            # NORMAL WORD
            group_objects.append({
                "type": "OTHER",
                "words": [raw_val],
                "q": Q(**{'business_description__icontains': raw_val}),
                "combine_with_prev": combine_with_prev
            })

        # GET KEYWORDS
        kw_list = self.request.GET.getlist('keywords')
        kw_conditions = [c.upper() for c in self.request.GET.getlist('keyword_condition')]

        for i, raw in enumerate(kw_list):
            combine = kw_conditions[i] if i < len(kw_conditions) else None

            for part in [p.strip() for p in re.split(r'[;,]+', raw) if p.strip()]:
                _add_group_from_raw(part, combine)

        # ----------------------------------
        # 3. APPLY LOGIC
        # ----------------------------------
        if group_objects:
            # Prefilter
            prefilter_q = None
            for g in group_objects:
                prefilter_q = g['q'] if prefilter_q is None else (prefilter_q | g['q'])

            candidate_qs = qs.filter(prefilter_q) if prefilter_q else qs

            # Collect ID sets
            same_groups = []
            exact_groups = []

            for g in group_objects:
                if g['type'] == 'SAME_SENTENCE':
                    same_groups.append(g)
                    g['_index'] = len(same_groups) - 1
                elif g['type'] == 'EXACT_PHRASE':
                    exact_groups.append(g)
                    g['_index'] = len(exact_groups) - 1

            same_id_sets = [set() for _ in same_groups]
            exact_id_sets = [set() for _ in exact_groups]

            for txn in candidate_qs.only('id', 'business_description'):
                desc = txn.business_description or ""

                for i, g in enumerate(same_groups):
                    if _sentence_matches_phrase_fuzzy(desc, g['words']):
                        same_id_sets[i].add(txn.id)

                for i, g in enumerate(exact_groups):
                    if _text_contains_exact_phrase(desc, g['phrase']):
                        exact_id_sets[i].add(txn.id)

            # FINAL Q
            final_q = None
            global_op = (self.request.GET.get('group_operator') or 'OR').upper()

            for g in group_objects:
                if g['type'] == 'SAME_SENTENCE':
                    ids = same_id_sets[g['_index']]
                    q_part = Q(id__in=list(ids))
                elif g['type'] == 'EXACT_PHRASE':
                    ids = exact_id_sets[g['_index']]
                    q_part = Q(id__in=list(ids))
                else:
                    q_part = g['q']

                if final_q is None:
                    final_q = q_part
                else:
                    op = g.get('combine_with_prev') or global_op
                    final_q = final_q & q_part if op == "AND" else final_q | q_part

            qs = qs.filter(final_q)

        return qs.order_by("-created_at")
    
    def _populate_ai_for_list(self, items, compare_desc):
        items_list = list(items)
        if not items_list or not compare_desc:
            return

        from concurrent.futures import ThreadPoolExecutor
        from django.core.cache import cache

        MAX_WORKERS = 6
        PER_FUTURE_TIMEOUT = 10
        CACHE_TTL = 60 * 60 * 6

        pending = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for txn in items_list:
                txn._ai_similarity = None
                txn._ai_rationale = None

                key_id = getattr(txn, "id", None) or (txn.business_description or "")
                ck = _cache_key_for_compare(str(key_id), compare_desc)

                cached = cache.get(ck)
                if cached:
                    txn._ai_similarity = cached.get("similarity")
                    txn._ai_rationale = cached.get("rationale")
                    continue

                future = ex.submit(
                    _safe_call_openai_compare,
                    txn.business_description or "",
                    compare_desc
                )
                pending.append((txn, future, ck))

            for txn, fut, ck in pending:
                try:
                    res = fut.result(timeout=PER_FUTURE_TIMEOUT)
                except Exception:
                    res = None

                if res:
                    txn._ai_similarity = res.get("similarity")
                    txn._ai_rationale = res.get("rationale")

                    try:
                        cache.set(ck, res, CACHE_TTL)
                    except:
                        pass

    def _get_counts(self, qs):
        return {
            "countries": qs.values("geography")
                           .exclude(geography__isnull=True)
                           .exclude(geography="")
                           .distinct()
                           .count(),

            "industries": qs.values("primary_industry")
                        .exclude(
                            Q(primary_industry__isnull=True) |
                            Q(primary_industry__exact="") |
                            Q(primary_industry__in=["-", "N/A", "None", "Unknown"])
                        )
                        .distinct()
                        .count(),
        }

    # ==========================================
    # FINAL LIST API
    # ==========================================
    def list(self, request, *args, **kwargs):
        compare_desc = request.GET.get('compare_description')

        queryset = self.get_queryset()
        counts = self._get_counts(queryset)
        page = self.paginate_queryset(queryset)

        if page is not None:
            if compare_desc:
                self._populate_ai_for_list(page, compare_desc)

            serializer = self.get_serializer(page, many=True)
            data = serializer.data

            if compare_desc:
                for idx, obj in enumerate(page):
                    data[idx]["business_model_similarity"] = getattr(obj, "_ai_similarity", None)
                    data[idx]["ai_rationale"] = getattr(obj, "_ai_rationale", None)

            response = self.get_paginated_response(data)
            response.data["counts"] = counts
            return response

        # non-paginated fallback
        if compare_desc:
            self._populate_ai_for_list(queryset, compare_desc)

        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data

        if compare_desc:
            for idx, obj in enumerate(queryset):
                data[idx]["business_model_similarity"] = getattr(obj, "_ai_similarity", None)
                data[idx]["ai_rationale"] = getattr(obj, "_ai_rationale", None)

        return Response({
            "results": data,
            "counts": counts
    })