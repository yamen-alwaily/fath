"""Synthetic Saudi Mock Bank Data Layer for Fath (فتح).

Generates realistic Saudi banking data adhering to SAMA Open Banking Framework
specifications:
- 3 Fictional Sandbox Banks (Fath National Bank, Fath Riyadh Bank, Fath Al Jazeera Bank)
- Valid MOD-97 Saudi IBAN generation (SA + 2 check digits + 20 alphanumeric chars)
- 4 Customer Archetypes: prime, average, stressed, flagged
- Bilingual merchants & transaction descriptions (Arabic & English)
- 6-12 months transaction ledgers per user
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import os
from pathlib import Path
import random
import sqlite3
from typing import Any

from faker import Faker

from database import get_connection, init_db, log_event

# Initialize Arabic and English Faker instances
fake_ar = Faker("ar_SA")
fake_en = Faker("en_US")

# Disclaimer required by SAMA sandbox guidelines
SAMA_SANDBOX_DISCLAIMER = (
    "Fath is an independent portfolio project for educational purposes. "
    "It is not affiliated with, endorsed by, or certified by SAMA (Saudi Central Bank). "
    "All bank names and financial data are fictional and generated for testing purposes only."
)

# 1. Three Fictional Saudi Banks (ASPSPs)
MOCK_BANKS = [
    {
        "code": "10",
        "name_ar": "البنك الأهلي الافتراضي",
        "name_en": "Fath National Bank (sandbox)",
        "short_en": "FNB",
    },
    {
        "code": "20",
        "name_ar": "بنك الرياض الافتراضي",
        "name_en": "Fath Riyadh Bank (sandbox)",
        "short_en": "FRB",
    },
    {
        "code": "30",
        "name_ar": "مصرف الجزيرة الافتراضي",
        "name_en": "Fath Al Jazeera Bank (sandbox)",
        "short_en": "FJB",
    },
]

# 2. Authentic Saudi Bilingual Merchants & Categories
MERCHANT_CATALOG = [
    {
        "merchant_ar": "هنقرستيشن",
        "merchant_en": "HungerStation",
        "category": "Food & Dining",
        "desc_ar": "طلب طعام عبر التطبيق - هنقرستيشن",
        "desc_en": "Online food order - HungerStation",
        "min_amount": 35.0,
        "max_amount": 180.0,
        "type": "debit",
    },
    {
        "merchant_ar": "أسواق التميمي",
        "merchant_en": "Tamimi Markets",
        "category": "Groceries",
        "desc_ar": "مشتريات بقالة - أسواق التميمي",
        "desc_en": "Grocery shopping - Tamimi Markets",
        "min_amount": 120.0,
        "max_amount": 950.0,
        "type": "debit",
    },
    {
        "merchant_ar": "مكتبة جرير",
        "merchant_en": "Jarir Bookstore",
        "category": "Electronics & Books",
        "desc_ar": "شراء مستلزمات وإلكترونيات - مكتبة جرير",
        "desc_en": "Electronics & stationery - Jarir Bookstore",
        "min_amount": 95.0,
        "max_amount": 4200.0,
        "type": "debit",
    },
    {
        "merchant_ar": "شركة الاتصالات السعودية (STC)",
        "merchant_en": "stc",
        "category": "Bills & Telecom",
        "desc_ar": "سداد فاتورة هاتف وإنترنت - STC",
        "desc_en": "Telecom bill payment - stc",
        "min_amount": 230.0,
        "max_amount": 650.0,
        "type": "debit",
    },
    {
        "merchant_ar": "نظام سداد للمدفوعات",
        "merchant_en": "SADAD Payment System",
        "category": "Bills & Utilities",
        "desc_ar": "سداد فواتير حكومية وخدمات - سداد",
        "desc_en": "Government & utilities bill - SADAD",
        "min_amount": 150.0,
        "max_amount": 1200.0,
        "type": "debit",
    },
    {
        "merchant_ar": "كريم السعودية",
        "merchant_en": "Careem Saudi",
        "category": "Transportation",
        "desc_ar": "مشوار نقل ذكي - كريم",
        "desc_en": "Ride-hailing trip - Careem",
        "min_amount": 25.0,
        "max_amount": 115.0,
        "type": "debit",
    },
    {
        "merchant_ar": "الشركة المتحدة للإلكترونيات (إكسترا)",
        "merchant_en": "eXtra Stores",
        "category": "Electronics & Appliances",
        "desc_ar": "شراء أجهزة منزلية - إكسترا",
        "desc_en": "Home appliances purchase - eXtra Stores",
        "min_amount": 350.0,
        "max_amount": 3800.0,
        "type": "debit",
    },
    {
        "merchant_ar": "مطاعم البيك",
        "merchant_en": "ALBAIK",
        "category": "Food & Dining",
        "desc_ar": "وجبة سريعة - مطعم البيك",
        "desc_en": "Fast food meal - ALBAIK Restaurant",
        "min_amount": 22.0,
        "max_amount": 85.0,
        "type": "debit",
    },
    {
        "merchant_ar": "محطات ساسكو",
        "merchant_en": "SASCO Fuel Stations",
        "category": "Transportation & Fuel",
        "desc_ar": "تزود بالوقود - محطة ساسكو",
        "desc_en": "Fuel refill - SASCO Station",
        "min_amount": 55.0,
        "max_amount": 175.0,
        "type": "debit",
    },
    {
        "merchant_ar": "أسواق الدانوب",
        "merchant_en": "Danube Supermarkets",
        "category": "Groceries",
        "desc_ar": "مشتريات استهلاكية - الدانوب",
        "desc_en": "Consumer groceries - Danube Supermarkets",
        "min_amount": 110.0,
        "max_amount": 780.0,
        "type": "debit",
    },
    {
        "merchant_ar": "منصة إحسان الوطنية",
        "merchant_en": "Ehsan Charity Platform",
        "category": "Charity & Donations",
        "desc_ar": "تبرع خيري وصدقة - منصة إحسان",
        "desc_en": "Charitable donation - Ehsan Platform",
        "min_amount": 100.0,
        "max_amount": 2000.0,
        "type": "debit",
    },
    {
        "merchant_ar": "الشركة السعودية للكهرباء",
        "merchant_en": "Saudi Electricity Company (SEC)",
        "category": "Bills & Utilities",
        "desc_ar": "فاتورة استهلاك كهرباء - سداد",
        "desc_en": "Electricity utility bill - SADAD",
        "min_amount": 180.0,
        "max_amount": 850.0,
        "type": "debit",
    },
]

# Archetype profiles definitions
ARCHETYPE_PROFILES = {
    "prime": {
        "username": "abdullah_tamimi",
        "full_name_ar": "عبدالله بن فهد التميمي",
        "full_name_en": "Abdullah Fahad Al-Tamimi",
        "national_id": "1084291840",
        "salary_range": (38000.0, 52000.0),
        "employer_ar": "أرامكو السعودية",
        "employer_en": "Saudi Aramco",
        "accounts": [
            {
                "bank_idx": 0,
                "account_type": "checking",
                "base_balance": 95000.0,
                "is_primary": True,
            },
            {
                "bank_idx": 2,
                "account_type": "savings",
                "base_balance": 320000.0,
                "is_primary": False,
            },
            {
                "bank_idx": 1,
                "account_type": "investment",
                "base_balance": 210000.0,
                "is_primary": False,
            },
        ],
    },
    "average": {
        "username": "sarah_shammari",
        "full_name_ar": "سارة بنت خالد الشمري",
        "full_name_en": "Sarah Khalid Al-Shammari",
        "national_id": "1047192834",
        "salary_range": (13500.0, 17500.0),
        "employer_ar": "شركة الاتصالات السعودية",
        "employer_en": "stc Solutions",
        "accounts": [
            {
                "bank_idx": 1,
                "account_type": "checking",
                "base_balance": 22400.0,
                "is_primary": True,
            },
            {
                "bank_idx": 0,
                "account_type": "savings",
                "base_balance": 48000.0,
                "is_primary": False,
            },
        ],
    },
    "stressed": {
        "username": "mohammed_qahtani",
        "full_name_ar": "محمد بن إبراهيم القحطاني",
        "full_name_en": "Mohammed Ibrahim Al-Qahtani",
        "national_id": "1092837461",
        "salary_range": (3500.0, 5500.0),
        "employer_ar": "أعمال حرة وتوصيل طلبات",
        "employer_en": "Freelance & Delivery Services",
        "accounts": [
            {
                "bank_idx": 0,
                "account_type": "checking",
                "base_balance": 850.0,
                "is_primary": True,
            },
        ],
    },
    "flagged": {
        "username": "faisal_dossary",
        "full_name_ar": "فيصل بن عبدالعزيز الدوسري",
        "full_name_en": "Faisal Abdulaziz Al-Dossary",
        "national_id": "1029384756",
        "salary_range": (18000.0, 24000.0),
        "employer_ar": "مؤسسة تجارية استيراد وتصدير",
        "employer_en": "General Trading & Import",
        "accounts": [
            {
                "bank_idx": 2,
                "account_type": "checking",
                "base_balance": 14200.0,
                "is_primary": True,
            },
        ],
    },
}


def calculate_saudi_iban(bank_code: str, account_number_str: str) -> str:
    """Generate a 24-character Saudi IBAN compliant with MOD-97 check-digit validation.

    Format: SA + 2 check digits + 2 bank code digits + 18 account number digits.
    """
    account_padded = str(account_number_str).zfill(18)
    numeric_bban = f"{bank_code}{account_padded}"
    # Calculate checksum: SA -> '2810' followed by '00'
    calc_str = f"{numeric_bban}281000"
    check = 98 - (int(calc_str) % 97)
    iban = f"SA{check:02d}{numeric_bban}"

    # Verify check
    verify_str = f"{iban[4:]}2810{iban[2:4]}"
    assert int(verify_str) % 97 == 1, f"IBAN checksum validation failed for {iban}"
    return iban


def generate_national_id(prefix: str = "1") -> str:
    """Generate a realistic 10-digit Saudi National ID (1 for citizens, 2 for residents)."""
    random_digits = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"{prefix}{random_digits}"


def generate_reference_number(date_obj: date, seq: int) -> str:
    """Generate an authentic SAMA-compliant transaction reference code."""
    hex_suffix = os.urandom(3).hex().upper()
    return f"FATH-{date_obj.strftime('%Y%m%d')}-{seq:04d}-{hex_suffix}"


def generate_archetype_transactions(
    account_id: int,
    user_id: int,
    archetype: str,
    months_history: int = 8,
    is_primary_checking: bool = True,
    profile_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Synthesize 6-12 months of realistic transactions tailored to the user archetype."""
    if profile_data is None:
        profile_data = ARCHETYPE_PROFILES.get(archetype, ARCHETYPE_PROFILES["average"])

    today = date.today()
    start_date = today - timedelta(days=months_history * 30)
    current_date = start_date

    transactions: list[dict[str, Any]] = []
    seq_counter = 1

    salary_min, salary_max = profile_data.get("salary_range", (12000.0, 16000.0))
    employer_ar = profile_data.get("employer_ar", "شركة مساهمة سعودية")
    employer_en = profile_data.get("employer_en", "Saudi Joint Stock Co.")

    while current_date <= today:
        # 1. Salary / Regular Income deposit on the 27th of each month for Prime and Average archetypes
        if is_primary_checking and current_date.day == 27 and archetype in ("prime", "average"):
            salary_amount = round(random.uniform(salary_min, salary_max), 2)
            ref = generate_reference_number(current_date, seq_counter)
            seq_counter += 1
            transactions.append(
                {
                    "account_id": account_id,
                    "user_id": user_id,
                    "description_ar": f"إيداع راتب شهري - {employer_ar}",
                    "description_en": f"Monthly payroll credit - {employer_en}",
                    "merchant_ar": employer_ar,
                    "merchant_en": employer_en,
                    "amount": salary_amount,
                    "type": "credit",
                    "category": "Salary & Income",
                    "transaction_date": current_date.isoformat(),
                    "reference_number": ref,
                    "source": "fath_demo",
                }
            )

        # 2. Daily Archetype-specific behavior
        if archetype == "prime":
            # Prime: Frequent upscale purchases, charity, fine dining, investments
            if random.random() < 0.70 and is_primary_checking:
                num_txns = random.choice([1, 2, 3])
                for _ in range(num_txns):
                    m = random.choice(MERCHANT_CATALOG)
                    amount = round(random.uniform(m["min_amount"] * 1.5, m["max_amount"] * 1.2), 2)
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": m["desc_ar"],
                            "description_en": m["desc_en"],
                            "merchant_ar": m["merchant_ar"],
                            "merchant_en": m["merchant_en"],
                            "amount": amount,
                            "type": m["type"],
                            "category": m["category"],
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )
            # Periodic dividend or return on investment
            if not is_primary_checking and current_date.day == 15 and random.random() < 0.35:
                div_amount = round(random.uniform(2500.0, 7500.0), 2)
                ref = generate_reference_number(current_date, seq_counter)
                seq_counter += 1
                transactions.append(
                    {
                        "account_id": account_id,
                        "user_id": user_id,
                        "description_ar": "توزيعات أرباح استثمارية - صندوق مرخص",
                        "description_en": "Investment dividend yield - Licensed Fund",
                        "merchant_ar": "صندوق استثماري مرخص",
                        "merchant_en": "Licensed Investment Fund",
                        "amount": div_amount,
                        "type": "credit",
                        "category": "Investments",
                        "transaction_date": current_date.isoformat(),
                        "reference_number": ref,
                        "source": "fath_demo",
                    }
                )

        elif archetype == "average":
            # Average: Balanced lifestyle, grocery, food delivery, telecom
            if random.random() < 0.55 and is_primary_checking:
                num_txns = random.choice([1, 2])
                for _ in range(num_txns):
                    m = random.choice(MERCHANT_CATALOG)
                    amount = round(random.uniform(m["min_amount"], m["max_amount"] * 0.75), 2)
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": m["desc_ar"],
                            "description_en": m["desc_en"],
                            "merchant_ar": m["merchant_ar"],
                            "merchant_en": m["merchant_en"],
                            "amount": amount,
                            "type": m["type"],
                            "category": m["category"],
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

        elif archetype == "stressed":
            # Stressed: Irregular gig incomes, micro expenses, high expense-to-income ratio
            if is_primary_checking:
                # Irregular gig credits throughout the month (2-3 times per month)
                if random.random() < 0.08:
                    gig_amount = round(random.uniform(250.0, 850.0), 2)
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": "إيداع أرباح توصيل ونقل - منصة رقمية",
                            "description_en": "Gig delivery payout - Digital platform",
                            "merchant_ar": "منصة التوصيل المرخصة",
                            "merchant_en": "Licensed Delivery Platform",
                            "amount": gig_amount,
                            "type": "credit",
                            "category": "Salary & Income",
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

                # Monthly rent / installment burden on the 1st
                if current_date.day == 1:
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": "سداد إيجار مسكن - منصة إيجار",
                            "description_en": "Housing rent payment - Ejar platform",
                            "merchant_ar": "منصة إيجار الوطنية",
                            "merchant_en": "National Ejar Platform",
                            "amount": 1600.0,
                            "type": "debit",
                            "category": "Housing & Rent",
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

                # Frequent micro food/fuel expenses
                if random.random() < 0.50:
                    micro_choices = [
                        m for m in MERCHANT_CATALOG if m["category"] in ("Food & Dining", "Transportation & Fuel")
                    ]
                    m = random.choice(micro_choices)
                    amount = round(random.uniform(18.0, 75.0), 2)
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": m["desc_ar"],
                            "description_en": m["desc_en"],
                            "merchant_ar": m["merchant_ar"],
                            "merchant_en": m["merchant_en"],
                            "amount": amount,
                            "type": "debit",
                            "category": m["category"],
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

        elif archetype == "flagged":
            # Flagged: High velocity round-numbers, rapid dispersal, overdraft trigger
            if is_primary_checking:
                # Regular modest salary
                if current_date.day == 27:
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": f"إيداع راتب شهري - {employer_ar}",
                            "description_en": f"Monthly payroll credit - {employer_en}",
                            "merchant_ar": employer_ar,
                            "merchant_en": employer_en,
                            "amount": 14000.0,
                            "type": "credit",
                            "category": "Salary & Income",
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

                # Standard everyday debit
                if random.random() < 0.25:
                    m = random.choice(MERCHANT_CATALOG)
                    amount = round(random.uniform(m["min_amount"], m["max_amount"] * 0.8), 2)
                    ref = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": m["desc_ar"],
                            "description_en": m["desc_en"],
                            "merchant_ar": m["merchant_ar"],
                            "merchant_en": m["merchant_en"],
                            "amount": amount,
                            "type": "debit",
                            "category": m["category"],
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref,
                            "source": "fath_demo",
                        }
                    )

                # Periodic suspicious high-value transfers (inflow immediately followed by rapid outflow)
                if random.random() < 0.04:
                    susp_amount = random.choice([49500.0, 48900.0, 49800.0, 47500.0])
                    ref_in = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": "تحويل مالي وارد عالي القيمة - حساب طرف ثالث مجهول",
                            "description_en": "High-value incoming transfer - Unverified 3rd party",
                            "merchant_ar": "طرف ثالث مجهول",
                            "merchant_en": "Unverified 3rd Party",
                            "amount": susp_amount,
                            "type": "credit",
                            "category": "Transfers",
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref_in,
                            "source": "fath_demo",
                        }
                    )

                    # Immediate rapid dispersal outward on same day or next day + fees leading to overdraft risk
                    ref_out = generate_reference_number(current_date, seq_counter)
                    seq_counter += 1
                    transactions.append(
                        {
                            "account_id": account_id,
                            "user_id": user_id,
                            "description_ar": "تحويل فوري سريع خارج النظام المصرفي - تشتيت سيولة",
                            "description_en": "Rapid outbound transfer - Immediate fund dispersal",
                            "merchant_ar": "مستفيد خارجي غير معتمد",
                            "merchant_en": "Unapproved Beneficiary",
                            "amount": susp_amount + random.choice([150.0, 250.0]),
                            "type": "debit",
                            "category": "Transfers",
                            "transaction_date": current_date.isoformat(),
                            "reference_number": ref_out,
                            "source": "fath_demo",
                        }
                    )

        current_date += timedelta(days=1)

    return transactions


