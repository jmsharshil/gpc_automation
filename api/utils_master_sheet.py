import pandas as pd
from decimal import Decimal, InvalidOperation
from django.db import transaction
from .models import Company, FinancialRecord

MASTER_SHEET_NAME = "Master database Screening"

# Exact headers you shared
EXPECTED_COLUMNS = [
    "Company Name",
    "Exchange:Ticker",
    "Primary Sector",
    "Primary Industry",
    "Headquarters - Country/Region",
    "Website",
    "Business Description",
    "Industry Classifications",
    "Market Capitalization [My Setting] [Latest] ($USDmm, Historical rate)",
    "Total Revenue [LTM] ($USDmm, Historical rate)",
    "Total Enterprise Value [My Setting] [Latest] ($USDmm, Historical rate)",
    "EBITDA [LTM] ($USDmm, Historical rate)",
    "Excel Company ID",
    "Country",
    "EV/ Revenu"
]

# helper: values that should be considered empty
EMPTY_TOKENS = {"", "-", "—", "na", "n/a", "none", "null", "nan", "--"}

def _norm_str(val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s.lower() in EMPTY_TOKENS:
        return None
    return s

def _parse_decimal(val):
    """
    Parse numeric values like "1,234.56", treat "-" or empty as None.
    Return Decimal or None.
    """
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s == '':
        return None
    if s.lower() in EMPTY_TOKENS:
        return None
    # remove commas, currency signs, spaces
    s = s.replace(',', '').replace('$', '').replace(' ', '')
    # sometimes values include parentheses for negative: (123) -> -123
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        # try to remove "%" if present (unlikely for your fields) then parse
        s2 = s.replace('%', '')
        try:
            return Decimal(s2)
        except Exception:
            return None

def process_master_screening_v2(uploaded_file, update_snapshot=True):
    """
    Reads the 'Master database Screening' sheet, header at row 2 (header=1),
    imports descriptive fields to Company and numeric metrics to FinancialRecord.
    Returns summary dict: created_companies, updated_companies, created_records, updated_records, errors
    """
    try:
        df = pd.read_excel(uploaded_file, sheet_name=MASTER_SHEET_NAME, header=1, engine='openpyxl')
    except Exception as e:
        return {'error': f'Failed to read sheet "{MASTER_SHEET_NAME}": {str(e)}'}

    # Normalize DataFrame columns (strip)
    df.columns = [str(c).strip() for c in df.columns]

    # Check presence of minimal column
    if "Company Name" not in df.columns:
        return {'error': 'Required column "Company Name" not found in header (row 2).'}

    created_companies = 0
    updated_companies = 0
    created_records = 0
    updated_records = 0
    skipped = 0
    errors = []

    companies_cache = {}

    with transaction.atomic():
        for idx, row in df.iterrows():
            try:
                # Excel human row number: header row is 2, so data starts at 3 => idx + 3
                excel_row = int(idx) + 3

                raw_name = row.get("Company Name")
                name = _norm_str(raw_name)
                if not name:
                    skipped += 1
                    continue

                # read descriptive fields
                exchange_ticker = _norm_str(row.get("Exchange:Ticker"))
                primary_sector = _norm_str(row.get("Primary Sector"))
                primary_industry = _norm_str(row.get("Primary Industry"))
                headquarters = _norm_str(row.get("Headquarters - Country/Region"))
                website = _norm_str(row.get("Website"))
                business_description = _norm_str(row.get("Business Description"))
                industry_classifications = _norm_str(row.get("Industry Classifications"))
                country = _norm_str(row.get("Country"))

                # read numeric fields
                market_cap = _parse_decimal(row.get("Market Capitalization [My Setting] [Latest] ($USDmm, Historical rate)"))
                total_revenue = _parse_decimal(row.get("Total Revenue [LTM] ($USDmm, Historical rate)"))
                enterprise_value = _parse_decimal(row.get("Total Enterprise Value [My Setting] [Latest] ($USDmm, Historical rate)"))
                ebitda = _parse_decimal(row.get("EBITDA [LTM] ($USDmm, Historical rate)"))
                ev_revenu = _parse_decimal(row.get("EV/ Revenu"))

                excel_company_id = _norm_str(row.get("Excel Company ID"))

                # find or create company (prefer Excel Company ID)
                company_key = excel_company_id or name.lower()
                company = companies_cache.get(company_key)
                if not company:
                    if excel_company_id:
                        company = Company.objects.filter(company_id=excel_company_id).first()
                    if not company:
                        # fallback by name (case-insensitive)
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
                        # update descriptive fields if they are present in sheet (avoid overwriting existing with None)
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

                # Upsert FinancialRecord for this company and period 'latest'
                period = 'latest'  # if you later add a period column, use it here
                fr_defaults = {
                    'market_cap': market_cap,
                    'total_revenue': total_revenue,
                    'enterprise_value': enterprise_value,
                    'ebitda': ebitda,
                    'ev_revenu': ev_revenu
                }
                fr, fr_created = FinancialRecord.objects.update_or_create(
                    company=company,
                    period=period,
                    defaults=fr_defaults
                )
                if fr_created:
                    created_records += 1
                else:
                    updated_records += 1

                # Update company snapshot numeric fields if update_snapshot True
                if update_snapshot:
                    company.latest_period = period
                    # only overwrite if we have values (so missing values won't nulllify previous snapshot)
                    snapshot_fields = []
                    if market_cap is not None:
                        company.latest_market_cap = market_cap
                        snapshot_fields.append('latest_market_cap')
                    if total_revenue is not None:
                        company.latest_total_revenue = total_revenue
                        snapshot_fields.append('latest_total_revenue')
                    if enterprise_value is not None:
                        company.latest_enterprise_value = enterprise_value
                        snapshot_fields.append('latest_enterprise_value')
                    if ebitda is not None:
                        company.latest_ebitda = ebitda
                        snapshot_fields.append('latest_ebitda')
                    if ev_revenu is not None:
                        company.latest_ev_revenu = ev_revenu
                        snapshot_fields.append('latest_ev_revenu')
                    # always update latest_period if at least one value present
                    if snapshot_fields:
                        snapshot_fields.append('latest_period')
                        company.latest_period = period
                        company.save(update_fields=snapshot_fields)
            except Exception as e:
                errors.append({'row': excel_row, 'error': str(e)})
    return {
        'created_companies': created_companies,
        'updated_companies': updated_companies,
        'created_records': created_records,
        'updated_records': updated_records,
        'skipped_rows': skipped,
        'errors': errors
    }
