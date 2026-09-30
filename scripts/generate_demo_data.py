#!/usr/bin/env python3
"""CLI utility to generate synthetic Saudi open banking data for Fath (فتح).

Usage:
    python scripts/generate_demo_data.py [--reset] [--months 8] [--db path/to/fath.db]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.mock_bank import SAMA_SANDBOX_DISCLAIMER, seed_mock_bank_data
from database import get_connection, init_db


def print_banner() -> None:
    print("=" * 80)
    print(" 🇸🇦 FATH (فتح) — Saudi Open Banking Sandbox Data Generator")
    print(" بيئة اختبار البنك المفتوح للمطورين")
    print("=" * 80)
    print(f"\n⚠️  DISCLAIMER:\n{SAMA_SANDBOX_DISCLAIMER}\n")
    print("-" * 80)


def display_seeded_summary(db_path: Path | str | None = None) -> None:
    """Print an ASCII breakdown of all seeded users, accounts, and transactions."""
    with get_connection(db_path) as conn:
        users = conn.execute(
            "SELECT id, username, full_name_ar, full_name_en, national_id, archetype FROM mock_users ORDER BY id"
        ).fetchall()

        if not users:
            print("No mock users found in database.")
            return

        print(f"\n{'ID':<4} | {'Archetype':<10} | {'Name (EN)':<28} | {'National ID':<12} | {'Accounts':<9} | {'Txns':<6}")
        print("-" * 80)

        for u in users:
            accounts = conn.execute(
                "SELECT id, bank_name_en, iban, account_type, balance FROM mock_accounts WHERE user_id = ?",
                (u["id"],),
            ).fetchall()
            txn_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM mock_transactions WHERE user_id = ?",
                (u["id"],),
            ).fetchone()["cnt"]

            print(
                f"{u['id']:<4} | {u['archetype']:<10} | {u['full_name_en']:<28} | {u['national_id']:<12} | {len(accounts):<9} | {txn_count:<6}"
            )
            for acc in accounts:
                print(
                    f"     └── [{acc['account_type'].upper():<10}] {acc['bank_name_en']:<32} | {acc['iban']} | SAR {acc['balance']:>10,.2f}"
                )
            print()

        total_txns = conn.execute("SELECT COUNT(*) as cnt FROM mock_transactions").fetchone()["cnt"]
        total_accounts = conn.execute("SELECT COUNT(*) as cnt FROM mock_accounts").fetchone()["cnt"]
        print("-" * 80)
        print(f"Total Users: {len(users)} | Total Accounts: {total_accounts} | Total Ledger Entries: {total_txns:,}")
        print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic Saudi banking demo data for Fath.")
    parser.add_argument("--reset", action="store_true", help="Wipe existing demo data before seeding.")
    parser.add_argument("--months", type=int, default=8, help="Number of months of historical transactions (default: 8).")
    parser.add_argument("--db", type=str, default=None, help="Custom SQLite database path.")

    args = parser.parse_args()

    print_banner()

    target_db = args.db or os.getenv("FATH_DB_PATH")
    print(f"Target Database: {target_db or 'Default fath.db'}")
    print(f"History Span   : {args.months} months")
    print(f"Force Reset    : {args.reset}\n")

    stats = seed_mock_bank_data(
        db_path=target_db,
        force_reset=args.reset,
        months_history=args.months,
    )

    print("✅ Seeding process completed successfully!")
    print(f"   - Users Created/Verified: {stats['users_seeded']}")
    print(f"   - Accounts Created       : {stats['accounts_seeded']}")
    print(f"   - Transactions Generated : {stats['transactions_seeded']:,}")

    display_seeded_summary(db_path=target_db)


if __name__ == "__main__":
    main()