def recalculate_account_balance(
    base_balance: float, transactions: list[dict[str, Any]], archetype: str = "average"
) -> float:
    """Compute true live balance based on credits and debits."""
    current = base_balance
    for txn in transactions:
        if txn["type"] == "credit":
            current += txn["amount"]
        elif txn["type"] == "debit":
            current -= txn["amount"]

    if archetype == "stressed":
        return round(max(current, 320.50), 2)
    elif archetype == "flagged":
        # Allow negative/overdraft or slim balance
        return round(current, 2)
    return round(max(current, 1500.0), 2)


def seed_mock_bank_data(
    db_path: Path | str | None = None,
    force_reset: bool = False,
    months_history: int = 8,
) -> dict[str, int]:
    """Populate database with synthetic Saudi users, multi-bank accounts, and ledgers."""
    init_db(db_path)

    stats = {
        "users_seeded": 0,
        "accounts_seeded": 0,
        "transactions_seeded": 0,
    }

    with get_connection(db_path) as conn:
        if force_reset:
            conn.execute("DELETE FROM mock_transactions WHERE source = 'fath_demo'")
            conn.execute("DELETE FROM mock_accounts")
            conn.execute("DELETE FROM mock_users")
            conn.execute("DELETE FROM audit_logs")

        # Check existing seeded users
        existing_users = {
            row["username"]: row["id"]
            for row in conn.execute("SELECT username, id FROM mock_users").fetchall()
        }

        account_seq = 1000

        for archetype_key, profile in ARCHETYPE_PROFILES.items():
            username = profile["username"]
            if username in existing_users:
                continue

            # 1. Insert User
            cursor = conn.execute(
                """
                INSERT INTO mock_users (username, full_name_ar, full_name_en, national_id, archetype)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    username,
                    profile["full_name_ar"],
                    profile["full_name_en"],
                    profile["national_id"],
                    archetype_key,
                ),
            )
            user_id = cursor.lastrowid
            stats["users_seeded"] += 1

            # 2. Insert Accounts across Fictional Banks
            for acc_spec in profile["accounts"]:
                bank = MOCK_BANKS[acc_spec["bank_idx"]]
                account_seq += 1
                iban = calculate_saudi_iban(bank["code"], f"{account_seq:010d}")

                acc_cursor = conn.execute(
                    """
                    INSERT INTO mock_accounts (user_id, bank_name_ar, bank_name_en, iban, account_type, currency, balance)
                    VALUES (?, ?, ?, ?, ?, 'SAR', ?)
                    """,
                    (
                        user_id,
                        bank["name_ar"],
                        bank["name_en"],
                        iban,
                        acc_spec["account_type"],
                        acc_spec["base_balance"],
                    ),
                )
                account_id = acc_cursor.lastrowid
                stats["accounts_seeded"] += 1

                # 3. Generate Transactions
                txns = generate_archetype_transactions(
                    account_id=account_id,
                    user_id=user_id,
                    archetype=archetype_key,
                    months_history=months_history,
                    is_primary_checking=acc_spec["is_primary"],
                    profile_data=profile,
                )

                # Compute final accurate balance
                final_balance = recalculate_account_balance(
                    acc_spec["base_balance"], txns, archetype=archetype_key
                )
                conn.execute(
                    "UPDATE mock_accounts SET balance = ? WHERE id = ?",
                    (final_balance, account_id),
                )

                # Insert transactions
                for t in txns:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO mock_transactions (
                            account_id, user_id, description_ar, description_en,
                            merchant_ar, merchant_en, amount, type, category,
                            transaction_date, reference_number, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t["account_id"],
                            t["user_id"],
                            t["description_ar"],
                            t["description_en"],
                            t["merchant_ar"],
                            t["merchant_en"],
                            t["amount"],
                            t["type"],
                            t["category"],
                            t["transaction_date"],
                            t["reference_number"],
                            t["source"],
                        ),
                    )
                    stats["transactions_seeded"] += 1

            # Log audit event for data generation
            log_event(
                conn,
                user_id,
                "DEMO_USER_SEEDED",
                {
                    "archetype": archetype_key,
                    "accounts_count": len(profile["accounts"]),
                    "history_months": months_history,
                },
            )

    return stats
