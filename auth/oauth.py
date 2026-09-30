"""Pure-Python OAuth 2.0 with PKCE Engine & Consent Management for Fath (فتح).

Implements RFC 7636 (PKCE) and RFC 6749 (Authorization Code Grant) from scratch
without external auth dependencies (no authlib, no PyJWT, no flask-oauthlib).
Uses standard library secrets.token_urlsafe() and hashlib.sha256() exclusively.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable

from flask import Blueprint, jsonify, render_template, request, session

from database import get_connection, log_event

# Configuration defaults
DEFAULT_TOKEN_TTL_SECONDS = int(os.getenv("FATH_TOKEN_TTL", "3600"))
AUTH_CODE_LIFETIME_SECONDS = 600  # 10 minutes

# Official SAMA Open Banking Standard Scopes
VALID_SCOPES = {
    "accounts:read": {
        "ar": "الاطلاع على أرقام الحسابات، تفاصيل الآيبان، والبيانات التعريفية",
        "en": "View bank account numbers, IBAN details, and identifying metadata",
    },
    "balances:read": {
        "ar": "الاطلاع على الأرصدة المالية الحية في الوقت الفعلي",
        "en": "View live account balances in real time",
    },
    "transactions:read": {
        "ar": "الاطلاع على سجل المعاملات المالية والمصروفات حتى 12 شهراً",
        "en": "View transaction ledger history and expenses for up to 12 months",
    },
    "payments:write": {
        "ar": "إنشاء وتفويض أوامر الدفع والتحويلات المالية المباشرة",
        "en": "Initiate and authorize direct payment and transfer orders",
    },
}

oauth_bp = Blueprint("oauth", __name__)


# --- Core Cryptographic & PKCE Functions ---


def generate_code_verifier(length: int = 64) -> str:
    """Generate a high-entropy cryptographic PKCE code_verifier string."""
    length = max(43, min(128, length))
    return secrets.token_urlsafe(length)[:length]


def generate_code_challenge_s256(code_verifier: str) -> str:
    """Compute BASE64URL(SHA256(ASCII(code_verifier))) without padding per RFC 7636."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_code_challenge(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """Verify code_verifier against stored code_challenge using constant-time comparison."""
    if not code_verifier or not code_challenge:
        return False
    if method == "S256":
        calculated = generate_code_challenge_s256(code_verifier)
        return secrets.compare_digest(calculated, code_challenge)
    elif method == "plain":
        return secrets.compare_digest(code_verifier, code_challenge)
    return False


def get_token_ttl() -> int:
    """Read dynamic or configured token TTL in seconds."""
    return int(os.getenv("FATH_TOKEN_TTL", str(DEFAULT_TOKEN_TTL_SECONDS)))


# --- Business Operations ---


def issue_authorization_code(
    user_id: int,
    client_id: str,
    scopes: list[str],
    code_challenge: str | None,
    code_challenge_method: str = "S256",
    db_path: Path | str | None = None,
) -> str:
    """Issue a single-use authorization code recorded in consent_grants."""
    auth_code = secrets.token_urlsafe(32)
    scopes_str = " ".join(scopes)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO consent_grants (
                user_id, client_id, scopes, code_challenge,
                code_challenge_method, auth_code, auth_code_used
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                user_id,
                client_id,
                scopes_str,
                code_challenge,
                code_challenge_method,
                auth_code,
            ),
        )
        log_event(
            conn,
            user_id,
            "AUTH_CODE_ISSUED",
            {
                "client_id": client_id,
                "scopes": scopes,
                "has_pkce": bool(code_challenge),
            },
        )

    return auth_code


def exchange_code_for_token(
    client_id: str,
    code: str,
    code_verifier: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Exchange authorization code and PKCE code_verifier for an access token."""
    with get_connection(db_path) as conn:
        grant = conn.execute(
            "SELECT * FROM consent_grants WHERE auth_code = ? AND client_id = ?",
            (code, client_id),
        ).fetchone()

        if not grant:
            log_event(conn, None, "AUTH_TOKEN_FAILED", {"reason": "invalid_code", "client_id": client_id})
            return False, {"error": "invalid_grant", "error_description": "Authorization code not found"}, 400

        # Check if already used (strictly single-use; revoke all if re-attempted per RFC 6749)
        if grant["auth_code_used"] == 1:
            conn.execute(
                "UPDATE consent_grants SET revoked_at = CURRENT_TIMESTAMP WHERE client_id = ? AND user_id = ?",
                (client_id, grant["user_id"]),
            )
            log_event(conn, grant["user_id"], "AUTH_CODE_REUSED_REVOCATION", {"client_id": client_id})
            return False, {
                "error": "invalid_grant",
                "error_description": "Authorization code was previously consumed. Grant revoked.",
            }, 400

        # Check expiration of authorization code
        created_time = datetime.fromisoformat(grant["created_at"])
        if datetime.now(timezone.utc).timestamp() - created_time.replace(tzinfo=timezone.utc).timestamp() > AUTH_CODE_LIFETIME_SECONDS:
            return False, {"error": "invalid_grant", "error_description": "Authorization code has expired"}, 400

        # PKCE verification
        stored_challenge = grant["code_challenge"]
        if stored_challenge:
            if not code_verifier:
                return False, {"error": "invalid_request", "error_description": "code_verifier is required for PKCE"}, 400
            method = grant["code_challenge_method"] or "S256"
            if not verify_code_challenge(code_verifier, stored_challenge, method):
                log_event(conn, grant["user_id"], "AUTH_PKCE_MISMATCH", {"client_id": client_id})
                return False, {"error": "invalid_grant", "error_description": "PKCE code_verifier validation failed"}, 400

        # Issue access token
        ttl = get_token_ttl()
        access_token = f"fath_at_{secrets.token_urlsafe(32)}"
        now_utc = datetime.now(timezone.utc)
        expires_at = (now_utc + timedelta(seconds=ttl)).isoformat()

        conn.execute(
            """
            UPDATE consent_grants
            SET auth_code_used = 1,
                access_token = ?,
                token_expires_at = ?
            WHERE id = ?
            """,
            (access_token, expires_at, grant["id"]),
        )

        log_event(
            conn,
            grant["user_id"],
            "ACCESS_TOKEN_ISSUED",
            {"client_id": client_id, "ttl": ttl, "scopes": grant["scopes"]},
        )

        return True, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": grant["scopes"],
            "created_at": now_utc.isoformat(),
        }, 200


