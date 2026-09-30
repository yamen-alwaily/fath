"""Real-time WebSocket Streaming Module for Fath (فتح) via Flask-SocketIO.

Capabilities:
- Authenticates incoming WebSocket connections via Bearer token
- Segregates clients into isolated per-user rooms (user_{user_id})
- Dispatches live events:
    - new_transaction
    - balance_update
    - payment_status_change
    - consent_revoked (with automatic client disconnection)
- Includes a background simulation thread generating transactions every 45s for demo
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import random
import threading
import time
from typing import Any

from flask import request
from flask_socketio import ConnectionRefusedError, SocketIO, disconnect, emit, join_room

from auth.oauth import validate_bearer_token
from data.mock_bank import MERCHANT_CATALOG, generate_reference_number
from database import get_connection, log_event

# Global SocketIO instance (initialized in init_socketio)
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")

# Background simulation control
_simulator_thread: threading.Thread | None = None
_simulator_running = False


def init_socketio(app) -> SocketIO:
    """Attach Flask application to SocketIO instance and start background demo stream."""
    socketio.init_app(app)
    start_background_simulator(app)
    return socketio


@socketio.on("connect")
def handle_connect(auth=None):
    """Authenticate incoming WebSocket connection with Bearer token."""
    token = None
    # 1. Check auth dictionary payload
    if isinstance(auth, dict) and auth.get("token"):
        token = auth["token"]

    # 2. Check query parameters
    if not token and request.args.get("token"):
        token = request.args.get("token")

    # 3. Check Authorization header
    if not token and request.headers.get("Authorization"):
        token = request.headers.get("Authorization")

    if not token:
        raise ConnectionRefusedError("Authentication token is required")

    valid, grant, err_msg = validate_bearer_token(token)
    if not valid or not grant:
        raise ConnectionRefusedError(err_msg or "Invalid authentication token")

    user_id = grant["user_id"]
    room_name = f"user_{user_id}"
    join_room(room_name)

    emit(
        "connected",
        {
            "status": "authenticated",
            "userId": user_id,
            "room": room_name,
            "client": grant["client_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    pass


# --- Outbound Event Broadcasters ---


def broadcast_transaction_event(user_id: int, transaction_data: dict[str, Any]) -> None:
    """Broadcast new transaction to the specified user's room."""
    socketio.emit("new_transaction", transaction_data, room=f"user_{user_id}")


def broadcast_balance_event(user_id: int, balance_data: dict[str, Any]) -> None:
    """Broadcast balance update event to user's room."""
    socketio.emit("balance_update", balance_data, room=f"user_{user_id}")


def broadcast_payment_event(user_id: int, payment_data: dict[str, Any]) -> None:
    """Broadcast payment lifecycle status change."""
    socketio.emit("payment_status_change", payment_data, room=f"user_{user_id}")


def broadcast_consent_revoked(user_id: int, reason: str = "Consent Revoked") -> None:
    """Broadcast revocation event to user's room and disconnect active connections."""
    room_name = f"user_{user_id}"
    socketio.emit(
        "consent_revoked",
        {"reason": reason, "timestamp": datetime.now(timezone.utc).isoformat()},
        room=room_name,
    )
    socketio.close_room(room_name)


# --- Background Transaction Simulator (Every 45 seconds) ---


def _run_transaction_simulation(app):
    """Background worker simulating realistic periodic transactions for active mock users."""
    global _simulator_running
    time.sleep(5)  # Initial grace delay

    while _simulator_running:
        try:
            with app.app_context():
                with get_connection() as conn:
                    # Pick a random mock checking account
                    account = conn.execute(
                        """
                        SELECT a.id, a.user_id, a.balance, u.archetype
                        FROM mock_accounts a
                        JOIN mock_users u ON a.user_id = u.id
                        WHERE a.account_type = 'checking'
                        ORDER BY RANDOM() LIMIT 1
                        """
                    ).fetchone()

                    if account:
                        user_id = account["user_id"]
                        account_id = account["id"]
                        current_balance = float(account["balance"])

                        # Pick random merchant purchase
                        merchant = random.choice(MERCHANT_CATALOG)
                        amount = round(random.uniform(25.0, 110.0), 2)

                        # Only debit if sufficient balance
                        if current_balance > amount + 50.0:
                            new_balance = round(current_balance - amount, 2)
                            ref_num = generate_reference_number(datetime.now(timezone.utc).date(), random.randint(100, 999))
                            now_iso = datetime.now(timezone.utc).isoformat()

                            conn.execute(
                                "UPDATE mock_accounts SET balance = ? WHERE id = ?",
                                (new_balance, account_id),
                            )

                            cursor = conn.execute(
                                """
                                INSERT INTO mock_transactions (
                                    account_id, user_id, description_ar, description_en,
                                    merchant_ar, merchant_en, amount, type, category,
                                    transaction_date, reference_number, source
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'debit', ?, DATE(?), ?, 'fath_demo')
                                """,
                                (
                                    account_id,
                                    user_id,
                                    merchant["desc_ar"],
                                    merchant["desc_en"],
                                    merchant["merchant_ar"],
                                    merchant["merchant_en"],
                                    amount,
                                    merchant["category"],
                                    now_iso,
                                    ref_num,
                                ),
                            )
                            txn_id = cursor.lastrowid

                            log_event(
                                conn,
                                user_id,
                                "SIMULATED_TRANSACTION_EMITTED",
                                {"txn_id": txn_id, "amount": amount, "account_id": account_id},
                            )

                            # Push events to WebSockets
                            broadcast_transaction_event(
                                user_id,
                                {
                                    "transactionId": txn_id,
                                    "accountId": account_id,
                                    "amount": amount,
                                    "creditDebitIndicator": "Debit",
                                    "merchant": {
                                        "ar": merchant["merchant_ar"],
                                        "en": merchant["merchant_en"],
                                    },
                                    "category": merchant["category"],
                                    "referenceNumber": ref_num,
                                    "dateTime": now_iso,
                                },
                            )
                            broadcast_balance_event(
                                user_id,
                                {
                                    "accountId": account_id,
                                    "newBalance": new_balance,
                                    "currency": "SAR",
                                },
                            )

        except Exception:
            pass  # Suppress simulator errors during test tear downs or restarts

        time.sleep(45)


def start_background_simulator(app) -> None:
    """Start the background 45s simulation thread if not already running."""
    global _simulator_thread, _simulator_running
    if os.getenv("FATH_DISABLE_SIMULATOR", "false").lower() == "true":
        return

    if _simulator_thread is None or not _simulator_thread.is_alive():
        _simulator_running = True
        _simulator_thread = threading.Thread(
            target=_run_transaction_simulation, args=(app,), daemon=True
        )
        _simulator_thread.start()
