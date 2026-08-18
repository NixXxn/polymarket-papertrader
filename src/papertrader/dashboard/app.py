from __future__ import annotations

import os
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

from papertrader.config import ROOT
from papertrader.dashboard.data import fetch_dashboard, reset_strategy_budgets
from papertrader.mode import load_dotenv_file

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
PORT = int(os.getenv("PORT", "8787"))


def _check_auth(username: str, password: str) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    return username == DASHBOARD_USER and password == DASHBOARD_PASSWORD


def _authenticate() -> Response:
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Papertrader Dashboard"'},
    )


def requires_auth(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return fn(*args, **kwargs)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return _authenticate()
        return fn(*args, **kwargs)

    return decorated


@app.route("/")
@requires_auth
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
@requires_auth
def dashboard_api():
    try:
        mode = request.args.get("mode")
        data_dir = request.args.get("data_dir")
        from pathlib import Path as P

        payload = fetch_dashboard(
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
        )
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset-balances", methods=["POST"])
@requires_auth
def reset_balances_api():
    try:
        body = request.get_json(silent=True) or {}
        balance = body.get("balance")
        if balance is None:
            return jsonify({"ok": False, "error": "balance is required"}), 400
        mode = request.args.get("mode") or body.get("mode")
        data_dir = request.args.get("data_dir") or body.get("data_dir")
        from pathlib import Path as P

        payload = reset_strategy_budgets(
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
            balance=float(balance),
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": os.getenv("SERVICE", "both")})


def run_dashboard(host: str = "127.0.0.1", port: int | None = None, debug: bool = False) -> None:
    load_dotenv_file(ROOT / ".env")
    app.run(host=host, port=port or PORT, debug=debug, threaded=True)
