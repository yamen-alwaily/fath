"""Database layer and schema definitions for Fath (فتح) — Saudi Open Banking Sandbox.

Implements SQLite storage with foreign keys, audit logging, additive migrations,
and connection pooling matching the Smart Wealth architectural pattern.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

# Default database path can be overridden by FATH_DB_PATH
DEFAULT_DB_FILENAME = "fath.db"


def get_db_path(custom_path: Path | str | None = None) -> Path:
    """Resolve the active database path based on parameter or environment."""
    if custom_path is not None:
        return Path(custom_path)
    env_path = os.getenv("FATH_DB_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent / DEFAULT_DB_FILENAME


class DatabaseConnection(sqlite3.Connection):
    """SQLite connection context manager that commits on clean exit, rolls back on error, and always closes."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return an active SQLite connection configured with row_factory and foreign keys."""
    resolved_path = get_db_path(db_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved_path, factory=DatabaseConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _add_column_if_missing(connection: sqlite3.Connection, table: str, definition: str) -> None:
    """Safely apply additive column migrations without dropping existing data."""
    column = definition.split()[0]
    cursor = connection.execute(f"PRAGMA table_info({table})")
    existing_columns = {row["name"] for row in cursor.fetchall()}
    if column not in existing_columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db(db_path: Path | str | None = None) -> None:
    """Initialize all Fath database tables, indexes, and default configurations."""
    with get_connection(db_path) as connection:
        connection.executescript(
            """
            -- 1. Mock Users (Simulated Saudi Citizens & Residents)
            CREATE TABLE IF NOT EXISTS mock_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                full_name_ar TEXT NOT NULL,
                full_name_en TEXT NOT NULL,
                national_id TEXT UNIQUE NOT NULL,
                archetype TEXT NOT NULL DEFAULT 'average' CHECK(archetype IN ('prime', 'average', 'stressed', 'flagged')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- 2. Mock Accounts (Simulated Saudi Bank Accounts across ASPSPs)
            CREATE TABLE IF NOT EXISTS mock_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bank_name_ar TEXT NOT NULL,
                bank_name_en TEXT NOT NULL,
                iban TEXT UNIQUE NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'checking' CHECK(account_type IN ('checking', 'savings', 'credit', 'investment')),
                currency TEXT NOT NULL DEFAULT 'SAR',
                balance REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES mock_users(id) ON DELETE CASCADE
            );

            -- 3. Mock Transactions (AIS Data Layer with 6-12 Months History)
            CREATE TABLE IF NOT EXISTS mock_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                description_ar TEXT NOT NULL,
                description_en TEXT NOT NULL,
                merchant_ar TEXT,
                merchant_en TEXT,
                amount REAL NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('debit', 'credit')),
                category TEXT NOT NULL,
                transaction_date TEXT NOT NULL DEFAULT CURRENT_DATE,
                reference_number TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'fath_demo',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES mock_accounts(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES mock_users(id) ON DELETE CASCADE,
                UNIQUE(user_id, source, reference_number)
            );

            -- 4. OAuth Clients (Third-Party AISP/PISP Fintech Apps)
            CREATE TABLE IF NOT EXISTS oauth_clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                scopes TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- 5. Consent Grants (Explicit Grants with PKCE, Scopes, and Token Expiry)
            CREATE TABLE IF NOT EXISTS consent_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                scopes TEXT NOT NULL,
                code_challenge TEXT,
                code_challenge_method TEXT DEFAULT 'S256',
                auth_code TEXT UNIQUE,
                auth_code_used INTEGER NOT NULL DEFAULT 0,
                access_token TEXT UNIQUE,
                token_expires_at TEXT,
                revoked_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES mock_users(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id) ON DELETE CASCADE
            );

            -- 6. Payment Initiations (PIS State Machine: Pending -> Processing -> Completed/Rejected)
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                from_account_id INTEGER NOT NULL,
                to_iban TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                currency TEXT NOT NULL DEFAULT 'SAR',
                reference TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Processing', 'Completed', 'Rejected')),
                rejection_reason TEXT,
                initiated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES mock_users(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
                FOREIGN KEY (from_account_id) REFERENCES mock_accounts(id) ON DELETE RESTRICT
            );

            -- 7. Audit Logs (Compliance & Security Trail for Open Banking Events)
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES mock_users(id) ON DELETE SET NULL
            );
            """
        )

        # Migrations / Additive columns if existing database schema was older
        _add_column_if_missing(connection, "mock_users", "archetype TEXT NOT NULL DEFAULT 'average'")
        _add_column_if_missing(connection, "mock_transactions", "source TEXT NOT NULL DEFAULT 'fath_demo'")
        _add_column_if_missing(connection, "payments", "rejection_reason TEXT")

        # Indexes for query performance and relational integrity
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mock_accounts_user ON mock_accounts(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mock_accounts_iban ON mock_accounts(iban)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mock_transactions_acc_date "
            "ON mock_transactions(account_id, transaction_date DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_mock_transactions_user_date "
            "ON mock_transactions(user_id, transaction_date DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_consent_grants_token ON consent_grants(access_token)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_consent_grants_code ON consent_grants(auth_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_consent_grants_user_client "
            "ON consent_grants(user_id, client_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, initiated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_client ON payments(client_id, initiated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_event "
            "ON audit_logs(event_type, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_user "
            "ON audit_logs(user_id, created_at DESC)"
        )

        # Seed default sandbox client if none exists
        default_client_id = "sandbox_client_demo"
        existing = connection.execute(
            "SELECT id FROM oauth_clients WHERE client_id = ?", (default_client_id,)
        ).fetchone()
        if not existing:
            connection.execute(
                """
                INSERT INTO oauth_clients (client_id, client_name, redirect_uri, scopes)
                VALUES (?, ?, ?, ?)
                """,
                (
                    default_client_id,
                    "Fath Developer Portal (Sandbox App)",
                    "http://localhost:5000/callback",
                    "accounts:read balances:read transactions:read payments:write",
                ),
            )


def log_event(
    connection: sqlite3.Connection,
    user_id: int | None,
    event_type: str,
    details: str | Mapping[str, Any] = "",
) -> None:
    """Record an audit trail event for security, consent, and financial operations."""
    if isinstance(details, (dict, list)):
        details_str = json.dumps(details, ensure_ascii=False)
    else:
        details_str = str(details)

    connection.execute(
        "INSERT INTO audit_logs (user_id, event_type, details) VALUES (?, ?, ?)",
        (user_id, event_type, details_str),
    )


# --- Reusable Helper Functions ---


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a sqlite3.Row instance into a standard Python dictionary."""
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of sqlite3.Row instances into a list of standard dictionaries."""
    return [dict(row) for row in rows]
