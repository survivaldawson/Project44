
import os
import json
import hmac
import hashlib
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, render_template


# ==========================================
# 1. PROJECT CONFIGURATION
# ==========================================

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID", "")
ADMIN_USER_IDS = {
    item.strip()
    for item in os.getenv("ADMIN_USER_IDS", "").split(",")
    if item.strip()
}

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

DATABASE_PATH = os.getenv("DATABASE_PATH", "project44.db")

POINTS_PER_REFERRAL = 5
MIN_WITHDRAWAL = 500
MAX_WITHDRAWAL = 2000
USD_PER_POINT = 0.0026

ASSET_RATES = {
    "ATF": 0,
    "GRAM": 0,
    "USDT": 1
}

app.config["JSON_SORT_KEYS"] = False


# ==========================================
# 2. DATABASE
# ==========================================

def get_db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():

    with get_db() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                points INTEGER DEFAULT 0,
                referred_by INTEGER,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                asset TEXT NOT NULL,
                points INTEGER NOT NULL,
                wallet_address TEXT,
                network TEXT,
                gift_telegram TEXT,
                status TEXT DEFAULT 'pending',
                estimated_usd REAL,
                created_at TEXT,
                updated_at TEXT,
                tx_reference TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER UNIQUE NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS ad_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS reward_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                reason TEXT NOT NULL,
                reference TEXT UNIQUE NOT NULL,
                created_at TEXT
            )
        """)


init_db()


# ==========================================
# 3. GENERAL HELPERS
# ==========================================

def now():
    return datetime.now(timezone.utc).isoformat()


def send_telegram_message(chat_id, message):

    if not BOT_TOKEN or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=15
        )

        return response.ok

    except requests.RequestException as error:
        app.logger.error("Telegram message error: %s", error)
        return False


def notify_admin(message):
    return send_telegram_message(ADMIN_GROUP_ID, message)


def notify_user(user_id, message):
    return send_telegram_message(user_id, message)


# ==========================================
# 4. TELEGRAM MINI APP AUTHENTICATION
# ==========================================

def verify_telegram_init_data(init_data):

    if not BOT_TOKEN or not init_data:
        return None

    try:

        parsed = dict(
            urllib.parse.parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        auth_date = int(parsed.get("auth_date", 0))

        current_time = datetime.now(timezone.utc).timestamp()

        # Reject expired authentication data.
        if current_time - auth_date > 3600:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user_data = json.loads(parsed.get("user", "{}"))

        if not user_data.get("id"):
            return None

        return user_data

    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def require_user():

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    user = verify_telegram_init_data(init_data)

    if not user:
        return None

    return user


def require_admin():

    init_data = request.headers.get(
        "X-Telegram-Init-Data",
        ""
    )

    user = verify_telegram_init_data(init_data)

    if not user:
        return None

    if str(user["id"]) not in ADMIN_USER_IDS:
        return None

    return user


# ==========================================
# 5. USER MANAGEMENT
# ==========================================

def create_user(user_data, referral_id=None):

    user_id = int(user_data["id"])

    with get_db() as db:

        existing = db.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if existing:
            db.execute("""
                UPDATE users
                SET username = ?, first_name = ?
                WHERE user_id = ?
            """, (
                user_data.get("username"),
                user_data.get("first_name"),
                user_id
            ))

            return

        valid_referrer = None

        if referral_id:

            try:
                referral_id = int(referral_id)

                if referral_id != user_id:

                    referrer = db.execute(
                        "SELECT user_id FROM users WHERE user_id = ?",
                        (referral_id,)
                    ).fetchone()

                    if referrer:
                        valid_referrer = referral_id

            except (ValueError, TypeError):
                pass

        db.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                points,
                referred_by,
                created_at
            )
            VALUES (?, ?, ?, 0, ?, ?)
        """, (
            user_id,
            user_data.get("username"),
            user_data.get("first_name"),
            valid_referrer,
            now()
        ))

        if valid_referrer:

            db.execute("""
                INSERT INTO referrals (
                    referrer_id,
                    referred_id,
                    status,
                    created_at
                )
                VALUES (?, ?, 'pending', ?)
            """, (
                valid_referrer,
                user_id,
                now()
            ))


