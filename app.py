import os, json, hmac, hashlib, secrets, sqlite3, urllib.parse, html
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID", "")
ADMIN_USER_IDS = {x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "project44.db")
POINTS_PER_REFERRAL = 5
AD_REWARD_POINTS = int(os.getenv("AD_REWARD_POINTS", "5"))
MIN_WITHDRAWAL, MAX_WITHDRAWAL = 500, 2000
USD_PER_POINT = 0.0026
ASSET_RATES = {"ATF": 0, "GRAM": 0, "USDT": 1}

def now():
    return datetime.now(timezone.utc).isoformat()

def esc(v):
    return html.escape(str(v or ""), quote=True)

def get_db():
    c = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    with get_db() as d:
        d.execute("PRAGMA journal_mode=WAL")
        d.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            points INTEGER DEFAULT 0, referred_by INTEGER, created_at TEXT)""")
        d.execute("""CREATE TABLE IF NOT EXISTS withdrawals(
            request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, asset TEXT NOT NULL,
            points INTEGER NOT NULL, wallet_address TEXT, network TEXT, gift_telegram TEXT,
            status TEXT DEFAULT 'pending', estimated_usd REAL, created_at TEXT,
            updated_at TEXT, tx_reference TEXT)""")
        d.execute("""CREATE TABLE IF NOT EXISTS referrals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL,
            referred_id INTEGER UNIQUE NOT NULL, status TEXT DEFAULT 'pending',
            created_at TEXT, verified_at TEXT)""")
        d.execute("""CREATE TABLE IF NOT EXISTS ad_sessions(
            session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending', created_at TEXT, completed_at TEXT)""")
        d.execute("""CREATE TABLE IF NOT EXISTS reward_ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            points INTEGER NOT NULL, reason TEXT NOT NULL,
            reference TEXT UNIQUE NOT NULL, created_at TEXT)""")
        # Compatibility migrations for an existing SQLite database.
        for table, col, definition in [
            ("referrals","verified_at","TEXT"),
            ("ad_sessions","completed_at","TEXT")]:
            cols={r["name"] for r in d.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                d.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
init_db()
@app.route("/debug/database")
def debug_database():
    try:
        with get_db() as db:
            users = db.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            tables = db.execute("""
                SELECT name FROM sqlite_master
                WHERE type = 'table'
            """).fetchall()

        return jsonify({
            "database": DATABASE_PATH,
            "connected": True,
            "users_registered": users,
            "tables": [row["name"] for row in tables]
        })

    except Exception as error:
        return jsonify({
            "connected": False,
            "error": str(error)
        }), 500

def tg_send(chat_id, text):
    if not BOT_TOKEN or not chat_id: return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id":chat_id,"text":text,"parse_mode":"HTML"},timeout=15)
        if not r.ok: app.logger.error("Telegram send failed: %s",r.text[:400])
        return r.ok
    except requests.RequestException as e:
        app.logger.error("Telegram request failed: %s",e); return False

def notify_admin(text): return tg_send(ADMIN_GROUP_ID,text)
def notify_user(uid,text): return tg_send(uid,text)

def verify_init_data(raw):
    if not BOT_TOKEN or not raw: return None
    try:
        p=dict(urllib.parse.parse_qsl(raw,keep_blank_values=True))
        received=p.pop("hash",None)
        if not received: return None
        ts=int(p.get("auth_date",0)); current=datetime.now(timezone.utc).timestamp()
        if ts>current+60 or current-ts>3600: return None
        check="\n".join(f"{k}={v}" for k,v in sorted(p.items()))
        sk=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
        calc=hmac.new(sk,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc,received): return None
        u=json.loads(p.get("user","{}"))
        return u if u.get("id") else None
    except (ValueError,TypeError,json.JSONDecodeError): return None

def user_from_request():
    return verify_init_data(request.headers.get("X-Telegram-Init-Data",""))

def create_user(u, ref=None):
    uid=int(u["id"]); username=u.get("username"); first=u.get("first_name")
    with get_db() as d:
        d.execute("BEGIN IMMEDIATE")
        old=d.execute("SELECT user_id FROM users WHERE user_id=?",(uid,)).fetchone()
        if old:
            d.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?",(username,first,uid))
            d.commit(); return False
        refid=None
        try:
            r=int(ref) if ref else None
            if r and r!=uid and d.execute("SELECT 1 FROM users WHERE user_id=?",(r,)).fetchone(): refid=r
        except (ValueError,TypeError): pass
        d.execute("INSERT INTO users(user_id,username,first_name,points,referred_by,created_at) VALUES(?,?,?,0,?,?)",
                 (uid,username,first,refid,now()))
        if refid:
            d.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,status,created_at) VALUES(?,?,'pending',?)",
                     (refid,uid,now()))
        d.commit(); return True

