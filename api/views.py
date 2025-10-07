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
from django.db.models import Q
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

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500

def _get_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

def _get_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

# helper to build a Q for a list of terms with AND/OR between those terms
def _build_q_for_terms(terms, condition='OR', field='business_description'):
    if not terms:
        return None
    cond = (condition or 'OR').strip().upper()
    q = None
    for term in terms:
        if not term:
            continue
        term_q = Q(**{f'{field}__icontains': term})
        if q is None:
            q = term_q
        else:
            q = (q & term_q) if cond == 'AND' else (q | term_q)
    return q

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


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 200
    page_size_query_param = 'page_size'
    max_page_size = 500

class CompanyListAPIView(generics.ListAPIView):
    serializer_class = CompanySerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs_companies = Company.objects.all()

        # Company-level filters
        raw_countries = self.request.GET.getlist('headquarters_country_region') or self.request.GET.getlist('country')
        primary_sector = self.request.GET.get('primary_sector')
        primary_industry = self.request.GET.get('primary_industry')

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
        if primary_sector:
            qs_companies = qs_companies.filter(primary_sector__icontains=primary_sector)
        if primary_industry:
            qs_companies = qs_companies.filter(primary_industry__icontains=primary_industry)

        # Financial filters target the 'latest' period
        fr_q = Q(period='latest')

        ev_rev_min = _get_decimal(self.request.GET.get('ev_revenu_min'))
        ev_rev_max = _get_decimal(self.request.GET.get('ev_revenu_max'))
        if ev_rev_min is not None:
            fr_q &= Q(ev_revenu__gte=ev_rev_min)
        if ev_rev_max is not None:
            fr_q &= Q(ev_revenu__lte=ev_rev_max)

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

        groups_q = []

        # 1) index-paired repeated keywords (matches your screenshot)
        # Accept both 'keywords' and 'keyword' param names (frontend may send either)
        kw_list = self.request.GET.getlist('keywords') or self.request.GET.getlist('keyword')
        cond_list = self.request.GET.getlist('keyword_condition')  # repeated per box

        # normalize cond_list and extend if shorter than kw_list
        cond_list = [c.strip().upper() if c else 'OR' for c in cond_list]
        if len(cond_list) < len(kw_list):
            cond_list += ['OR'] * (len(kw_list) - len(cond_list))

        for i, raw in enumerate(kw_list):
            if not raw:
                continue
            cond = cond_list[i] if i < len(cond_list) else 'OR'
            # split on explicit delimiters first (comma/semicolon/pipe)
            terms = [t.strip() for t in re.split(r'[;,|]+', raw) if t.strip()]

            # If the user provided a single phrase with spaces (e.g. "Fleet Management")
            # and there were no explicit delimiters, treat it as multiple words and
            # require ALL words to appear (force AND between the words).
            cond_local = cond
            if len(terms) == 1 and ' ' in terms[0]:
                words = [w.strip() for w in re.split(r'\s+', terms[0]) if w.strip()]
                if words:
                    terms = words
                    cond_local = 'AND'  # force AND between space-separated words

            if terms:
                q_group = _build_q_for_terms(terms, condition=cond_local, field='business_description')
                if q_group is not None:
                    groups_q.append(q_group)


        # 2) explicit keyword_group param (each group encodes its own condition)
        # Format per value: "term1,term2|AND" (condition optional, defaults to OR)
        for raw_group in self.request.GET.getlist('keyword_group'):
            if not raw_group:
                continue
            if '|' in raw_group:
                terms_part, cond_part = raw_group.rsplit('|', 1)
                cond = cond_part.strip().upper() or 'OR'
            else:
                terms_part, cond = raw_group, 'OR'

            terms = [t.strip() for t in re.split(r'[;,|]+', terms_part) if t.strip()]

            cond_local = cond
            if len(terms) == 1 and ' ' in terms[0]:
                words = [w.strip() for w in re.split(r'\s+', terms[0]) if w.strip()]
                if words:
                    terms = words
                    cond_local = 'AND'

            if terms:
                q_group = _build_q_for_terms(terms, condition=cond_local, field='business_description')
                if q_group is not None:
                    groups_q.append(q_group)

        # 3) legacy single keywords param (if nothing else provided)
        if not groups_q:
            legacy = self.request.GET.get('keywords') or self.request.GET.get('keyword')
            if legacy:
                legacy_cond = (self.request.GET.get('keyword_condition') or 'OR').strip().upper()
                parts = [p.strip() for p in re.split(r'[;,|]+', legacy) if p.strip()]

                cond_local = legacy_cond
                # if single phrase with spaces, split into words and force AND
                if len(parts) == 1 and ' ' in parts[0]:
                    words = [w.strip() for w in re.split(r'\s+', parts[0]) if w.strip()]
                    if words:
                        parts = words
                        cond_local = 'AND'

                if parts:
                    q_legacy = _build_q_for_terms(parts, condition=cond_local, field='business_description')
                    if q_legacy is not None:
                        groups_q.append(q_legacy)

        # Combine all groups into a single Q using group_operator (default AND)
        final_keyword_q = None
        if groups_q:
            group_operator = (self.request.GET.get('group_operator') or 'AND').strip().upper()
            final_keyword_q = groups_q[0]
            for g in groups_q[1:]:
                final_keyword_q = (final_keyword_q & g) if group_operator == 'AND' else (final_keyword_q | g)

        # Apply final keyword filter
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
            """
            # collect comps that need remote calls
            pending = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                for comp in items:
                    comp._ai_similarity = None
                    comp._ai_rationale = None

                    # determine cache key: prefer stable ID if present
                    key_id = getattr(comp, "id", None) or (comp.business_description or "")
                    ck = _cache_key_for_compare(str(key_id), compare_desc)
                    cached = cache.get(ck) if compare_desc else None
                    if cached is not None:
                        comp._ai_similarity = cached.get("similarity")
                        comp._ai_rationale = cached.get("rationale")
                        continue

                    # schedule a call if compare_desc provided
                    if compare_desc:
                        # submit async call
                        future = ex.submit(_safe_call_openai_compare, comp.business_description or "", compare_desc)
                        pending.append((comp, future, ck))

                # gather results
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
                            # ignore cache failures; don't break main flow
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