def get_available_points(db, user_id):

    user = db.execute(
        "SELECT points FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not user:
        return 0

    reserved = db.execute("""
        SELECT COALESCE(SUM(points), 0) AS total
        FROM withdrawals
        WHERE user_id = ?
        AND status IN ('pending', 'approved')
    """, (user_id,)).fetchone()["total"]

    return int(user["points"]) - int(reserved)


# ==========================================
# 6. HOME PAGE
# ==========================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "project": "Project 44"
    })


# ==========================================
# 7. DASHBOARD API
# ==========================================

@app.route("/api/status", methods=["GET"])
def api_status():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    create_user(user)

    user_id = int(user["id"])

    with get_db() as db:

        available = get_available_points(db, user_id)

        completed_ads = db.execute("""
            SELECT COUNT(*) AS total
            FROM reward_ledger
            WHERE user_id = ?
            AND reason = 'verified_ad'
        """, (user_id,)).fetchone()["total"]

    return jsonify({
        "points": available,
        "completed_ads": completed_ads,
        "rates": {
            "stars_per_500_points": 100,
            "usd_per_point": USD_PER_POINT,
            "ATF": ASSET_RATES["ATF"],
            "GRAM": ASSET_RATES["GRAM"],
            "USDT": ASSET_RATES["USDT"]
        }
    })


# ==========================================
# 8. AD SESSION API
# ==========================================

@app.route("/api/ad/start", methods=["POST"])
def start_ad():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    user_id = int(user["id"])

    create_user(user)

    session_id = secrets.token_urlsafe(24)

    with get_db() as db:

        db.execute("""
            INSERT INTO ad_sessions (
                session_id,
                user_id,
                status,
                created_at
            )
            VALUES (?, ?, 'pending', ?)
        """, (
            session_id,
            user_id,
            now()
        ))

    return jsonify({
        "session_id": session_id,
        "status": "pending"
    })


@app.route("/api/ad/complete", methods=["POST"])
def complete_ad():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    data = request.get_json(silent=True) or {}

    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"message": "Missing ad session."}), 400

    user_id = int(user["id"])

    with get_db() as db:

        session = db.execute("""
            SELECT * FROM ad_sessions
            WHERE session_id = ?
            AND user_id = ?
        """, (session_id, user_id)).fetchone()

        if not session:
            return jsonify({"message": "Invalid ad session."}), 404

        # Browser completion is NOT proof of a valid reward.
        # Never credit points from this request alone.

        db.execute("""
            UPDATE ad_sessions
            SET status = 'awaiting_verification'
            WHERE session_id = ?
        """, (session_id,))

    return jsonify({
        "credited": False,
        "status": "awaiting_verification",
        "message": "Ad completion received. Reward is awaiting server verification."
    })


# ==========================================
# 9. REFERRAL API
# ==========================================

@app.route("/api/referrals", methods=["GET"])
def api_referrals():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    user_id = int(user["id"])

    create_user(user)

    with get_db() as db:

        verified = db.execute("""
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id = ?
            AND status = 'verified'
        """, (user_id,)).fetchone()["total"]

    if not BOT_USERNAME:
        referral_link = ""

    else:
        referral_link = (
            f"https://t.me/{BOT_USERNAME}"
            f"?start=ref_{user_id}"
        )

    return jsonify({
        "referral_link": referral_link,
        "verified_referrals": verified,
        "points_per_referral": POINTS_PER_REFERRAL
    })


# ==========================================
# 10. WITHDRAWAL API
# ==========================================