def verify_referral(new_uid):
    # New invitee must have started the bot; award once using unique ledger reference.
    with get_db() as d:
        d.execute("BEGIN IMMEDIATE")
        row=d.execute("SELECT referrer_id,status FROM referrals WHERE referred_id=?",(int(new_uid),)).fetchone()
        if not row or row["status"]=="verified": d.commit(); return False
        refid=int(row["referrer_id"]); ref=f"referral:{int(new_uid)}"
        added=d.execute("""INSERT OR IGNORE INTO reward_ledger(user_id,points,reason,reference,created_at)
            VALUES(?,?,?,?,?)""",(refid,POINTS_PER_REFERRAL,"verified_referral",ref,now())).rowcount
        if added: d.execute("UPDATE users SET points=points+? WHERE user_id=?",(POINTS_PER_REFERRAL,refid))
        d.execute("UPDATE referrals SET status='verified',verified_at=? WHERE referred_id=?",(now(),int(new_uid)))
        d.commit(); return bool(added)

def available_points(d,uid):
    u=d.execute("SELECT points FROM users WHERE user_id=?",(uid,)).fetchone()
    if not u: return 0
    reserved=d.execute("""SELECT COALESCE(SUM(points),0) n FROM withdrawals WHERE user_id=?
        AND status IN ('pending','approved')""",(uid,)).fetchone()["n"]
    return int(u["points"])-int(reserved)

@app.route("/")
def home(): return render_template("index.html")

@app.route("/health")
def health(): return jsonify(status="online",project="Project 44")


@app.route("/api/status", methods=["GET"])
def api_status():
    user = require_user()

    if not user:
        app.logger.warning("Project44: Telegram authentication failed")
        return jsonify({
            "registered": False,
            "message": "Telegram authentication failed"
        }), 401

    try:
        create_user(user)
        user_id = int(user["id"])

        with get_db() as db:
            account = db.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if not account:
                app.logger.error(
                    "Project44: create_user ran but user was not saved: %s",
                    user_id
                )
                return jsonify({
                    "registered": False,
                    "message": "User creation failed"
                }), 500

            available = get_available_points(db, user_id)

            completed_ads = db.execute("""
                SELECT COUNT(*) AS total
                FROM reward_ledger
                WHERE user_id = ?
                AND reason = 'verified_ad'
            """, (user_id,)).fetchone()["total"]

        return jsonify({
            "registered": True,
            "points": available,
            "completed_ads": completed_ads,
            "message": "User successfully registered"
        })

    except Exception:
        app.logger.exception("Project44 registration error")
        return jsonify({
            "registered": False,
            "message": "Internal registration error"
        }), 500
        
    

@app.route("/api/ad/start",methods=["POST"])
def ad_start():
    u=user_from_request()
    if not u: return jsonify(message="Authentication failed."),401
    create_user(u); sid=secrets.token_urlsafe(24)
    with get_db() as d:
        d.execute("INSERT INTO ad_sessions(session_id,user_id,status,created_at) VALUES(?,?,'pending',?)",
                 (sid,int(u["id"]),now()))
    return jsonify(session_id=sid,status="pending")

