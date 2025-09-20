# safe version: no snapshot updates, less DB churn, and UploadJob logging
import pandas as pd
from decimal import Decimal, InvalidOperation
from django.db import transaction
from .models import Company, FinancialRecord
from api.models import UploadJob  # adjust import if in different app
import logging

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

def _fr_values_equal(fr_obj, defaults):
    """Return True if all numeric/default values are equal (None and Decimal compare correctly)."""
    for k, v in defaults.items():
        if getattr(fr_obj, k) != v:
            return False
    return True

def process_master_screening_v2(uploaded_file, update_snapshot=False, uploaded_by=None, save_file_to_job=False):
    """
    Reads the sheet and upserts Companies + FinancialRecord. Does NOT update Company snapshot fields.
    - update_snapshot parameter is kept for compatibility but will be ignored (no snapshot fields on Company).
    - uploaded_by can be a user instance to attach to UploadJob (optional).
    - save_file_to_job: if True, UploadJob.file = uploaded_file will be saved (ensure storage supports it).
    """
    try:
        df = pd.read_excel(uploaded_file, sheet_name=MASTER_SHEET_NAME, header=1, engine='openpyxl')
    except Exception as e:
        return {'error': f'Failed to read sheet \"{MASTER_SHEET_NAME}\": {str(e)}'}

    df.columns = [str(c).strip() for c in df.columns]
    if "Company Name" not in df.columns:
        return {'error': 'Required column \"Company Name\" not found in header (row 2).'}

    created_companies = 0
    updated_companies = 0
    created_records = 0
    updated_records = 0
    skipped = 0
    errors = []

    companies_cache = {}

    # Create UploadJob now so we can attach file/filename and update summary later
    job = UploadJob.objects.create(
        uploaded_by=uploaded_by if uploaded_by and getattr(uploaded_by, 'is_authenticated', False) else None,
        filename=getattr(uploaded_file, 'name', '') or '',
    )
    if save_file_to_job:
        try:
            job.file = uploaded_file
            job.save(update_fields=['file'])
        except Exception:
            # don't fail upload if saving file is not possible
            logger.exception("Could not save uploaded file into UploadJob.file")

    with transaction.atomic():
        for idx, row in df.iterrows():
            excel_row = int(idx) + 3
            try:
                raw_name = row.get("Company Name")
                name = _norm_str(raw_name)
                if not name:
                    skipped += 1
                    continue

                exchange_ticker = _norm_str(row.get("Exchange:Ticker"))
                primary_sector = _norm_str(row.get("Primary Sector"))
                primary_industry = _norm_str(row.get("Primary Industry"))
                headquarters = _norm_str(row.get("Headquarters - Country/Region"))
                website = _norm_str(row.get("Website"))
                business_description = _norm_str(row.get("Business Description"))
                industry_classifications = _norm_str(row.get("Industry Classifications"))
                country = _norm_str(row.get("Country"))
                excel_company_id = _norm_str(row.get("Excel Company ID"))

                market_cap = _parse_decimal(row.get("Market Capitalization [My Setting] [Latest] ($USDmm, Historical rate)"))
                total_revenue = _parse_decimal(row.get("Total Revenue [LTM] ($USDmm, Historical rate)"))
                enterprise_value = _parse_decimal(row.get("Total Enterprise Value [My Setting] [Latest] ($USDmm, Historical rate)"))
                ebitda = _parse_decimal(row.get("EBITDA [LTM] ($USDmm, Historical rate)"))
                ev_revenu = _parse_decimal(row.get("EV/ Revenu"))

                # find/create company (prefer Excel Company ID)
                company_key = excel_company_id or name.lower()
                company = companies_cache.get(company_key)
                if not company:
                    if excel_company_id:
                        company = Company.objects.filter(company_id=excel_company_id).first()
                    if not company:
                        company = Company.objects.filter(name__iexact=name).first()
                    if not company:
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
                            country=country
                        )
                        created_companies += 1
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
                        if updated_fields:
                            company.save(update_fields=updated_fields)
                            updated_companies += 1

                    companies_cache[company_key] = company

                # Upsert FinancialRecord with period='latest'
                period = 'latest'
                fr_defaults = {
                    'market_cap': market_cap,
                    'total_revenue': total_revenue,
                    'enterprise_value': enterprise_value,
                    'ebitda': ebitda,
                    'ev_revenu': ev_revenu
                }

                # Try to find existing FR
                fr = FinancialRecord.objects.filter(company=company, period=period).first()
                if not fr:
                    FinancialRecord.objects.create(company=company, period=period, **fr_defaults)
                    created_records += 1
                else:
                    # Only update if something changed (reduce writes)
                    if not _fr_values_equal(fr, fr_defaults):
                        for k, v in fr_defaults.items():
                            setattr(fr, k, v)
                        fr.save(update_fields=[k for k in fr_defaults.keys()])
                        updated_records += 1

            except Exception as e:
                logger.exception("Error processing row %s", excel_row)
                errors.append({'row': excel_row, 'error': str(e)})

        # update job summary and save
        summary = {
            'created_companies': created_companies,
            'updated_companies': updated_companies,
            'created_records': created_records,
            'updated_records': updated_records,
            'skipped_rows': skipped,
            'errors': errors
        }
        job.summary = summary
        job.save(update_fields=['summary'])

    return summary
