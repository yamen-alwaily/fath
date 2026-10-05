"""Main Application for Fath (فتح) — Saudi Open Banking Sandbox for Developers.

Tagline: Saudi Open Banking Sandbox for Developers | بيئة اختبار البنك المفتوح للمطورين
SAMA Standard: Open Banking Technical Standards v1.0
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from api.ais import ais_bp
from api.pis import pis_bp
from auth.oauth import issue_authorization_code, oauth_bp, revoke_token
from data.mock_bank import SAMA_SANDBOX_DISCLAIMER, seed_mock_bank_data
from database import get_connection, init_db, log_event
from ws.stream import broadcast_consent_revoked, init_socketio

# Load .env configuration
load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("FATH_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# Initialize database schema and default seed
init_db()

# Register Blueprints
app.register_blueprint(oauth_bp, url_prefix="/oauth")
app.register_blueprint(ais_bp)
app.register_blueprint(pis_bp)

# Attach WebSocket Streaming Engine
socketio = init_socketio(app)


def _current_language() -> str:
    """Detect selected or requested language ('ar' or 'en')."""
    lang_param = request.args.get("lang")
    if lang_param in ("ar", "en"):
        session["lang"] = lang_param
        return lang_param
    return session.get("lang", "ar")


# --- UI and Portal Routes ---


@app.route("/")
def index():
    """Bilingual landing page."""
    lang = _current_language()
    template_name = "landing.html" if lang == "ar" else "landing_en.html"
    return render_template(template_name)


@app.route("/portal")
def portal():
    """Developer sandbox management portal."""
    lang = _current_language()
    template_name = "portal.html" if lang == "ar" else "portal_en.html"

    with get_connection() as conn:
        total_users = conn.execute("SELECT COUNT(*) as c FROM mock_users").fetchone()["c"]
        total_accounts = conn.execute("SELECT COUNT(*) as c FROM mock_accounts").fetchone()["c"]
        total_txns = conn.execute("SELECT COUNT(*) as c FROM mock_transactions").fetchone()["c"]

        consents = conn.execute(
            """
            SELECT g.*, u.full_name_ar, u.full_name_en, u.username
            FROM consent_grants g
            JOIN mock_users u ON g.user_id = u.id
            ORDER BY g.id DESC LIMIT 20
            """
        ).fetchall()

        audit_logs = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 25"
        ).fetchall()

    return render_template(
        template_name,
        total_users=total_users,
        total_accounts=total_accounts,
        total_transactions=total_txns,
        consents=[dict(c) for c in consents],
        audit_logs=[dict(a) for a in audit_logs],
    )


@app.route("/explorer")
def explorer():
    """Interactive API Explorer."""
    with get_connection() as conn:
        users = conn.execute(
            "SELECT id, full_name_en, full_name_ar, archetype FROM mock_users ORDER BY id ASC"
        ).fetchall()

    return render_template("explorer.html", users=[dict(u) for u in users])


@app.route("/portal/seed", methods=["POST"])
def portal_reseed():
    """Reset and seed fresh mock bank data."""
    seed_mock_bank_data(force_reset=True, months_history=8)
    lang = session.get("lang", "ar")
    return redirect(url_for("portal", lang=lang))


@app.route("/portal/revoke", methods=["POST"])
def portal_revoke_token():
    """Revoke consent directly from Developer Portal."""
    token = request.form.get("token")
    if token:
        with get_connection() as conn:
            grant = conn.execute("SELECT user_id FROM consent_grants WHERE access_token = ?", (token,)).fetchone()
            if grant:
                revoke_token(token)
                broadcast_consent_revoked(grant["user_id"], "Revoked via Developer Portal")

    lang = session.get("lang", "ar")
    return redirect(url_for("portal", lang=lang))


@app.route("/portal/mint-token", methods=["POST"])
def portal_mint_token():
    """Convenience endpoint for API Explorer to instantly mint a valid Bearer token."""
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", 1)

    with get_connection() as conn:
        user = conn.execute("SELECT * FROM mock_users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        token_str = f"fath_at_{secrets.token_urlsafe(32)}"
        ttl = int(os.getenv("FATH_TOKEN_TTL", "3600"))
        now = datetime.now(timezone.utc)
        expires_at = (now + datetime.resolution * ttl).isoformat() if hasattr(datetime, 'resolution') else (now).isoformat()
        from datetime import timedelta
        expires_at = (now + timedelta(seconds=ttl)).isoformat()

        all_scopes = "accounts:read balances:read transactions:read payments:write"
        conn.execute(
            """
            INSERT INTO consent_grants (
                user_id, client_id, scopes, auth_code, auth_code_used,
                access_token, token_expires_at
            ) VALUES (?, 'sandbox_client_demo', ?, ?, 1, ?, ?)
            """,
            (user_id, all_scopes, secrets.token_urlsafe(16), token_str, expires_at),
        )
        log_event(
            conn,
            user_id,
            "SANDBOX_TOKEN_MINTED",
            {"scopes": all_scopes, "client_id": "sandbox_client_demo"},
        )

    return jsonify({
        "access_token": token_str,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": all_scopes,
        "user_id": user_id,
        "customer": user["full_name_en"],
    }), 200


@app.route("/health")
def health():
    """Container & Service Health Check Endpoint."""
    with get_connection() as conn:
        conn.execute("SELECT 1").fetchone()

    return jsonify({
        "status": "healthy",
        "service": "fath",
        "name": "Fath | فتح",
        "tagline": "Saudi Open Banking Sandbox for Developers",
        "framework": "SAMA Open Banking Framework v1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "disclaimer": SAMA_SANDBOX_DISCLAIMER,
    }), 200


@app.route("/docs")
def swagger_docs():
    """Interactive Swagger UI documentation page for OpenAPI 3.0 specification."""
    lang = _current_language()
    template_name = "docs.html" if lang == "ar" else "docs_en.html"
    return render_template(template_name)


@app.route("/openapi.yaml")
def openapi_spec():
    """Serve the OpenAPI 3.0 specification file."""
    return send_from_directory(
        directory=str(Path(__file__).resolve().parent),
        path="openapi.yaml",
        mimetype="text/yaml",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = os.getenv("DEBUG", "false").lower() == "true"
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