def revoke_token(
    token: str,
    client_id: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Revoke an active access token or consent grant."""
    with get_connection(db_path) as conn:
        query = "SELECT * FROM consent_grants WHERE access_token = ?"
        params: list[Any] = [token]
        if client_id:
            query += " AND client_id = ?"
            params.append(client_id)

        grant = conn.execute(query, params).fetchone()
        if grant:
            conn.execute(
                "UPDATE consent_grants SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (grant["id"],),
            )
            log_event(
                conn,
                grant["user_id"],
                "ACCESS_TOKEN_REVOKED",
                {"client_id": grant["client_id"], "token_hint": token[:10] + "..."},
            )
            return True
        return False


def introspect_token(token: str, db_path: Path | str | None = None) -> dict[str, Any]:
    """Inspect token validity per RFC 7662."""
    with get_connection(db_path) as conn:
        grant = conn.execute(
            "SELECT * FROM consent_grants WHERE access_token = ?", (token,)
        ).fetchone()

        if not grant or grant["revoked_at"] is not None:
            return {"active": False}

        expires_at_str = grant["token_expires_at"]
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                return {"active": False, "reason": "expired"}

            return {
                "active": True,
                "scope": grant["scopes"],
                "client_id": grant["client_id"],
                "user_id": grant["user_id"],
                "token_type": "Bearer",
                "exp": int(expires_at.timestamp()),
            }

        return {"active": False}


def validate_bearer_token(
    token_str: str,
    required_scope: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Validate Bearer token from HTTP headers, verify expiry, revocation, and required scope."""
    if not token_str:
        return False, None, "Missing Authorization Bearer token"

    cleaned_token = token_str.replace("Bearer ", "").strip()

    with get_connection(db_path) as conn:
        grant = conn.execute(
            "SELECT * FROM consent_grants WHERE access_token = ?", (cleaned_token,)
        ).fetchone()

        if not grant:
            return False, None, "Invalid access token"

        if grant["revoked_at"] is not None:
            return False, None, "Access token has been revoked"

        expires_at_str = grant["token_expires_at"]
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                return False, None, "Access token has expired"

        # Scope validation
        granted_scopes = set(grant["scopes"].split())
        if required_scope and required_scope not in granted_scopes:
            return False, None, f"Insufficient scope: requires '{required_scope}'"

        return True, dict(grant), None


def require_oauth_token(required_scope: str | None = None) -> Callable:
    """Decorator for Flask API routes to enforce Bearer token and scope validation."""

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return (
                    jsonify(
                        {
                            "error": "unauthorized",
                            "error_description": "Authorization Bearer token required",
                        }
                    ),
                    401,
                )

            valid, grant, err_msg = validate_bearer_token(auth_header, required_scope)
            if not valid:
                status_code = 403 if "Insufficient scope" in (err_msg or "") else 401
                return (
                    jsonify({"error": "forbidden" if status_code == 403 else "unauthorized", "error_description": err_msg}),
                    status_code,
                )

            # Pass validated grant to route
            kwargs["oauth_grant"] = grant
            return fn(*args, **kwargs)

        return wrapper

    return decorator


# --- Blueprint HTTP Endpoints ---


@oauth_bp.route("/authorize", methods=["GET", "POST"])
def authorize():
    """OAuth 2.0 Authorization Endpoint supporting PKCE and Consent Approval."""
    data = request.get_json(silent=True) or request.form or request.args

    client_id = data.get("client_id")
    redirect_uri = data.get("redirect_uri")
    response_type = data.get("response_type", "code")
    scope_param = data.get("scope", "accounts:read balances:read transactions:read")
    code_challenge = data.get("code_challenge")
    code_challenge_method = data.get("code_challenge_method", "S256")
    state = data.get("state", "")
    user_id = data.get("user_id")

    if not client_id:
        return jsonify({"error": "invalid_request", "error_description": "client_id is required"}), 400

    if response_type != "code":
        return jsonify({"error": "unsupported_response_type", "error_description": "Only 'code' is supported"}), 400

    # Validate client in database
    with get_connection() as conn:
        client = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if not client:
            return jsonify({"error": "unauthorized_client", "error_description": "Unknown client_id"}), 400

        # Parse and sanitize requested scopes
        requested_scopes = [s.strip() for s in scope_param.split() if s.strip() in VALID_SCOPES]
        if not requested_scopes:
            return jsonify({"error": "invalid_scope", "error_description": "No valid scopes requested"}), 400

        # If user_id is provided directly (e.g. testing or consented form submission)
        if request.method == "POST" and user_id:
            try:
                user_id_int = int(user_id)
            except ValueError:
                return jsonify({"error": "invalid_request", "error_description": "Invalid user_id"}), 400

            user = conn.execute("SELECT id FROM mock_users WHERE id = ?", (user_id_int,)).fetchone()
            if not user:
                return jsonify({"error": "invalid_request", "error_description": "Mock user not found"}), 404

            # Approved scopes from form or defaults
            approved_scopes = data.getlist("scopes") if hasattr(data, "getlist") and data.getlist("scopes") else requested_scopes

            auth_code = issue_authorization_code(
                user_id=user_id_int,
                client_id=client_id,
                scopes=approved_scopes,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
            )

            # Build redirect response
            target_redirect = redirect_uri or client["redirect_uri"]
            delimiter = "&" if "?" in target_redirect else "?"
            redirect_url = f"{target_redirect}{delimiter}code={auth_code}"
            if state:
                redirect_url += f"&state={state}"

            if request.is_json:
                return jsonify({
                    "code": auth_code,
                    "state": state,
                    "redirect_uri": redirect_url,
                    "scopes": approved_scopes,
                }), 200

            from flask import redirect
            return redirect(redirect_url)

        # For GET or initial display, return JSON metadata or template parameters
        mock_users = conn.execute("SELECT id, username, full_name_ar, full_name_en, archetype FROM mock_users").fetchall()
        return jsonify({
            "client_name": client["client_name"],
            "client_id": client_id,
            "redirect_uri": redirect_uri or client["redirect_uri"],
            "requested_scopes": [
                {
                    "scope": s,
                    "desc_ar": VALID_SCOPES[s]["ar"],
                    "desc_en": VALID_SCOPES[s]["en"],
                }
                for s in requested_scopes
            ],
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "state": state,
            "available_users": [dict(u) for u in mock_users],
        }), 200


@oauth_bp.route("/token", methods=["POST"])
def token():
    """OAuth 2.0 Token Endpoint exchanging auth_code + code_verifier for access token."""
    data = request.get_json(silent=True) or request.form

    grant_type = data.get("grant_type")
    client_id = data.get("client_id")
    code = data.get("code")
    code_verifier = data.get("code_verifier")

    if grant_type != "authorization_code":
        return jsonify({
            "error": "unsupported_grant_type",
            "error_description": "Only 'authorization_code' grant_type is supported",
        }), 400

    if not client_id or not code:
        return jsonify({
            "error": "invalid_request",
            "error_description": "client_id and code are required",
        }), 400

    success, payload, status_code = exchange_code_for_token(
        client_id=client_id,
        code=code,
        code_verifier=code_verifier,
    )
    return jsonify(payload), status_code


@oauth_bp.route("/revoke", methods=["POST"])
def revoke():
    """OAuth 2.0 Token Revocation Endpoint."""
    data = request.get_json(silent=True) or request.form
    token_str = data.get("token")
    client_id = data.get("client_id")

    if not token_str:
        return jsonify({"error": "invalid_request", "error_description": "token parameter required"}), 400

    revoked = revoke_token(token=token_str, client_id=client_id)
    return jsonify({"status": "revoked" if revoked else "not_found"}), 200


@oauth_bp.route("/introspect", methods=["GET", "POST"])
def introspect():
    """OAuth 2.0 Token Introspection Endpoint (RFC 7662)."""
    token_str = ""
    auth_header = request.headers.get("Authorization")
    if auth_header and "Bearer " in auth_header:
        token_str = auth_header.replace("Bearer ", "").strip()
    else:
        data = request.get_json(silent=True) or request.form or request.args
        token_str = data.get("token", "")

    if not token_str:
        return jsonify({"active": False, "error": "token_missing"}), 200

    result = introspect_token(token_str)
    return jsonify(result), 200