@app.route("/api/ad/complete",methods=["POST"])
def ad_complete():
    u=user_from_request()
    if not u: return jsonify(message="Authentication failed."),401
    data=request.get_json(silent=True) or {}; sid=data.get("session_id")
    if not sid: return jsonify(message="Missing ad session."),400
    with get_db() as d:
        row=d.execute("SELECT status FROM ad_sessions WHERE session_id=? AND user_id=?",
                      (sid,int(u["id"]))).fetchone()
        if not row: return jsonify(message="Invalid ad session."),404
        d.execute("UPDATE ad_sessions SET status='awaiting_verification' WHERE session_id=? AND status='pending'",(sid,))
    return jsonify(credited=False,status="awaiting_verification",
                  message="Client completion is not trusted proof. Reward awaits secure provider verification.")

@app.route("/api/referrals")
def referrals_api():
    u=user_from_request()
    if not u: return jsonify(message="Authentication failed."),401
    create_user(u); uid=int(u["id"])
    with get_db() as d:
        n=d.execute("SELECT COUNT(*) n FROM referrals WHERE referrer_id=? AND status='verified'",(uid,)).fetchone()["n"]
    link=f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else ""
    return jsonify(referral_link=link,verified_referrals=n,points_per_referral=POINTS_PER_REFERRAL)

