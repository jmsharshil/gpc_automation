import pandas as pd
from .models import Transaction
from decimal import Decimal
import uuid


# -----------------------------
# HELPERS (KEEP THESE)
# -----------------------------

def normalize_column(col):
    return str(col).strip().replace("\n", " ").replace("\r", "").replace("  ", " ")


def safe_get(row, column_name):
    if column_name not in row:
        return None
    value = row[column_name]
    if pd.isna(value):
        return None
    return value


def to_decimal(val):
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except:
        return None


def to_date(val):
    if val is None:
        return None

    if isinstance(val, pd.Timestamp):
        return val.date()

    if isinstance(val, str):
        val = val.strip()
        if not val or "denotes" in val.lower():
            return None

        parsed = pd.to_datetime(val, errors='coerce')
        return parsed.date() if not pd.isna(parsed) else None

    return None


# -----------------------------
# MAIN FUNCTION (REPLACE THIS)
# -----------------------------

def process_transaction_excel(file, uploaded_by=None, save_file_to_job=False):
    try:
        df = pd.read_excel(file, sheet_name="Master Transaction Screening")

        # Clean column names
        df.columns = [normalize_column(col) for col in df.columns]

        # Clean empty values
        df = df.replace(r'^\s*$', None, regex=True)

        transactions = []
        created_count = 0
        batch_size = 1000  # 🔥 important

        for _, row in df.iterrows():
            row_dict = row.to_dict()

            transactions.append(Transaction(
                transaction_id=str(uuid.uuid4()),

                announced_date=to_date(safe_get(row_dict, "All Transactions Announced Date")),
                target_issuer=safe_get(row_dict, "Target/Issuer"),

                total_transaction_value_inr=to_decimal(
                    safe_get(row_dict, "Total Transaction Value (INRmm, Historical rate)")
                ),
                total_transaction_value_usd=to_decimal(
                    safe_get(row_dict, "Total Transaction Value ($USDmm, Historical rate)")
                ),

                buyers_investors=safe_get(row_dict, "Buyers/Investors"),
                sellers=safe_get(row_dict, "Sellers"),

                ciq_transaction_id=safe_get(row_dict, "CIQ Transaction ID"),
                ma_closed_date=to_date(safe_get(row_dict, "M&A Closed Date")),

                geography=safe_get(row_dict, "Geographic Locations [Target/Issuer]"),
                country=safe_get(row_dict, "Geography"),
                
                ev_revenue=to_decimal(
                    safe_get(row_dict, "Implied Enterprise Value/Revenues (x)")
                ),
                ev_ebitda=to_decimal(
                    safe_get(row_dict, "Implied Enterprise Value/EBITDA (x)")
                ),

                percent_sought=to_decimal(
                    safe_get(row_dict, "Percent Sought (%)")
                ),
                
                target_stock_premium_1d=to_decimal(
                    safe_get(row_dict, "Target Stock Premium - 1 Day Prior (%)")
                ),
                target_stock_premium_1w=to_decimal(
                    safe_get(row_dict, "Target Stock Premium - 1 Week Prior (%)")
                ),
                target_stock_premium_1m=to_decimal(
                    safe_get(row_dict, "Target Stock Premium - 1 Month Prior (%)")
                ),

                comments=safe_get(row_dict, "Transaction Comments"),

                implied_ev_usd=to_decimal(
                    safe_get(row_dict, "Implied Enterprise Value ($USDmm, Historical rate)")
                ),

                business_description=safe_get(row_dict, "Business Description [Target/Issuer]"),
                primary_industry=safe_get(row_dict, "Primary Industry [Target/Issuer]"),

                target_revenue=to_decimal(
                    safe_get(row_dict, "Target/Issuer LTM Financials - Total Revenue (at Announcement) ($USDmm, Historical rate)")
                ),
                target_ebitda=to_decimal(
                    safe_get(row_dict, "Target/Issuer LTM Financials - EBITDA (at Announcement) ($USDmm, Historical rate)")
                ),

                acquirer_revenue=to_decimal(
                    safe_get(row_dict, "Acquirer LTM Financials - Total Revenue (at Announcement) ($USDmm, Historical rate)")
                ),
                acquirer_ebitda=to_decimal(
                    safe_get(row_dict, "Acquirer LTM Financials - EBITDA (at Announcement) ($USDmm, Historical rate)")
                ),

                consideration_offered=safe_get(row_dict, "Consideration Offered"),
                target_security_type=safe_get(row_dict, "Target Security Types"),
                accounting_method=safe_get(row_dict, "Accounting Method"),
                deal_attitude=safe_get(row_dict, "Deal Attitude"),
            ))

            # 🔥 Insert in batches
            if len(transactions) >= batch_size:
                Transaction.objects.bulk_create(transactions, batch_size=batch_size)
                created_count += len(transactions)
                transactions = []

        # Insert remaining
        if transactions:
            Transaction.objects.bulk_create(transactions, batch_size=batch_size)
            created_count += len(transactions)

        return {
            "created": created_count,
            "total_rows": len(df),
            "status": "completed"
        }

    except Exception as e:
        return {"error": str(e)}