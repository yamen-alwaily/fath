"""Payment Initiation Services (PIS) REST API for Fath (فتح).

Implements SAMA Open Banking Framework Technical Standards v1.0 for PIS:
- POST /open-banking/v1/payments
- GET /open-banking/v1/payments/{paymentId}

Lifecycle:
- Pending -> Processing -> Completed (or Rejected upon insufficient funds)
- Atomic deduction of account balance on Completed
- Dynamic creation of matching debit ledger entry in mock_transactions
- SAMA regulatory compliance and audit logging
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from flask import Blueprint, jsonify, request

from auth.oauth import require_oauth_token
from data.mock_bank import SAMA_SANDBOX_DISCLAIMER
from database import get_connection, log_event

pis_bp = Blueprint("pis", __name__, url_prefix="/open-banking/v1")


def _format_payment(p: dict[str, Any]) -> dict[str, Any]:
    """Format payment object according to SAMA PIS JSON schema."""
    return {
        "paymentId": p["id"],
        "fromAccountId": p["from_account_id"],
        "toIban": p["to_iban"],
        "amount": round(float(p["amount"]), 2),
        "currency": p["currency"],
        "reference": p["reference"],
        "status": p["status"],
        "rejectionReason": p.get("rejection_reason"),
        "initiatedAt": p["initiated_at"],
        "completedAt": p["completed_at"],
    }


@pis_bp.route("/payments", methods=["POST"])
@require_oauth_token(required_scope="payments:write")
def initiate_payment(oauth_grant: dict[str, Any]):
    """Initiate a single immediate payment order (PIS)."""
    user_id = oauth_grant["user_id"]
    client_id = oauth_grant["client_id"]

    body = request.get_json(silent=True) or {}
    from_account_id = body.get("fromAccountId")
    to_iban = (body.get("toIban") or "").strip().upper()
    amount_raw = body.get("amount")
    currency = body.get("currency", "SAR").strip().upper()
    reference = (body.get("reference") or "Payment Order").strip()

    # 1. Validation
    if not from_account_id:
        return jsonify({"error": "invalid_request", "error_description": "fromAccountId is required"}), 400

    if not to_iban or len(to_iban) < 15 or not to_iban.startswith("SA"):
        return jsonify({
            "error": "invalid_request",
            "error_description": "Invalid Saudi IBAN format (must start with 'SA' and be at least 15 characters)",
        }), 400

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_request", "error_description": "amount must be a positive number"}), 400

    if currency != "SAR":
        return jsonify({"error": "unsupported_currency", "error_description": "Only 'SAR' currency is supported"}), 400

    with get_connection() as conn:
        # 2. Check source account ownership
        account = conn.execute(
            "SELECT * FROM mock_accounts WHERE id = ? AND user_id = ?",
            (from_account_id, user_id),
        ).fetchone()

        if not account:
            return jsonify({
                "error": "not_found",
                "error_description": f"Source account {from_account_id} not found or unauthorized for this user.",
            }), 404

        current_balance = float(account["balance"])
        now_str = datetime.now(timezone.utc).isoformat()

        # 3. Check sufficiency of funds
        if current_balance < amount:
            # Rejection flow
            cursor = conn.execute(
                """
                INSERT INTO payments (
                    user_id, client_id, from_account_id, to_iban,
                    amount, currency, reference, status, rejection_reason,
                    initiated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Rejected', ?, ?, ?)
                """,
                (
                    user_id,
                    client_id,
                    from_account_id,
                    to_iban,
                    amount,
                    currency,
                    reference,
                    "Insufficient funds in source account",
                    now_str,
                    now_str,
                ),
            )
            payment_id = cursor.lastrowid
            log_event(
                conn,
                user_id,
                "PIS_PAYMENT_REJECTED",
                {
                    "payment_id": payment_id,
                    "reason": "insufficient_funds",
                    "balance": current_balance,
                    "amount": amount,
                },
            )

            rej_payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            return jsonify({
                "data": _format_payment(dict(rej_payment)),
                "meta": {"disclaimer": SAMA_SANDBOX_DISCLAIMER},
            }), 422

        # 4. Success flow: Pending -> Processing -> Completed
        # Deduct balance from mock_accounts
        new_balance = round(current_balance - amount, 2)
        conn.execute(
            "UPDATE mock_accounts SET balance = ? WHERE id = ?",
            (new_balance, from_account_id),
        )

        # Insert Payment Record
        cursor = conn.execute(
            """
            INSERT INTO payments (
                user_id, client_id, from_account_id, to_iban,
                amount, currency, reference, status, initiated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Completed', ?, ?)
            """,
            (
                user_id,
                client_id,
                from_account_id,
                to_iban,
                amount,
                currency,
                reference,
                now_str,
                now_str,
            ),
        )
        payment_id = cursor.lastrowid

        # Insert corresponding transaction into mock_transactions
        txn_ref = f"PIS-{payment_id:05d}-{os.urandom(3).hex().upper()}"
        conn.execute(
            """
            INSERT INTO mock_transactions (
                account_id, user_id, description_ar, description_en,
                merchant_ar, merchant_en, amount, type, category,
                transaction_date, reference_number, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'debit', 'Transfers', DATE(?), ?, 'fath_demo')
            """,
            (
                from_account_id,
                user_id,
                f"حوالة بنك مفتوح إلى {to_iban[:10]}... ({reference})",
                f"Open Banking transfer to {to_iban[:10]}... ({reference})",
                "نظام التحويل الفوري (سريع)",
                "Instant Payment System (Sarie)",
                amount,
                now_str,
                txn_ref,
            ),
        )

        log_event(
            conn,
            user_id,
            "PIS_PAYMENT_COMPLETED",
            {
                "payment_id": payment_id,
                "amount": amount,
                "from_account_id": from_account_id,
                "to_iban": to_iban,
                "new_balance": new_balance,
            },
        )

        # Notify active WebSocket subscribers if stream module is available
        try:
            from ws.stream import broadcast_payment_event, broadcast_balance_event
            broadcast_payment_event(user_id, {
                "paymentId": payment_id,
                "status": "Completed",
                "amount": amount,
                "fromAccountId": from_account_id,
                "toIban": to_iban,
            })
            broadcast_balance_event(user_id, {
                "accountId": from_account_id,
                "newBalance": new_balance,
            })
        except Exception:
            pass

        payment_row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()

    return jsonify({
        "data": _format_payment(dict(payment_row)),
        "meta": {"disclaimer": SAMA_SANDBOX_DISCLAIMER},
    }), 201


@pis_bp.route("/payments/<int:payment_id>", methods=["GET"])
@require_oauth_token()
def get_payment_status(payment_id: int, oauth_grant: dict[str, Any]):
    """Retrieve current status of an initiated payment."""
    user_id = oauth_grant["user_id"]

    with get_connection() as conn:
        payment = conn.execute(
            "SELECT * FROM payments WHERE id = ? AND user_id = ?",
            (payment_id, user_id),
        ).fetchone()

        if not payment:
            return jsonify({
                "error": "not_found",
                "error_description": f"Payment {payment_id} not found or unauthorized.",
            }), 404

        return jsonify({
            "data": _format_payment(dict(payment)),
            "meta": {"disclaimer": SAMA_SANDBOX_DISCLAIMER},
        }), 200
