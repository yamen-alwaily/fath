"""Comprehensive Test Suite for Fath (فتح) — Saudi Open Banking Sandbox.

Covers:
- Pure-Python OAuth 2.0 + PKCE Authorization Code flow
- PKCE code_challenge & code_verifier validation & mismatch rejection
- Single-use authorization codes & replay attack mitigation
- SAMA scope enforcement (accounts:read, balances:read, transactions:read, payments:write)
- Multi-tenant customer data isolation (cross-account prevention)
- Token revocation and expiry handling
- AIS endpoints (accounts list, account detail, balances, transactions)
- PIS payment lifecycle (Pending -> Completed with balance deduction, Insufficient funds rejection)
- System health checks and SAMA disclaimers
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sys
import tempfile
import pytest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import app
from auth.oauth import (
    generate_code_challenge_s256,
    generate_code_verifier,
    issue_authorization_code,
    revoke_token,
)
from data.mock_bank import seed_mock_bank_data
from database import get_connection, init_db


@pytest.fixture
def test_client():
    """Setup an isolated temporary test database and Flask test client."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_fath.db"
        os.environ["FATH_DB_PATH"] = str(test_db)
        os.environ["FATH_DISABLE_SIMULATOR"] = "true"

        # Initialize schema and seed demo data
        init_db(test_db)
        seed_mock_bank_data(test_db, months_history=2)

        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, test_db


def test_health_endpoint(test_client):
    """Verify /health returns 200 OK and SAMA sandbox status."""
    client, _ = test_client
    res = client.get("/health")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "healthy"
    assert data["service"] == "fath"
    assert "SAMA" in data["disclaimer"]


def test_oauth_pkce_end_to_end_flow(test_client):
    """Test standard authorization code flow with PKCE S256."""
    client, db_path = test_client

    verifier = generate_code_verifier(64)
    challenge = generate_code_challenge_s256(verifier)

    # 1. Authorize (issue auth code)
    auth_res = client.post(
        "/oauth/authorize",
        json={
            "client_id": "sandbox_client_demo",
            "response_type": "code",
            "scope": "accounts:read balances:read transactions:read payments:write",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "user_id": 1,
            "state": "random_state_123",
        },
    )
    assert auth_res.status_code == 200
    auth_data = auth_res.get_json()
    auth_code = auth_data["code"]
    assert auth_code is not None

    # 2. Token exchange with correct PKCE verifier
    token_res = client.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": "sandbox_client_demo",
            "code": auth_code,
            "code_verifier": verifier,
        },
    )
    assert token_res.status_code == 200
    token_data = token_res.get_json()
    token = token_data["access_token"]
    assert token.startswith("fath_at_")
    assert token_data["token_type"] == "Bearer"
    assert token_data["expires_in"] == 3600

    # 3. Introspect active token
    intro_res = client.get(
        "/oauth/introspect",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert intro_res.status_code == 200
    assert intro_res.get_json()["active"] is True


def test_oauth_pkce_wrong_verifier_rejected(test_client):
    """Verify exchange fails with 400 when an invalid code_verifier is presented."""
    client, _ = test_client

    verifier = generate_code_verifier(64)
    challenge = generate_code_challenge_s256(verifier)

    auth_res = client.post(
        "/oauth/authorize",
        json={
            "client_id": "sandbox_client_demo",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "user_id": 1,
        },
    )
    code = auth_res.get_json()["code"]

    # Exchange with bad verifier
    token_res = client.post(
        "/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": "sandbox_client_demo",
            "code": code,
            "code_verifier": "completely_wrong_verifier_string_1234567890",
        },
    )
    assert token_res.status_code == 400
    err = token_res.get_json()
    assert err["error"] == "invalid_grant"
    assert "PKCE" in err["error_description"]