@app.route("/api/withdrawals", methods=["POST"])
def create_withdrawal():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    data = request.get_json(silent=True) or {}

    user_id = int(user["id"])

    asset = str(data.get("asset", "")).upper()

    try:
        points = int(data.get("points", 0))
    except (ValueError, TypeError):
        return jsonify({"message": "Invalid points amount."}), 400

    if asset not in ("ATF", "GRAM", "USDT", "GIFT"):
        return jsonify({"message": "Invalid reward asset."}), 400

    if not MIN_WITHDRAWAL <= points <= MAX_WITHDRAWAL:
        return jsonify({
            "message": "Withdrawal must be between 500 and 2,000 points."
        }), 400

    wallet = data.get("wallet_address")
    network = data.get("network")
    gift_recipient = data.get("gift_telegram")

    if asset == "GIFT":

        if not isinstance(gift_recipient, str) or not gift_recipient.strip():
            return jsonify({
                "message": "Telegram gift recipient is required."
            }), 400

        wallet = None
        network = None

    else:

        if not isinstance(wallet, str) or not wallet.strip():
            return jsonify({
                "message": "TON wallet address is required."
            }), 400

        if network != "TON":
            return jsonify({
                "message": "Only the TON network is supported."
            }), 400

        gift_recipient = None

    request_id = "P44-" + secrets.token_hex(5).upper()

    estimated_usd = round(points * USD_PER_POINT, 2)

    with get_db() as db:

        available = get_available_points(db, user_id)

        if available < points:
            return jsonify({
                "message": "Insufficient available points."
            }), 400

        db.execute("""
            INSERT INTO withdrawals (
                request_id,
                user_id,
                asset,
                points,
                wallet_address,
                network,
                gift_telegram,
                status,
                estimated_usd,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """, (
            request_id,
            user_id,
            asset,
            points,
            wallet,
            network,
            gift_recipient,
            estimated_usd,
            now(),
            now()
        ))

        account = db.execute("""
            SELECT username, first_name
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    username = account["username"] or "No username"
    first_name = account["first_name"] or "Unknown"

    destination = (
        f"Gift recipient: {gift_recipient}"
        if asset == "GIFT"
        else f"Wallet: {wallet}\nNetwork: TON"
    )

    message = (
        "🔔 <b>PROJECT 44 — NEW WITHDRAWAL</b>\n\n"
        f"Request ID: <code>{request_id}</code>\n"
        f"User: {first_name}\n"
        f"Username: @{username}\n"
        f"Telegram ID: <code>{user_id}</code>\n\n"
        f"Asset: <b>{asset}</b>\n"
        f"Points: <b>{points:,}</b>\n"
        f"Estimated value: ${estimated_usd:.2f}\n"
        f"{destination}\n\n"
        "Status: ⏳ PENDING"
    )

    notify_admin(message)

    return jsonify({
        "success": True,
        "request_id": request_id,
        "status": "pending",
        "message": "Your withdrawal request has been received. Payment is subject to admin review."
    }), 201


@app.route("/api/withdrawals", methods=["GET"])
def get_withdrawals():

    user = require_user()

    if not user:
        return jsonify({"message": "Authentication failed."}), 401

    with get_db() as db:

        rows = db.execute("""
            SELECT request_id, asset, points, status,
                   estimated_usd, created_at, tx_reference
            FROM withdrawals
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (int(user["id"]),)).fetchall()

    return jsonify({
        "withdrawals": [dict(row) for row in rows]
    })


# ==========================================
# 11. ADMIN WITHDRAWAL ACTIONS
# ==========================================

