"""
Graphene Intel — Web Dashboard
Přehled odeslaných alertů s filtrováním a paginací.

Spuštění (development):
    .venv/bin/python web/app.py

Produkce (za nginx):
    gunicorn -w 2 -b 127.0.0.1:5000 "web.app:create_app()"

Konfigurace v .env:
    DASHBOARD_USER     — přihlašovací jméno (výchozí: admin)
    DASHBOARD_PASSWORD — přihlašovací heslo (POVINNÉ)
    DASHBOARD_SECRET   — Flask secret key (POVINNÉ v produkci)
    DB_PATH            — cesta k SQLite DB (výchozí: data/graphene.db)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent

_DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "graphene.db"))
_DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
_DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_SECRET_KEY = os.getenv("DASHBOARD_SECRET", "dev-insecure-key-change-in-prod")

PAGE_SIZE = 50

# Alert type labels (Czech)
ALERT_TYPE_LABELS: dict[str, str] = {
    "instant": "Okamžitý alert",
    "anomaly": "Anomálie",
    "daily_summary": "Denní souhrn",
    "weekly_report": "Týdenní report",
}

ALERT_TYPE_BADGE: dict[str, str] = {
    "instant": "danger",
    "anomaly": "warning",
    "daily_summary": "primary",
    "weekly_report": "success",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with _get_db() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_pw_hash() -> str:
    """Return hash of the configured password (evaluated lazily on first call)."""
    pw = os.getenv("DASHBOARD_PASSWORD", _DASHBOARD_PASSWORD)
    return generate_password_hash(pw) if pw else ""


_PW_HASH: str = ""  # populated in create_app()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> Flask:
    global _PW_HASH
    _PW_HASH = _get_pw_hash()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.getenv("DASHBOARD_SECRET", _SECRET_KEY)

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if not _DASHBOARD_PASSWORD:
                error = "DASHBOARD_PASSWORD není nastaveno v .env"
            elif username == _DASHBOARD_USER and check_password_hash(_PW_HASH, password):
                session["logged_in"] = True
                session.permanent = True
                return redirect(request.args.get("next") or url_for("index"))
            else:
                error = "Nesprávné přihlašovací údaje"
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        # Summary stats
        stats_rows = _query("""
            SELECT alert_type, COUNT(*) as cnt
            FROM alerts_sent
            GROUP BY alert_type
            ORDER BY cnt DESC
        """)
        stats = {r["alert_type"]: r["cnt"] for r in stats_rows}
        total = sum(stats.values())

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_count = (_query_one(
            "SELECT COUNT(*) as cnt FROM alerts_sent WHERE sent_at >= ?",
            (today_str,)
        ) or {}).get("cnt", 0)

        # Filters
        filter_type = request.args.get("type", "")
        filter_ticker = request.args.get("ticker", "").strip().upper()
        page = max(1, int(request.args.get("page", 1)))
        offset = (page - 1) * PAGE_SIZE

        where_clauses = []
        params: list[Any] = []
        if filter_type:
            where_clauses.append("a.alert_type = ?")
            params.append(filter_type)
        if filter_ticker:
            where_clauses.append("(h.tickers LIKE ? OR h.affected_tickers LIKE ?)")
            params.extend([f"%{filter_ticker}%", f"%{filter_ticker}%"])

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_row = _query_one(f"""
            SELECT COUNT(*) as cnt
            FROM alerts_sent a
            LEFT JOIN headlines h ON a.headline_id = h.id
            {where_sql}
        """, tuple(params))
        total_rows = (count_row or {}).get("cnt", 0)
        total_pages = max(1, -(-total_rows // PAGE_SIZE))  # ceil division

        rows = _query(f"""
            SELECT
                a.id,
                a.sent_at,
                a.alert_type,
                a.telegram_message_id,
                h.id        AS headline_id,
                h.title,
                h.tickers,
                h.score,
                h.sentiment,
                h.source,
                h.url,
                h.impact_summary,
                h.is_red_flag,
                h.is_pump_suspect,
                h.published_at
            FROM alerts_sent a
            LEFT JOIN headlines h ON a.headline_id = h.id
            {where_sql}
            ORDER BY a.sent_at DESC
            LIMIT ? OFFSET ?
        """, tuple(params) + (PAGE_SIZE, offset))

        return render_template(
            "dashboard.html",
            rows=rows,
            stats=stats,
            total=total,
            today_count=today_count,
            filter_type=filter_type,
            filter_ticker=filter_ticker,
            page=page,
            total_pages=total_pages,
            total_rows=total_rows,
            alert_types=list(ALERT_TYPE_LABELS.keys()),
            ALERT_TYPE_LABELS=ALERT_TYPE_LABELS,
            ALERT_TYPE_BADGE=ALERT_TYPE_BADGE,
        )

    @app.route("/alert/<int:alert_id>")
    @login_required
    def alert_detail(alert_id: int):
        row = _query_one("""
            SELECT
                a.*,
                h.title, h.tickers, h.score, h.sentiment, h.source, h.url,
                h.impact_summary, h.raw_content, h.is_red_flag, h.is_pump_suspect,
                h.published_at, h.evaluated_at, h.category, h.affected_tickers
            FROM alerts_sent a
            LEFT JOIN headlines h ON a.headline_id = h.id
            WHERE a.id = ?
        """, (alert_id,))
        if row is None:
            return "Alert nenalezen", 404

        return render_template(
            "alert_detail.html",
            row=row,
            ALERT_TYPE_LABELS=ALERT_TYPE_LABELS,
            ALERT_TYPE_BADGE=ALERT_TYPE_BADGE,
        )

    return app


# ── Dev server ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")

    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=False)
