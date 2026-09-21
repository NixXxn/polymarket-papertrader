from __future__ import annotations

import os
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

from papertrader.config import ROOT
from papertrader.dashboard.data import (
    fetch_dashboard,
    reset_all_statistics,
    reset_strategy_budgets,
    set_strategy_budget,
    list_dashboard_copy_wallets,
    add_dashboard_copy_wallet,
    remove_dashboard_copy_wallet,
)
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
    # UI moved to the Next/OpenUI app on :3000. JSON APIs stay on this Flask port.
    ui = os.getenv("DASHBOARD_UI_URL", "http://127.0.0.1:3000")
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" in accept or request.args.get("legacy") == "1":
        if request.args.get("legacy") == "1":
            return render_template("index.html")
        return (
            "<!doctype html><html><head><meta charset=utf-8>"
            f"<meta http-equiv=refresh content='0;url={ui}'>"
            f"<title>Papertrader Dashboard</title></head><body style='"
            "font-family:system-ui,sans-serif;background:#0b1020;color:#eef3ff;"
            "padding:40px'>"
            "<h1>Dashboard moved</h1>"
            f"<p>Open the OpenUI dashboard at <a href='{ui}' style='color:#7dd3fc'>{ui}</a>.</p>"
            "<p>JSON APIs remain on this port (<code>/api/dashboard</code>).</p>"
            "<p><a href='/?legacy=1' style='color:#94a3b8'>Legacy HTML UI</a></p>"
            "</body></html>"
        ), 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify(
        {
            "ok": True,
            "service": "papertrader-dashboard-api",
            "ui": ui,
            "message": "Use the Next/OpenUI app for the UI; JSON APIs are on this host.",
            "legacy_html": "/?legacy=1",
        }
    )


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


@app.route("/api/reset-statistics", methods=["POST"])
@requires_auth
def reset_statistics_api():
    try:
        body = request.get_json(silent=True) or {}
        mode = request.args.get("mode") or body.get("mode")
        data_dir = request.args.get("data_dir") or body.get("data_dir")
        balance = body.get("balance")
        from pathlib import Path as P

        payload = reset_all_statistics(
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
            balance=float(balance) if balance is not None else None,
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/set-strategy-budget", methods=["POST"])
@requires_auth
def set_strategy_budget_api():
    try:
        body = request.get_json(silent=True) or {}
        strategy = str(body.get("strategy") or "").strip()
        balance = body.get("balance")
        if not strategy:
            return jsonify({"ok": False, "error": "strategy is required"}), 400
        if balance is None:
            return jsonify({"ok": False, "error": "balance is required"}), 400
        mode = request.args.get("mode") or body.get("mode")
        data_dir = request.args.get("data_dir") or body.get("data_dir")
        from pathlib import Path as P

        payload = set_strategy_budget(
            strategy=strategy,
            balance=float(balance),
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
        )
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/copy/wallets", methods=["GET"])
@requires_auth
def copy_wallets_list_api():
    try:
        mode = request.args.get("mode")
        data_dir = request.args.get("data_dir")
        from pathlib import Path as P

        payload = list_dashboard_copy_wallets(
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
        )
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/copy/wallets", methods=["POST"])
@requires_auth
def copy_wallets_add_api():
    try:
        body = request.get_json(silent=True) or {}
        address = str(body.get("address") or body.get("wallet") or "").strip()
        label = str(body.get("label") or "").strip()
        if not address:
            return jsonify({"ok": False, "error": "address is required"}), 400
        mode = request.args.get("mode") or body.get("mode")
        data_dir = request.args.get("data_dir") or body.get("data_dir")
        from pathlib import Path as P

        payload = add_dashboard_copy_wallet(
            address=address,
            label=label,
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
        )
        return jsonify(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/copy/wallets", methods=["DELETE"])
@requires_auth
def copy_wallets_remove_api():
    try:
        body = request.get_json(silent=True) or {}
        address = str(
            body.get("address") or body.get("wallet") or request.args.get("address") or ""
        ).strip()
        if not address:
            return jsonify({"ok": False, "error": "address is required"}), 400
        mode = request.args.get("mode") or body.get("mode")
        data_dir = request.args.get("data_dir") or body.get("data_dir")
        from pathlib import Path as P

        payload = remove_dashboard_copy_wallet(
            address=address,
            data_dir=P(data_dir) if data_dir else None,
            mode=mode,
        )
        return jsonify(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": os.getenv("SERVICE", "both")})


def run_dashboard(host: str = "127.0.0.1", port: int | None = None, debug: bool = False) -> None:
    load_dotenv_file(ROOT / ".env")
    app.run(host=host, port=port or PORT, debug=debug, threaded=True)