def admin_withdrawal_action(action, request_id, tx_reference=None):

    if action not in ("approve", "reject", "paid"):
        return False, "Invalid action."

    with get_db() as db:

        withdrawal = db.execute("""
            SELECT * FROM withdrawals
            WHERE request_id = ?
        """, (request_id,)).fetchone()

        if not withdrawal:
            return False, "Withdrawal not found."

        status = withdrawal["status"]
        user_id = withdrawal["user_id"]
        points = withdrawal["points"]

        if action == "approve":

            if status != "pending":
                return False, "Only pending requests can be approved."

            db.execute("""
                UPDATE withdrawals
                SET status = 'approved', updated_at = ?
                WHERE request_id = ?
            """, (now(), request_id))

        elif action == "reject":

            if status not in ("pending", "approved"):
                return False, "This request cannot be rejected."

            db.execute("""
                UPDATE withdrawals
                SET status = 'rejected', updated_at = ?
                WHERE request_id = ?
            """, (now(), request_id))

        elif action == "paid":

            if status != "approved":
                return False, "Approve the request before marking it paid."

            account = db.execute("""
                SELECT points FROM users
                WHERE user_id = ?
            """, (user_id,)).fetchone()

            if not account or account["points"] < points:
                return False, "Insufficient account balance."

            db.execute("""
                UPDATE users
                SET points = points - ?
                WHERE user_id = ?
            """, (points, user_id))

            db.execute("""
                UPDATE withdrawals
                SET status = 'paid',
                    tx_reference = ?,
                    updated_at = ?
                WHERE request_id = ?
            """, (tx_reference, now(), request_id))

    if action == "approve":
        notify_user(user_id, f"Your Project 44 withdrawal {request_id} has been approved. Payment is still pending.")

    elif action == "reject":
        notify_user(user_id, f"Your Project 44 withdrawal {request_id} was rejected. Your reserved points have been released.")

    elif action == "paid":
        notify_user(user_id, f"Your Project 44 withdrawal {request_id} has been marked as paid. Reference: {tx_reference or 'Not supplied'}")

    return True, "Withdrawal updated."


# ==========================================
# 12. TELEGRAM BOT WEBHOOK
# ==========================================

@app.route("/webhook", methods=["POST"])
def telegram_webhook():

    if not WEBHOOK_SECRET:
        return "Webhook secret not configured.", 503

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):
        return "Unauthorized", 403

    update = request.get_json(silent=True) or {}

    message = update.get("message", {})

    if not message:
        return "OK", 200

    chat = message.get("chat", {})
    sender = message.get("from", {})
    text = message.get("text", "")

    if chat.get("type") != "private":
        return "OK", 200

    sender_id = str(sender.get("id", ""))

    # User starts the bot with an optional referral code.
    if text.startswith("/start"):

        parts = text.split(maxsplit=1)
        referral_id = None

        if len(parts) > 1 and parts[1].startswith("ref_"):
            referral_id = parts[1][4:]

        create_user(sender, referral_id)

        send_telegram_message(
            chat["id"],
            "🚀 <b>Welcome to Project 44!</b>\n\n"
            "Discover rewards and opportunities.\n\n"
            "Open the Project 44 Mini App using the button below."
        )

        # Mini App menu/button configuration is handled separately.
        return "OK", 200

    # Admin commands:
    # /approve P44-REQUEST
    # /reject P44-REQUEST
    # /paid P44-REQUEST REFERENCE

    if sender_id not in ADMIN_USER_IDS:
        return "OK", 200

    parts = text.strip().split()

    if len(parts) >= 2 and parts[0] in (
        "/approve", "/reject", "/paid"
    ):

        command = parts[0][1:]

        request_id = parts[1]

        tx_reference = (
            " ".join(parts[2:])
            if command == "paid" and len(parts) > 2
            else None
        )

        if command == "paid" and not tx_reference:
            send_telegram_message(
                chat["id"],
                "Usage: /paid REQUEST_ID TRANSACTION_REFERENCE"
            )
            return "OK", 200

        success, result = admin_withdrawal_action(
            command,
            request_id,
            tx_reference
        )

        send_telegram_message(
            chat["id"],
            ("✅ " if success else "⚠️ ") + result
        )

    return "OK", 200


# ==========================================
# 13. ADSGRAM REWARD VERIFICATION PLACEHOLDER
# ==========================================

@app.route("/adsgram/reward", methods=["GET"])
def adsgram_reward():

    # IMPORTANT:
    # Do not credit points from an unauthenticated GET request.
    # AdsGram's current publisher documentation does not establish
    # a callback signature that this application can validate.
    #
    # Implement this endpoint only after AdsGram confirms
    # a secure callback authentication method.

    return jsonify({
        "accepted": False,
        "message": "Secure AdsGram callback verification is not configured."
    }), 501


# ==========================================
# 14. APPLICATION START
# ==========================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False
    )

