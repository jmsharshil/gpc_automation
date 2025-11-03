# api/utils_master_sheet.py
import logging
import pandas as pd
from decimal import Decimal, InvalidOperation

from django.db import transaction, DataError
from django.core.exceptions import FieldDoesNotExist
from django.conf import settings

from .models import Company, FinancialRecord
from api.models import UploadJob  # adjust import if UploadJob lives elsewhere
import datetime
import math

logger = logging.getLogger(__name__)

MASTER_SHEET_NAME = "Master database Screening"
EMPTY_TOKENS = {"", "-", "—", "na", "n/a", "none", "null", "nan", "--"}


def _norm_str(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s.lower() in EMPTY_TOKENS:
        return None
    return s


def _parse_decimal(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s == '':
        return None
    if s.lower() in EMPTY_TOKENS:
        return None
    s = s.replace(',', '').replace('$', '').replace(' ', '')
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        s2 = s.replace('%', '')
        try:
            return Decimal(s2)
        except Exception:
            return None


def _truncate_for_model(model_cls, attr_name, value):
    """
    If value is a string and model field has max_length and value is longer,
    return (truncated_value, True). Otherwise return (value, False).
    """
    if value is None or not isinstance(value, str):
        return value, False
    try:
        field = model_cls._meta.get_field(attr_name)
    except FieldDoesNotExist:
        return value, False
    max_length = getattr(field, 'max_length', None)
    if max_length and len(value) > max_length:
        return value[:max_length], True
    return value, False


def _fr_values_equal(fr_obj, defaults):
    for k, v in defaults.items():
        if getattr(fr_obj, k) != v:
            return False
    return True

def _parse_date(val):
    """
    Safely parse date from:
      - string like '08-03-2000' or '08/03/2000' (day-first)
      - pandas Timestamp / datetime.date / datetime.datetime
      - Excel serial numbers (e.g., 37653)  -> Windows base (1899-12-30)
      - blanks / NA -> None
    Returns datetime.date or None.
    """
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    # pandas NA
    try:
        import pandas as pd
        if pd.isna(val):
            return None
    except Exception:
        pass

    # datetime / date
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val

    # Excel serial numbers (int/float)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # Excel for Windows uses 1899-12-30 as day 0 (handling the 1900 leap bug)
        try:
            base = datetime.date(1899, 12, 30)
            serial_days = int(val)
            return base + datetime.timedelta(days=serial_days)
        except Exception:
            pass

    # Strings
    s = str(val).strip()
    if not s:
        return None
    if s.lower() in EMPTY_TOKENS:
        return None

    # Try pandas day-first parse (handles '08-03-2000' as 08 March 2000)
    try:
        import pandas as pd
        ts = pd.to_datetime(s, dayfirst=True, errors='coerce')
        if pd.notna(ts):
            # ts may be Timestamp or NaT
            return ts.date()
    except Exception:
        pass

    # Fallback: try datetime.strptime with a couple of common patterns
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            continue

    return None

def process_master_screening_v2(uploaded_file, update_snapshot=False, uploaded_by=None, save_file_to_job=False):
    """
    Safe importer that:
      - creates an UploadJob before processing
      - truncates fields that would overflow DB columns
      - processes each row in its own small transaction so one bad row doesn't break everything
      - does NOT update Company snapshot fields (update_snapshot kept for backward compat)
    Returns summary dict.
    """
    try:
        df = pd.read_excel(uploaded_file, sheet_name=MASTER_SHEET_NAME, header=1, engine='openpyxl')
    except Exception as e:
        return {'error': f'Failed to read sheet \"{MASTER_SHEET_NAME}\": {str(e)}'}

    df.columns = [str(c).strip() for c in df.columns]

    if "Company Name" not in df.columns:
        return {'error': 'Required column "Company Name" not found in header (row 2).'}

    created_companies = 0
    updated_companies = 0
    created_records = 0
    updated_records = 0
    skipped = 0
    errors = []

    companies_cache = {}

    # Create UploadJob before processing so we persist metadata even if atomic fails
    job = UploadJob.objects.create(
        uploaded_by=uploaded_by if uploaded_by and getattr(uploaded_by, 'is_authenticated', False) else None,
        filename=getattr(uploaded_file, 'name', '') or '',
        summary={}
    )
    if save_file_to_job:
        try:
            # Save file to the FileField if desired (may require storage configured)
            job.file.save(getattr(uploaded_file, 'name', 'uploaded.xlsx'), uploaded_file)
        except Exception:
            logger.exception("Could not save uploaded file into UploadJob.file (non-fatal)")

    for idx, row in df.iterrows():
        excel_row = int(idx) + 3
        # Use per-row atomic block so one row failure doesn't mark whole transaction broken
        try:
            with transaction.atomic():
                raw_name = row.get("Company Name")
                name = _norm_str(raw_name)
                if not name:
                    skipped += 1
                    continue

                # read and normalize descriptive fields
                exchange_ticker = _norm_str(row.get("Exchange:Ticker"))
                primary_sector = _norm_str(row.get("Primary Sector"))
                primary_industry = _norm_str(row.get("Primary Industry"))
                headquarters = _norm_str(row.get("Headquarters - Country/Region"))
                website = _norm_str(row.get("Website"))
                business_description = _norm_str(row.get("Business Description"))
                industry_classifications = _norm_str(row.get("Industry Classifications"))
                country = _norm_str(row.get("Country"))
                excel_company_id = _norm_str(row.get("Excel Company ID"))

                # numeric fields
                market_cap = _parse_decimal(row.get("Market Capitalization [My Setting] [Latest] ($USDmm, Historical rate)"))
                total_revenue = _parse_decimal(row.get("Total Revenue [LTM] ($USDmm, Historical rate)"))
                enterprise_value = _parse_decimal(row.get("Total Enterprise Value [My Setting] [Latest] ($USDmm, Historical rate)"))
                ebitda = _parse_decimal(row.get("EBITDA [LTM] ($USDmm, Historical rate)"))
                ev_revenu = _parse_decimal(row.get("EV/ Revenu"))
                
                # NEW: read/parse first pricing date
                first_pricing_date_raw = row.get("First Pricing Date")
                first_pricing_date = _parse_date(first_pricing_date_raw)

                # Truncate strings so DB won't reject them
                truncated_fields = {}
                name, t = _truncate_for_model(Company, 'name', name)
                if t: truncated_fields['name'] = True
                exchange_ticker, t = _truncate_for_model(Company, 'exchange_ticker', exchange_ticker); 
                if t: truncated_fields['exchange_ticker'] = True
                primary_sector, t = _truncate_for_model(Company, 'primary_sector', primary_sector);
                if t: truncated_fields['primary_sector'] = True
                primary_industry, t = _truncate_for_model(Company, 'primary_industry', primary_industry);
                if t: truncated_fields['primary_industry'] = True
                headquarters, t = _truncate_for_model(Company, 'headquarters_country_region', headquarters);
                if t: truncated_fields['headquarters_country_region'] = True
                website, t = _truncate_for_model(Company, 'website', website);
                if t: truncated_fields['website'] = True
                industry_classifications, t = _truncate_for_model(Company, 'industry_classifications', industry_classifications);
                if t: truncated_fields['industry_classifications'] = True
                country, t = _truncate_for_model(Company, 'country', country);
                if t: truncated_fields['country'] = True
                excel_company_id, t = _truncate_for_model(Company, 'company_id', excel_company_id);
                if t: truncated_fields['company_id'] = True

                if truncated_fields:
                    errors.append({
                        'row': excel_row,
                        'warning': 'Truncated fields to fit DB column lengths',
                        'truncated_fields': list(truncated_fields.keys())
                    })

                # Find or create company (prefer Excel Company ID)
                company_key = excel_company_id or name.lower()
                company = companies_cache.get(company_key)
                if not company:
                    if excel_company_id:
                        company = Company.objects.filter(company_id=excel_company_id).first()
                    if not company:
                        company = Company.objects.filter(name__iexact=name).first()
                    if not company:
                        # Create company
                        try:
                            company = Company.objects.create(
                                company_id=excel_company_id,
                                name=name,
                                exchange_ticker=exchange_ticker,
                                primary_sector=primary_sector,
                                primary_industry=primary_industry,
                                headquarters_country_region=headquarters,
                                website=website,
                                business_description=business_description,
                                industry_classifications=industry_classifications,
                                country=country,
                                first_pricing_date=first_pricing_date,
                            )
                            created_companies += 1
                        except DataError as e:
                            # Defensive: if DB still rejects, record error and skip this row
                            logger.exception("DataError creating Company on row %s: %s", excel_row, e)
                            errors.append({'row': excel_row, 'error': f'DataError creating Company: {str(e)}'})
                            continue
                    else:
                        # update descriptive fields only if present and changed
                        updated_fields = []
                        def set_if_present(attr, value):
                            nonlocal updated_fields
                            if value is not None and getattr(company, attr) != value:
                                setattr(company, attr, value)
                                updated_fields.append(attr)

                        set_if_present('company_id', excel_company_id)
                        set_if_present('exchange_ticker', exchange_ticker)
                        set_if_present('primary_sector', primary_sector)
                        set_if_present('primary_industry', primary_industry)
                        set_if_present('headquarters_country_region', headquarters)
                        set_if_present('website', website)
                        set_if_present('business_description', business_description)
                        set_if_present('industry_classifications', industry_classifications)
                        set_if_present('country', country)
                        set_if_present('first_pricing_date', first_pricing_date)
                        if updated_fields:
                            try:
                                company.save(update_fields=updated_fields)
                                updated_companies += 1
                            except DataError as e:
                                logger.exception("DataError updating Company on row %s: %s", excel_row, e)
                                errors.append({'row': excel_row, 'error': f'DataError updating Company: {str(e)}'})
                                continue

                    companies_cache[company_key] = company

                # Upsert FinancialRecord (period='latest')
                period = 'latest'
                fr_defaults = {
                    'market_cap': market_cap,
                    'total_revenue': total_revenue,
                    'enterprise_value': enterprise_value,
                    'ebitda': ebitda,
                    'ev_revenu': ev_revenu
                }

                fr = FinancialRecord.objects.filter(company=company, period=period).first()
                if not fr:
                    try:
                        FinancialRecord.objects.create(company=company, period=period, **fr_defaults)
                        created_records += 1
                    except DataError as e:
                        logger.exception("DataError creating FinancialRecord on row %s: %s", excel_row, e)
                        errors.append({'row': excel_row, 'error': f'DataError creating FinancialRecord: {str(e)}'})
                        continue
                else:
                    # update only if changed
                    if not _fr_values_equal(fr, fr_defaults):
                        for k, v in fr_defaults.items():
                            setattr(fr, k, v)
                        try:
                            fr.save(update_fields=[k for k in fr_defaults.keys()])
                            updated_records += 1
                        except DataError as e:
                            logger.exception("DataError updating FinancialRecord on row %s: %s", excel_row, e)
                            errors.append({'row': excel_row, 'error': f'DataError updating FinancialRecord: {str(e)}'})
                            continue

                # NOTE: we intentionally skip Company snapshot updates to avoid writing to non-existent latest_* fields
                # If you later add snapshot fields to Company model, enable update_snapshot and add guarded checks.
        except Exception as e:
            # Catch any unexpected exceptions per-row to continue processing others.
            logger.exception("Unexpected error processing row %s: %s", excel_row, e)
            errors.append({'row': excel_row, 'error': f'Unexpected error: {str(e)}'})
            continue

    # After loop: update job summary outside per-row transactions
    summary = {
        'created_companies': created_companies,
        'updated_companies': updated_companies,
        'created_records': created_records,
        'updated_records': updated_records,
        'skipped_rows': skipped,
        'errors': errors
    }
    try:
        job.summary = summary
        job.save(update_fields=['summary'])
    except Exception:
        # If saving fails, at least log it; return summary to caller
        logger.exception("Failed to save UploadJob.summary for job %s", job.pk)

    return summary
