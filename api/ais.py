"""Account Information Services (AIS) REST API for Fath (فتح).

Implements SAMA Open Banking Framework Technical Standards v1.0 for AIS:
- GET /open-banking/v1/accounts
- GET /open-banking/v1/accounts/{accountId}
- GET /open-banking/v1/accounts/{accountId}/balances
- GET /open-banking/v1/accounts/{accountId}/transactions

Features:
- Bearer token authentication & PKCE consent validation
- Granular scope enforcement (accounts:read, balances:read, transactions:read)
- Strict multi-tenant user isolation (prevents cross-account data leakage)
- Bilingual payloads (Arabic and English)
- SAMA metadata and regulatory audit logging
"""

from __future__ import annotations

import math
from typing import Any

from flask import Blueprint, jsonify, request

from auth.oauth import require_oauth_token
from data.mock_bank import SAMA_SANDBOX_DISCLAIMER
from database import get_connection, log_event

ais_bp = Blueprint("ais", __name__, url_prefix="/open-banking/v1")


def _format_account(acc: dict[str, Any]) -> dict[str, Any]:
    """Format account record according to SAMA Open Banking JSON schema."""
    return {
        "accountId": acc["id"],
        "bankName": {
            "ar": acc["bank_name_ar"],
            "en": acc["bank_name_en"],
        },
        "iban": acc["iban"],
        "accountType": acc["account_type"],
        "currency": acc["currency"],
        "status": "Enabled",
        "createdAt": acc["created_at"],
    }


def _format_transaction(txn: dict[str, Any]) -> dict[str, Any]:
    """Format transaction ledger record according to SAMA Open Banking schema."""
    return {
        "transactionId": txn["id"],
        "accountId": txn["account_id"],
        "amount": round(txn["amount"], 2),
        "currency": "SAR",
        "creditDebitIndicator": "Credit" if txn["type"] == "credit" else "Debit",
        "status": "Booked",
        "bookingDateTime": txn["transaction_date"],
        "description": {
            "ar": txn["description_ar"],
            "en": txn["description_en"],
        },
        "merchant": {
            "ar": txn["merchant_ar"] or "",
            "en": txn["merchant_en"] or "",
        },
        "category": txn["category"],
        "referenceNumber": txn["reference_number"],
    }