@app.route("/api/withdrawals",methods=["POST"])
def withdrawal_create():
    u=user_from_request()
    if not u: return jsonify(message="Authentication failed."),401
    data=request.get_json(silent=True) or {}; uid=int(u["id"])
    asset=str(data.get("asset","")).upper()
    try: pts=int(data.get("points",0))
    except (ValueError,TypeError): return jsonify(message="Invalid points amount."),400
    if asset not in ("ATF","GRAM","USDT","GIFT"): return jsonify(message="Invalid reward asset."),400
    if not MIN_WITHDRAWAL<=pts<=MAX_WITHDRAWAL:
        return jsonify(message="Withdrawal must be between 500 and 2,000 points."),400
    wallet=data.get("wallet_address"); network=data.get("network"); gift=data.get("gift_telegram")
    if asset=="GIFT":
        if not isinstance(gift,str) or not gift.strip(): return jsonify(message="Telegram gift recipient required."),400
        wallet=network=None
    else:
        if not isinstance(wallet,str) or not wallet.strip(): return jsonify(message="TON wallet required."),400
        if network!="TON": return jsonify(message="Only TON network is supported."),400
        gift=None
    rid="P44-"+secrets.token_hex(5).upper(); usd=round(pts*USD_PER_POINT,2)
    with get_db() as d:
        d.execute("BEGIN IMMEDIATE")
        if available_points(d,uid)<pts:
            d.rollback(); return jsonify(message="Insufficient available points."),400
        d.execute("""INSERT INTO withdrawals(request_id,user_id,asset,points,wallet_address,network,
            gift_telegram,status,estimated_usd,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",(rid,uid,asset,pts,wallet,network,gift,usd,now(),now()))
        acct=d.execute("SELECT username,first_name FROM users WHERE user_id=?",(uid,)).fetchone()
        d.commit()
    dest=f"Gift recipient: {esc(gift)}" if asset=="GIFT" else f"Wallet: <code>{esc(wallet)}</code>\nNetwork: TON"
    notify_admin("🔔 <b>PROJECT 44 — NEW WITHDRAWAL</b>\n\n"
        f"Request: <code>{rid}</code>\nUser: {esc(acct['first_name'] or 'Unknown')}\n"
        f"Username: @{esc(acct['username'] or 'No username')}\nTelegram ID: <code>{uid}</code>\n"
        f"Asset: <b>{esc(asset)}</b>\nPoints: <b>{pts:,}</b>\nEstimated: ${usd:.2f}\n{dest}\nStatus: ⏳ PENDING")
    return jsonify(success=True,request_id=rid,status="pending",
                   message="Withdrawal submitted for admin review."),201

@app.route("/api/withdrawals",methods=["GET"])
def withdrawals_list():
    u=user_from_request()
    if not u: return jsonify(message="Authentication failed."),401
    with get_db() as d:
        rows=d.execute("""SELECT request_id,asset,points,status,estimated_usd,created_at,tx_reference
            FROM withdrawals WHERE user_id=? ORDER BY created_at DESC""",(int(u["id"]),)).fetchall()
    return jsonify(withdrawals=[dict(x) for x in rows])

def admin_action(action,rid,tx=None):
    if action not in ("approve","reject","paid"): return False,"Invalid action."
    with get_db() as d:
        d.execute("BEGIN IMMEDIATE")
        w=d.execute("SELECT * FROM withdrawals WHERE request_id=?",(rid,)).fetchone()
        if not w: d.rollback(); return False,"Withdrawal not found."
        uid,pts,st=w["user_id"],w["points"],w["status"]
        if action=="approve":
            if st!="pending": d.rollback(); return False,"Only pending requests can be approved."
            d.execute("UPDATE withdrawals SET status='approved',updated_at=? WHERE request_id=?",(now(),rid))
        elif action=="reject":
            if st not in ("pending","approved"): d.rollback(); return False,"Cannot reject this request."
            d.execute("UPDATE withdrawals SET status='rejected',updated_at=? WHERE request_id=?",(now(),rid))
        else:
            if st!="approved": d.rollback(); return False,"Approve before marking paid."
            account=d.execute("SELECT points FROM users WHERE user_id=?",(uid,)).fetchone()
            if not account or account["points"]<pts: d.rollback(); return False,"Insufficient user balance."
            d.execute("UPDATE users SET points=points-? WHERE user_id=?",(pts,uid))
            d.execute("UPDATE withdrawals SET status='paid',tx_reference=?,updated_at=? WHERE request_id=?",(tx,now(),rid))
        d.commit()
    if action=="approve": notify_user(uid,f"Withdrawal {rid} approved. Payment pending.")
    elif action=="reject": notify_user(uid,f"Withdrawal {rid} rejected. Reserved points released.")
    else: notify_user(uid,f"Withdrawal {rid} paid. Reference: {esc(tx or 'Not supplied')}")
    return True,"Withdrawal updated."

@app.route("/webhook",methods=["POST"])
def telegram_webhook():
    if not WEBHOOK_SECRET: return "Webhook secret not configured.",503
    token=request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
    if not hmac.compare_digest(token,WEBHOOK_SECRET): return "Unauthorized",403
    upd=request.get_json(silent=True) or {}
    msg=upd.get("message") or upd.get("edited_message") or {}
    if not msg: return "OK",200
    chat=msg.get("chat",{}); sender=msg.get("from",{}); text=msg.get("text","")
    sid=str(sender.get("id","")); chatid=chat.get("id")
    in_admin_chat=(chat.get("type")=="private" or str(chatid)==str(ADMIN_GROUP_ID))
    if sid in ADMIN_USER_IDS and in_admin_chat:
        parts=text.strip().split()
        if len(parts)>=2 and parts[0].split("@")[0] in ("/approve","/reject","/paid"):
            cmd=parts[0].split("@")[0][1:]; rid=parts[1]
            tx=" ".join(parts[2:]) if cmd=="paid" and len(parts)>2 else None
            if cmd=="paid" and not tx:
                tg_send(chatid,"Usage: /paid REQUEST_ID TRANSACTION_REFERENCE"); return "OK",200
            ok,result=admin_action(cmd,rid,tx)
            tg_send(chatid,("✅ " if ok else "⚠️ ")+esc(result)); return "OK",200
    if chat.get("type")!="private": return "OK",200
    if text.startswith("/start"):
        args=text.split(maxsplit=1); ref=args[1][4:] if len(args)>1 and args[1].startswith("ref_") else None
        new=create_user(sender,ref)
        if new: verify_referral(int(sender["id"]))
        tg_send(chatid,"🚀 <b>Welcome to Project 44!</b>\n\nOpen Project 44 from the Mini App button.")
    return "OK",200

@app.route("/adsgram/reward",methods=["GET","POST"])
def adsgram_reward():
    # Security: never credit based on a client callback without documented signature verification.
    return jsonify(accepted=False,message="Secure AdsGram server-side verification is not configured."),501

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