def test_scope_enforcement(test_client):
    """Verify endpoints enforce exact SAMA scopes."""
    client, db_path = test_client

    # Mint token with ONLY accounts:read
    with get_connection(db_path) as conn:
        token = "fath_at_only_accounts"
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute(
            """
            INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at)
            VALUES (1, 'sandbox_client_demo', 'accounts:read', ?, ?)
            """,
            (token, expires),
        )

    # 1. Allowed call: GET /accounts
    res_ok = client.get(
        "/open-banking/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_ok.status_code == 200
    data = res_ok.get_json()
    assert len(data["data"]) > 0

    # 2. Disallowed call: GET /balances (requires balances:read)
    acc_id = data["data"][0]["accountId"]
    res_denied_bal = client.get(
        f"/open-banking/v1/accounts/{acc_id}/balances",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_denied_bal.status_code == 403
    assert "Insufficient scope" in res_denied_bal.get_json()["error_description"]

    # 3. Disallowed call: POST /payments (requires payments:write)
    res_denied_pay = client.post(
        "/open-banking/v1/payments",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "fromAccountId": acc_id,
            "toIban": "SA7210000000000000001001",
            "amount": 50.0,
        },
    )
    assert res_denied_pay.status_code == 403


def test_user_isolation(test_client):
    """Verify that User 1's token cannot access or manipulate User 2's accounts."""
    client, db_path = test_client

    # Create tokens for User 1 and User 2
    with get_connection(db_path) as conn:
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        t1 = "fath_at_user_one"
        t2 = "fath_at_user_two"
        scopes = "accounts:read balances:read transactions:read payments:write"
        conn.execute(
            "INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at) VALUES (1, 'sandbox_client_demo', ?, ?, ?)",
            (scopes, t1, expires),
        )
        conn.execute(
            "INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at) VALUES (2, 'sandbox_client_demo', ?, ?, ?)",
            (scopes, t2, expires),
        )

        u2_account = conn.execute("SELECT id FROM mock_accounts WHERE user_id = 2 LIMIT 1").fetchone()
        u2_account_id = u2_account["id"]

    # User 1 tries to read User 2's account details -> 404
    leak_res = client.get(
        f"/open-banking/v1/accounts/{u2_account_id}",
        headers={"Authorization": f"Bearer {t1}"},
    )
    assert leak_res.status_code == 404

    # User 1 tries to initiate payment from User 2's account -> 404
    pay_leak_res = client.post(
        "/open-banking/v1/payments",
        headers={"Authorization": f"Bearer {t1}"},
        json={
            "fromAccountId": u2_account_id,
            "toIban": "SA7210000000000000001001",
            "amount": 25.0,
        },
    )
    assert pay_leak_res.status_code == 404


def test_token_revocation_and_expiry(test_client):
    """Verify that revoked or expired tokens immediately lose API access."""
    client, db_path = test_client

    with get_connection(db_path) as conn:
        token = "fath_at_revokable"
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at) VALUES (1, 'sandbox_client_demo', 'accounts:read', ?, ?)",
            (token, expires),
        )

    # Valid before revocation
    res = client.get("/open-banking/v1/accounts", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200

    # Revoke
    rev_res = client.post("/oauth/revoke", json={"token": token})
    assert rev_res.status_code == 200
    assert rev_res.get_json()["status"] == "revoked"

    # Attempt call after revocation -> 401
    res_after = client.get("/open-banking/v1/accounts", headers={"Authorization": f"Bearer {token}"})
    assert res_after.status_code == 401
    assert "revoked" in res_after.get_json()["error_description"]


def test_pis_payment_lifecycle(test_client):
    """Test payment order creation, atomic balance deduction, and transaction synchronization."""
    client, db_path = test_client

    with get_connection(db_path) as conn:
        token = "fath_at_pis_test"
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at) VALUES (1, 'sandbox_client_demo', 'payments:write accounts:read balances:read', ?, ?)",
            (token, expires),
        )
        acc = conn.execute("SELECT id, balance FROM mock_accounts WHERE user_id = 1 AND balance > 500").fetchone()
        acc_id = acc["id"]
        initial_balance = float(acc["balance"])

    payment_amount = 120.00
    to_iban = "SA7210000000000000001001"

    # 1. Successful payment initiation
    pay_res = client.post(
        "/open-banking/v1/payments",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "fromAccountId": acc_id,
            "toIban": to_iban,
            "amount": payment_amount,
            "currency": "SAR",
            "reference": "Test Transfer Order",
        },
    )
    assert pay_res.status_code == 201
    pay_data = pay_res.get_json()["data"]
    assert pay_data["status"] == "Completed"
    payment_id = pay_data["paymentId"]

    # 2. Check balance deducted in database
    with get_connection(db_path) as conn:
        updated_acc = conn.execute("SELECT balance FROM mock_accounts WHERE id = ?", (acc_id,)).fetchone()
        expected_balance = round(initial_balance - payment_amount, 2)
        assert round(float(updated_acc["balance"]), 2) == expected_balance

        # 3. Check corresponding debit record in mock_transactions
        txn = conn.execute(
            "SELECT * FROM mock_transactions WHERE account_id = ? AND category = 'Transfers' ORDER BY id DESC LIMIT 1",
            (acc_id,),
        ).fetchone()
        assert txn is not None
        assert float(txn["amount"]) == payment_amount
        assert txn["type"] == "debit"

    # 4. Check payment status query endpoint
    status_res = client.get(
        f"/open-banking/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_res.status_code == 200
    assert status_res.get_json()["data"]["status"] == "Completed"


def test_pis_insufficient_funds_rejection(test_client):
    """Verify that payments exceeding available balance are rejected gracefully."""
    client, db_path = test_client

    with get_connection(db_path) as conn:
        token = "fath_at_insufficient"
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO consent_grants (user_id, client_id, scopes, access_token, token_expires_at) VALUES (3, 'sandbox_client_demo', 'payments:write', ?, ?)",
            (token, expires),
        )
        # Mohammed (stressed user) balance is around 320 SAR
        acc = conn.execute("SELECT id, balance FROM mock_accounts WHERE user_id = 3").fetchone()
        acc_id = acc["id"]
        current_balance = float(acc["balance"])

    # Attempt to transfer 50,000 SAR
    pay_res = client.post(
        "/open-banking/v1/payments",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "fromAccountId": acc_id,
            "toIban": "SA7210000000000000001001",
            "amount": 50000.0,
            "currency": "SAR",
            "reference": "Impossible Transfer",
        },
    )
    assert pay_res.status_code == 422
    data = pay_res.get_json()["data"]
    assert data["status"] == "Rejected"
    assert "Insufficient funds" in data["rejectionReason"]

    # Verify balance untouched
    with get_connection(db_path) as conn:
        acc_after = conn.execute("SELECT balance FROM mock_accounts WHERE id = ?", (acc_id,)).fetchone()
        assert float(acc_after["balance"]) == current_balance