@ais_bp.route("/accounts", methods=["GET"])
@require_oauth_token(required_scope="accounts:read")
def get_accounts(oauth_grant: dict[str, Any]):
    """List all accounts for the authenticated consented user."""
    user_id = oauth_grant["user_id"]
    client_id = oauth_grant["client_id"]

    page = max(1, int(request.args.get("page", 1)))
    page_size = min(100, max(1, int(request.args.get("pageSize", 25))))
    offset = (page - 1) * page_size

    with get_connection() as conn:
        total_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM mock_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()["cnt"]

        rows = conn.execute(
            """
            SELECT * FROM mock_accounts
            WHERE user_id = ?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (user_id, page_size, offset),
        ).fetchall()

        log_event(
            conn,
            user_id,
            "AIS_ACCOUNTS_ACCESSED",
            {"client_id": client_id, "accounts_returned": len(rows)},
        )

    data = [_format_account(dict(r)) for r in rows]
    total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

    return jsonify({
        "data": data,
        "meta": {
            "total": total_count,
            "page": page,
            "pageSize": page_size,
            "totalPages": total_pages,
            "disclaimer": SAMA_SANDBOX_DISCLAIMER,
        },
    }), 200


@ais_bp.route("/accounts/<int:account_id>", methods=["GET"])
@require_oauth_token(required_scope="accounts:read")
def get_account_detail(account_id: int, oauth_grant: dict[str, Any]):
    """Retrieve detailed information for a specific account with strict user isolation."""
    user_id = oauth_grant["user_id"]
    client_id = oauth_grant["client_id"]

    with get_connection() as conn:
        acc = conn.execute(
            "SELECT * FROM mock_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        ).fetchone()

        if not acc:
            # 404 returned to prevent enumeration attacks across other users' accounts
            return jsonify({
                "error": "not_found",
                "error_description": f"Account with ID {account_id} not found or unauthorized.",
            }), 404

        log_event(
            conn,
            user_id,
            "AIS_ACCOUNT_DETAIL_ACCESSED",
            {"client_id": client_id, "account_id": account_id},
        )

        return jsonify({
            "data": _format_account(dict(acc)),
            "meta": {"disclaimer": SAMA_SANDBOX_DISCLAIMER},
        }), 200


@ais_bp.route("/accounts/<int:account_id>/balances", methods=["GET"])
@require_oauth_token(required_scope="balances:read")
def get_account_balances(account_id: int, oauth_grant: dict[str, Any]):
    """Retrieve live available and ledger balances for a verified account."""
    user_id = oauth_grant["user_id"]
    client_id = oauth_grant["client_id"]

    with get_connection() as conn:
        acc = conn.execute(
            "SELECT * FROM mock_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        ).fetchone()

        if not acc:
            return jsonify({
                "error": "not_found",
                "error_description": f"Account with ID {account_id} not found or unauthorized.",
            }), 404

        balance_val = float(acc["balance"])

        log_event(
            conn,
            user_id,
            "AIS_BALANCES_ACCESSED",
            {"client_id": client_id, "account_id": account_id},
        )

        return jsonify({
            "data": [
                {
                    "accountId": acc["id"],
                    "iban": acc["iban"],
                    "currency": acc["currency"],
                    "amount": round(balance_val, 2),
                    "balanceType": "InterimAvailable",
                    "creditDebitIndicator": "Credit" if balance_val >= 0 else "Debit",
                    "dateTime": acc["created_at"],
                },
                {
                    "accountId": acc["id"],
                    "iban": acc["iban"],
                    "currency": acc["currency"],
                    "amount": round(balance_val, 2),
                    "balanceType": "InterimBooked",
                    "creditDebitIndicator": "Credit" if balance_val >= 0 else "Debit",
                    "dateTime": acc["created_at"],
                },
            ],
            "meta": {"disclaimer": SAMA_SANDBOX_DISCLAIMER},
        }), 200


@ais_bp.route("/accounts/<int:account_id>/transactions", methods=["GET"])
@require_oauth_token(required_scope="transactions:read")
def get_account_transactions(account_id: int, oauth_grant: dict[str, Any]):
    """Retrieve historical transaction ledgers for an account with filtering and pagination."""
    user_id = oauth_grant["user_id"]
    client_id = oauth_grant["client_id"]

    page = max(1, int(request.args.get("page", 1)))
    page_size = min(100, max(1, int(request.args.get("pageSize", 25))))
    offset = (page - 1) * page_size

    from_date = request.args.get("fromBookingDateTime")
    to_date = request.args.get("toBookingDateTime")

    with get_connection() as conn:
        acc = conn.execute(
            "SELECT id FROM mock_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        ).fetchone()

        if not acc:
            return jsonify({
                "error": "not_found",
                "error_description": f"Account with ID {account_id} not found or unauthorized.",
            }), 404

        # Dynamic query with date filters
        query_conditions = ["account_id = ?", "user_id = ?"]
        params: list[Any] = [account_id, user_id]

        if from_date:
            query_conditions.append("transaction_date >= ?")
            params.append(from_date)
        if to_date:
            query_conditions.append("transaction_date <= ?")
            params.append(to_date)

        where_clause = " AND ".join(query_conditions)

        total_count = conn.execute(
            f"SELECT COUNT(*) as cnt FROM mock_transactions WHERE {where_clause}",
            params,
        ).fetchone()["cnt"]

        rows = conn.execute(
            f"""
            SELECT * FROM mock_transactions
            WHERE {where_clause}
            ORDER BY transaction_date DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()

        log_event(
            conn,
            user_id,
            "AIS_TRANSACTIONS_ACCESSED",
            {
                "client_id": client_id,
                "account_id": account_id,
                "count": len(rows),
                "page": page,
            },
        )

    data = [_format_transaction(dict(r)) for r in rows]
    total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1

    return jsonify({
        "data": data,
        "meta": {
            "total": total_count,
            "page": page,
            "pageSize": page_size,
            "totalPages": total_pages,
            "disclaimer": SAMA_SANDBOX_DISCLAIMER,
        },
    }), 200
