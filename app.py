import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).parent
DATABASE = ROOT / "auth.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_DB_READY = False
_TELEGRAM_WEBHOOK_READY = False
PASSWORD_ROUNDS = 310_000
LOCK_SECONDS = 6 * 60 * 60
SESSION_SECONDS = 7 * 24 * 60 * 60
ORDER_RATE_MICROS = {
    "views": 85,
    "likes": 5210,
    "comments": 52000,
    "saves": 312,
    "shares": 520,
    "reposts": 2080,
}
ADMIN_EMAIL = "aryan793gupta@gmail.com"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
METRIC_LIMITS = {
    "views": (100, 10000000),
    "likes": (10, 500000),
    "comments": (1, 100000),
    "saves": (10, 500000),
    "shares": (10, 100000),
    "reposts": (10, 1000000),
}
CURVE_SERIES = ("views", "likes", "saves", "shares")


def telegram_webhook_secret(token):
    return hmac.new(token.encode(), b"r-smm-telegram-payment-approvals-v1", hashlib.sha256).hexdigest()


def telegram_api_call(token, method, payload, timeout=3):
    try:
        request = Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
            return result if response.status == 200 and isinstance(result, dict) else None
    except Exception as error:
        print("R-SMM Telegram API call failed:", method, type(error).__name__)
    return None


def ensure_telegram_webhook(token):
    global _TELEGRAM_WEBHOOK_READY
    if _TELEGRAM_WEBHOOK_READY:
        return True
    if not os.environ.get("VERCEL"):
        return False
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        webhook_url = "https://r-smm.vercel.app/api/telegram/webhook"
    response = telegram_api_call(token, "setWebhook", {
        "url": webhook_url,
        "secret_token": telegram_webhook_secret(token),
        "allowed_updates": ["callback_query"],
    })
    _TELEGRAM_WEBHOOK_READY = bool(response and response.get("ok"))
    if not _TELEGRAM_WEBHOOK_READY:
        print("R-SMM Telegram webhook setup failed.")
    return _TELEGRAM_WEBHOOK_READY


def notify_admin_telegram(message, reply_markup=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    payload = {"chat_id": chat_id, "text": message}
    if reply_markup is not None:
        if ensure_telegram_webhook(token):
            payload["reply_markup"] = reply_markup
        else:
            payload["text"] += "\n\nTelegram actions are unavailable right now; review this request in the admin panel."
    response = telegram_api_call(token, "sendMessage", payload)
    if not response or not response.get("ok"):
        print("R-SMM Telegram notification failed.")


class PostgresConnection:
    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg = psycopg
        self.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *args):
        return self.db.__exit__(*args)

    def execute(self, sql, params=()):
        sql = re.sub(r"VALUES\s*\(NULL,", "VALUES (DEFAULT,", sql, flags=re.IGNORECASE)
        sql = sql.replace("?", "%s")
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            sql = "BEGIN"
        try:
            return self.db.execute(sql, params)
        except self.psycopg.IntegrityError as error:
            raise sqlite3.IntegrityError(str(error)) from error

    def executescript(self, sql):
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()


def connect():
    if DATABASE_URL:
        return PostgresConnection()
    if os.environ.get("VERCEL"):
        raise RuntimeError("DATABASE_URL is required for deployed storage.")
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def row_lock(sql):
    return sql + (" FOR UPDATE" if DATABASE_URL else "")


def init_db():
    with connect() as db:
        if DATABASE_URL:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY, name TEXT NOT NULL, salt BYTEA NOT NULL,
                password_hash BYTEA NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until BIGINT NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'customer',
                currency TEXT NOT NULL DEFAULT 'USDT', balance_micros BIGINT NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
                expires BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email), created_at BIGINT NOT NULL,
                platform TEXT NOT NULL, post_url TEXT NOT NULL, views INTEGER NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros BIGINT NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending', rejection_reason TEXT,
                CONSTRAINT orders_status_check_v2 CHECK (status IN ('pending', 'completed', 'rejected'))
            );
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email), created_at BIGINT NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros BIGINT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('deposit', 'order')),
                reference TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email), created_at BIGINT NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros BIGINT NOT NULL CHECK (amount_micros > 0),
                reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'credited', 'rejected')),
                reviewed_at BIGINT
            );
            """)
            db.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
            status_constraint = db.execute("""
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'orders'::regclass AND conname = 'orders_status_check_v2'
            """).fetchone()
            if not status_constraint:
                db.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check")
                db.execute("ALTER TABLE orders ADD CONSTRAINT orders_status_check_v2 "
                           "CHECK (status IN ('pending', 'completed', 'rejected'))")
        else:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'customer',
                currency TEXT NOT NULL DEFAULT 'USDT',
                balance_micros INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
                expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email),
                created_at INTEGER NOT NULL,
                platform TEXT NOT NULL,
                post_url TEXT NOT NULL,
                views INTEGER NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros INTEGER NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected')),
                rejection_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email),
                created_at INTEGER NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('deposit', 'order')),
                reference TEXT NOT NULL,
                UNIQUE (reference)
            );
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL REFERENCES users(email),
                created_at INTEGER NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                amount_micros INTEGER NOT NULL CHECK (amount_micros > 0),
                reference TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'credited', 'rejected')),
                reviewed_at INTEGER
            );
        """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "role" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'customer'")
            if "currency" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN currency TEXT NOT NULL DEFAULT 'USDT'")
            if "balance_micros" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN balance_micros INTEGER NOT NULL DEFAULT 0")
            order_columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
            if "settings_json" not in order_columns:
                db.execute("ALTER TABLE orders ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'")
            if "status" not in order_columns:
                db.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            if "rejection_reason" not in order_columns:
                db.execute("ALTER TABLE orders ADD COLUMN rejection_reason TEXT")
            order_sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'").fetchone()["sql"]
            if "'rejected'" not in order_sql:
                db.execute("ALTER TABLE orders RENAME TO orders_legacy")
                db.execute("""
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email),
                        created_at INTEGER NOT NULL, platform TEXT NOT NULL, post_url TEXT NOT NULL,
                        views INTEGER NOT NULL, currency TEXT NOT NULL CHECK (currency IN ('USDT', 'USDC')),
                        amount_micros INTEGER NOT NULL, settings_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected')),
                        rejection_reason TEXT
                    )
                """)
                db.execute("""
                    INSERT INTO orders (id, email, created_at, platform, post_url, views, currency,
                                        amount_micros, settings_json, status, rejection_reason)
                    SELECT id, email, created_at, platform, post_url, views, currency,
                           amount_micros, settings_json, status, rejection_reason FROM orders_legacy
                """)
                db.execute("DROP TABLE orders_legacy")

        admin_password = os.environ.pop("RSMM_ADMIN_PASSWORD", "")
        if admin_password and DATABASE_URL:
            salt = secrets.token_bytes(16)
            db.execute("""
                INSERT INTO users (email, name, salt, password_hash, role)
                VALUES (?, 'Aryan', ?, ?, 'admin') ON CONFLICT(email) DO NOTHING
            """, (ADMIN_EMAIL, salt, password_hash(admin_password, salt)))
        elif admin_password:
            salt = secrets.token_bytes(16)
            db.execute("""
                INSERT INTO users (email, name, salt, password_hash, role)
                VALUES (?, 'Aryan', ?, ?, 'admin')
                ON CONFLICT(email) DO UPDATE SET name = 'Aryan', salt = excluded.salt,
                    password_hash = excluded.password_hash, role = 'admin',
                    failed_attempts = 0, locked_until = 0
            """, (ADMIN_EMAIL, salt, password_hash(admin_password, salt)))


def ensure_db():
    global _DB_READY
    if not _DB_READY:
        init_db()
        _DB_READY = True


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS)


def valid_order_settings(settings, views):
    if not isinstance(settings, dict) or len(json.dumps(settings)) > 8000:
        return False
    items = settings.get("metrics")
    curves = settings.get("curves")
    if not isinstance(items, list) or len(items) != len(METRIC_LIMITS) or not isinstance(curves, dict):
        return False
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            return False
        kind = item.get("type")
        quantity = item.get("quantity")
        interval = item.get("interval_minutes")
        legacy_delivery = item.get("delivery")
        if "interval_minutes" in item:
            delivery_is_valid = (not isinstance(interval, bool) and isinstance(interval, int)
                                 and 0 <= interval <= 10080
                                 and (item.get("enabled") or interval == 0))
        else:
            delivery_is_valid = (isinstance(legacy_delivery, list) and len(legacy_delivery) == 4
                                 and all(isinstance(value, str) and len(value) <= 50 for value in legacy_delivery))
        if (not isinstance(kind, str) or kind not in METRIC_LIMITS or kind in seen or isinstance(quantity, bool)
                or not isinstance(quantity, int) or not METRIC_LIMITS[kind][0] <= quantity <= METRIC_LIMITS[kind][1]
                or not isinstance(item.get("enabled"), bool)
                or not delivery_is_valid):
            return False
        if kind == "views" and quantity != views:
            return False
        seen.add(kind)
    if seen != set(METRIC_LIMITS) or not any(item["enabled"] for item in items) or set(curves) != set(CURVE_SERIES):
        return False
    for points in curves.values():
        if not isinstance(points, list) or not 2 <= len(points) <= 20:
            return False
        last_x = -1
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not 0 <= value <= 1 for value in point)
                    or point[0] <= last_x):
                return False
            last_x = point[0]
        if points[0][0] != 0 or points[-1][0] != 1:
            return False
    return isinstance(settings.get("drawing_enabled"), bool)


def order_amount_micros(settings):
    return sum(item["quantity"] * ORDER_RATE_MICROS[item["type"]]
               for item in settings["metrics"] if item["enabled"])


class App(BaseHTTPRequestHandler):
    def respond(self, status, data, cookie=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 16384:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def session_user(self):
        cookies = self.headers.get("Cookie", "")
        token = next((part.split("=", 1)[1] for part in cookies.split("; ")
                      if part.startswith("session=")), None)
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with connect() as db:
            return db.execute("""
                SELECT users.email, users.name, users.role, users.currency, users.balance_micros FROM sessions
                JOIN users USING (email)
                WHERE sessions.token_hash = ? AND sessions.expires > ?
            """, (digest, int(time.time()))).fetchone()

    def create_session(self, email):
        token = secrets.token_urlsafe(32)
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (
                hashlib.sha256(token.encode()).hexdigest(),
                email,
                int(time.time()) + SESSION_SECONDS,
            ))
        secure = "; Secure" if os.environ.get("VERCEL") else ""
        return f"session={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={SESSION_SECONDS}"

    def do_GET(self):
        if self.path == "/":
            page = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif self.path == "/api/me":
            user = self.session_user()
            self.respond(200, {"authenticated": bool(user), "name": user["name"] if user else "",
                               "email": user["email"] if user else "",
                               "role": user["role"] if user else "customer",
                               "currency": user["currency"] if user else "USDT",
                               "balance_micros": user["balance_micros"] if user else 0})
        elif self.path == "/api/admin/summary":
            self.admin_summary()
        elif self.path == "/api/orders":
            self.customer_orders()
        elif self.path == "/api/wallet":
            self.customer_wallet()
        else:
            self.respond(404, {"error": "Not found."})

    def do_POST(self):
        try:
            data = self.read_json()
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            if self.path == "/api/signup":
                self.signup(data)
            elif self.path == "/api/login":
                self.login(data)
            elif self.path == "/api/order":
                self.create_order(data)
            elif self.path == "/api/deposits":
                self.create_deposit_request(data)
            elif re.fullmatch(r"/api/admin/orders/\d+/complete", self.path):
                self.complete_order(data)
            elif re.fullmatch(r"/api/admin/orders/\d+/reject", self.path):
                self.reject_order(data)
            elif re.fullmatch(r"/api/admin/deposits/\d+/(credit|reject)", self.path):
                self.review_deposit(data)
            elif self.path == "/api/telegram/webhook":
                self.telegram_webhook(data)
            elif self.path == "/api/logout":
                self.logout()
            else:
                self.respond(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError):
            self.respond(400, {"error": "Please check the information and try again."})

    def signup(self, data):
        name = data.get("name", "")
        email = data.get("email", "")
        password = data.get("password", "")
        currency = data.get("currency", "USDT")
        if (not isinstance(name, str) or not isinstance(email, str)
                or not 1 <= len(name.strip()) <= 60
                or len(email) > 254 or not EMAIL_RE.fullmatch(email.strip())
                or not isinstance(password, str) or not 10 <= len(password) <= 128
                or currency not in ("USDT", "USDC")):
            self.respond(400, {"error": "Add your name, a valid email, a supported wallet currency, and a password with at least 10 characters."})
            return
        name = name.strip()
        email = email.strip().lower()
        if email == ADMIN_EMAIL:
            self.respond(403, {"error": "This administrator account is reserved."})
            return
        salt = secrets.token_bytes(16)
        try:
            with connect() as db:
                db.execute("INSERT INTO users (email, name, salt, password_hash, currency) VALUES (?, ?, ?, ?, ?)",
                           (email, name, salt, password_hash(password, salt), currency))
        except sqlite3.IntegrityError:
            self.respond(409, {"error": "An account with that email already exists. Sign in instead."})
            return
        self.respond(201, {"ok": True, "name": name, "email": email, "role": "customer",
                           "currency": currency, "balance_micros": 0},
                     self.create_session(email))

    def login(self, data):
        email = data.get("email", "")
        password = data.get("password", "")
        if (not isinstance(email, str) or not isinstance(password, str)
                or len(email) > 254 or len(password) > 128):
            self.respond(400, {"error": "Enter your email and password."})
            return
        email = email.strip().lower()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute(row_lock("SELECT * FROM users WHERE email = ?"), (email,)).fetchone()
            now = int(time.time())
            if not user:
                password_hash(password, b"login-dummy-salt")
                db.commit()
                self.respond(401, {"error": "That email and password don’t match."})
                return
            if user["locked_until"] > now:
                remaining = user["locked_until"] - now
                remaining_hours = (remaining + 3599) // 3600
                db.commit()
                self.respond(429, {"error": f"Too many tries. This email is locked for about {remaining_hours} more hours."})
                return
            attempts = user["failed_attempts"] if user["locked_until"] == 0 else 0
            valid = secrets.compare_digest(password_hash(password, user["salt"]), user["password_hash"])
            if not valid:
                attempts += 1
                locked_until = now + LOCK_SECONDS if attempts >= 3 else 0
                db.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE email = ?",
                           (attempts, locked_until, email))
                db.commit()
                if locked_until:
                    self.respond(429, {"error": "Too many tries. This email is locked for 6 hours."})
                else:
                    self.respond(401, {"error": f"That email and password don’t match. {3 - attempts} tries left."})
                return
            db.execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE email = ?", (email,))
            db.commit()
        self.respond(200, {"ok": True, "name": user["name"], "email": user["email"], "role": user["role"],
                           "currency": user["currency"], "balance_micros": user["balance_micros"]},
                     self.create_session(email))

    def create_order(self, data):
        user = self.session_user()
        if not user:
            self.respond(401, {"error": "Please sign in again."})
            return
        if user["role"] != "customer":
            self.respond(403, {"error": "Administrator accounts cannot place customer orders."})
            return
        views = data.get("views")
        platform = data.get("platform")
        post_url = data.get("post_url")
        if (isinstance(views, bool) or not isinstance(views, int) or not 100 <= views <= 10000000
                or platform not in ("instagram", "tiktok")
                or not isinstance(post_url, str) or len(post_url) > 2048):
            self.respond(400, {"error": "Choose a platform, enter a valid post link, and set views between 100 and 10,000,000."})
            return
        parsed = urlsplit(post_url)
        host = (parsed.hostname or "").lower()
        hosts = {"instagram": ("instagram.com", "www.instagram.com"),
                 "tiktok": ("tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "m.tiktok.com")}
        if parsed.scheme != "https" or host not in hosts[platform]:
            self.respond(400, {"error": "Use a valid post link for the selected platform."})
            return
        settings = data.get("settings", {})
        if not valid_order_settings(settings, views):
            self.respond(400, {"error": "Review the engagement quantities and growth curve, then try again."})
            return
        amount = order_amount_micros(settings)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            wallet = db.execute(row_lock("SELECT currency, balance_micros FROM users WHERE email = ?"), (user["email"],)).fetchone()
            if wallet["balance_micros"] < amount:
                db.rollback()
                self.respond(402, {"error": "Your balance is too low for this order.",
                                   "balance_micros": wallet["balance_micros"], "currency": wallet["currency"],
                                   "required_micros": amount})
                return
            new_balance = wallet["balance_micros"] - amount
            db.execute("UPDATE users SET balance_micros = ? WHERE email = ?", (new_balance, user["email"]))
            cursor = db.execute("""
                INSERT INTO orders (email, created_at, platform, post_url, views, currency, amount_micros, settings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """, (user["email"], int(time.time()), platform, post_url, views, wallet["currency"], amount,
                  json.dumps(settings, separators=(",", ":"))))
            order_id = cursor.fetchone()["id"]
            db.execute("INSERT INTO wallet_transactions VALUES (NULL, ?, ?, ?, ?, 'order', ?)",
                       (user["email"], int(time.time()), wallet["currency"], -amount, f"order:{order_id}"))
            db.commit()
        notify_admin_telegram(
            f"New R-SMM order #{order_id}\nPlatform: {platform.title()}\n"
            f"Views: {views:,}\nAmount: {Decimal(amount) / Decimal(1_000_000):.2f} {wallet['currency']}"
        )
        self.respond(201, {"ok": True, "order_id": order_id, "views": views,
                           "amount_micros": amount, "balance_micros": new_balance, "currency": wallet["currency"]})

    def customer_orders(self):
        user = self.session_user()
        if not user:
            self.respond(401, {"error": "Please sign in again."})
            return
        with connect() as db:
            rows = db.execute("""
                SELECT id, created_at, platform, post_url, views, currency, amount_micros, status, rejection_reason
                FROM orders WHERE email = ? ORDER BY id DESC LIMIT 100
            """, (user["email"],)).fetchall()
        self.respond(200, {"orders": [dict(row) for row in rows]})

    def customer_wallet(self):
        user = self.session_user()
        if not user:
            self.respond(401, {"error": "Please sign in again."})
            return
        with connect() as db:
            transactions = db.execute("""
                SELECT created_at, currency, amount_micros, kind, reference
                FROM wallet_transactions WHERE email = ? ORDER BY id DESC LIMIT 100
            """, (user["email"],)).fetchall()
            deposits = db.execute("""
                SELECT id, created_at, currency,
                       CASE currency WHEN 'USDC' THEN 'Base' ELSE 'TRC20' END AS network,
                       amount_micros, reference, status
                FROM deposit_requests WHERE email = ? ORDER BY id DESC LIMIT 100
            """, (user["email"],)).fetchall()
        self.respond(200, {"transactions": [dict(row) for row in transactions],
                           "deposits": [dict(row) for row in deposits]})

    def create_deposit_request(self, data):
        user = self.session_user()
        if not user:
            self.respond(401, {"error": "Please sign in again."})
            return
        if user["role"] != "customer":
            self.respond(403, {"error": "Administrator accounts do not create customer deposits."})
            return
        currency = data.get("currency", "")
        amount_text = data.get("amount", "")
        reference = data.get("reference", "")
        if (currency not in ("USDT", "USDC") or not isinstance(amount_text, str)
                or len(amount_text) > 32 or not isinstance(reference, str)
                or not 8 <= len(reference.strip()) <= 256):
            self.respond(400, {"error": "Choose USDT or USDC, enter an amount, and include your transfer reference."})
            return
        try:
            amount = Decimal(amount_text)
        except InvalidOperation:
            self.respond(400, {"error": "Deposit amount must be a valid number."})
            return
        if not amount.is_finite():
            self.respond(400, {"error": "Deposit amount must be a finite number."})
            return
        amount_tuple = amount.as_tuple()
        decimal_places = max(0, -amount_tuple.exponent)
        for digit in reversed(amount_tuple.digits):
            if digit != 0:
                break
            decimal_places -= 1
        if amount < Decimal("2") or amount > 1000000 or decimal_places > 2:
            self.respond(400, {"error": "The minimum deposit is 2 USDT or USDC, with no more than 2 decimal places."})
            return
        amount_micros = int(amount * 1_000_000)
        reference = reference.strip().casefold()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            wallet = db.execute("SELECT currency FROM users WHERE email = ?", (user["email"],)).fetchone()
            prior_activity = db.execute("SELECT 1 FROM wallet_transactions WHERE email = ? LIMIT 1",
                                        (user["email"],)).fetchone()
            if prior_activity and wallet["currency"] != currency:
                db.rollback()
                self.respond(409, {"error": "This wallet already uses " + wallet["currency"] + "."})
                return
            created_at = int(time.time())
            try:
                cursor = db.execute("""
                    INSERT INTO deposit_requests (email, created_at, currency, amount_micros, reference)
                    VALUES (?, ?, ?, ?, ?) RETURNING id
                """, (user["email"], created_at, currency, amount_micros, reference))
            except sqlite3.IntegrityError:
                db.rollback()
                self.respond(409, {"error": "That transfer reference has already been submitted."})
                return
            deposit_id = cursor.fetchone()["id"]
            db.commit()
        reference_display = re.sub(r"\s+", " ", reference).strip()
        notify_admin_telegram(
            f"New R-SMM payment request #{deposit_id}\n"
            f"Amount: {Decimal(amount_micros) / Decimal(1_000_000):.2f} {currency}\n"
            f"Network: {'Base' if currency == 'USDC' else 'TRC20'}\n"
            f"Transfer reference: {reference_display}",
            reply_markup={"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"deposit:approve:{deposit_id}"},
                {"text": "❌ Reject", "callback_data": f"deposit:reject:{deposit_id}"},
            ]]},
        )
        self.respond(201, {"ok": True, "deposit_id": deposit_id, "created_at": created_at,
                           "currency": currency, "network": "Base" if currency == "USDC" else "TRC20",
                           "amount_micros": amount_micros, "reference": reference, "status": "pending"})

    def require_admin(self):
        user = self.session_user()
        if not user:
            self.respond(401, {"error": "Please sign in again."})
            return None
        if user["role"] != "admin":
            self.respond(403, {"error": "Administrator access is required."})
            return None
        return user

    def admin_summary(self):
        if not self.require_admin():
            return
        with connect() as db:
            orders = db.execute("""
                SELECT o.id, o.email, u.name, o.created_at, o.platform, o.post_url, o.views,
                       o.currency, o.amount_micros, o.status, o.settings_json, o.rejection_reason
                FROM orders o JOIN users u ON u.email = o.email
                WHERE o.status = 'pending' ORDER BY o.id ASC
            """).fetchall()
            deposits = db.execute("""
                SELECT d.id, d.email, u.name, d.created_at, d.currency,
                       CASE d.currency WHEN 'USDC' THEN 'Base' ELSE 'TRC20' END AS network,
                       d.amount_micros,
                       d.reference, d.status
                FROM deposit_requests d JOIN users u ON u.email = d.email
                WHERE d.status = 'pending' ORDER BY d.id ASC
            """).fetchall()
            completed_orders = db.execute("""
                SELECT o.id, o.email, u.name, o.created_at, o.platform, o.post_url, o.views,
                       o.currency, o.amount_micros, o.status, o.settings_json
                FROM orders o JOIN users u ON u.email = o.email
                WHERE o.status = 'completed' ORDER BY o.id DESC
            """).fetchall()
            approved_deposits = db.execute("""
                SELECT d.id, d.email, u.name, d.created_at, d.currency,
                       CASE d.currency WHEN 'USDC' THEN 'Base' ELSE 'TRC20' END AS network,
                       d.amount_micros, d.reference, d.status, d.reviewed_at
                FROM deposit_requests d JOIN users u ON u.email = d.email
                WHERE d.status = 'credited' ORDER BY d.id DESC
            """).fetchall()
            accounts = db.execute("""
                SELECT email, name, role, currency, balance_micros
                FROM users ORDER BY role, email
            """).fetchall()
        self.respond(200, {"pending_orders": [dict(row) for row in orders],
                           "pending_deposits": [dict(row) for row in deposits],
                           "completed_orders": [dict(row) for row in completed_orders],
                           "approved_deposits": [dict(row) for row in approved_deposits],
                           "accounts": [dict(row) for row in accounts]})

    def complete_order(self, data):
        if not self.require_admin():
            return
        order_id = int(self.path.split("/")[4])
        with connect() as db:
            changed = db.execute("UPDATE orders SET status = 'completed' WHERE id = ? AND status = 'pending'",
                                 (order_id,)).rowcount
            exists = db.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not exists:
            self.respond(404, {"error": "Order not found."})
        elif not changed:
            self.respond(409, {"error": "This order is already complete."})
        else:
            self.respond(200, {"ok": True, "order_id": order_id, "status": "completed"})

    def reject_order(self, data):
        if not self.require_admin():
            return
        reason = data.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            self.respond(400, {"error": "Enter a reason for rejecting this order."})
            return
        order_id = int(self.path.split("/")[4])
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute(row_lock("SELECT email, currency, amount_micros, status FROM orders WHERE id = ?"),
                                (order_id,)).fetchone()
            if not order:
                db.rollback()
                self.respond(404, {"error": "Order not found."})
                return
            if order["status"] != "pending":
                db.rollback()
                self.respond(409, {"error": "Only pending orders can be rejected."})
                return
            wallet = db.execute(row_lock("SELECT balance_micros FROM users WHERE email = ?"),
                                (order["email"],)).fetchone()
            balance = wallet["balance_micros"] + order["amount_micros"]
            reviewed_at = int(time.time())
            db.execute("UPDATE users SET balance_micros = ? WHERE email = ?", (balance, order["email"]))
            db.execute("UPDATE orders SET status = 'rejected', rejection_reason = ? WHERE id = ? AND status = 'pending'",
                       (reason.strip(), order_id))
            db.execute("INSERT INTO wallet_transactions VALUES (NULL, ?, ?, ?, ?, 'order', ?)",
                       (order["email"], reviewed_at, order["currency"], order["amount_micros"],
                        f"order-refund:{order_id}"))
            db.commit()
        self.respond(200, {"ok": True, "order_id": order_id, "status": "rejected",
                           "balance_micros": balance, "currency": order["currency"]})

    def review_deposit(self, data):
        admin = self.require_admin()
        if not admin:
            return
        parts = self.path.split("/")
        deposit_id = int(parts[4])
        action = parts[5]
        status, result = self.apply_deposit_review(deposit_id, action)
        self.respond(status, result)

    def apply_deposit_review(self, deposit_id, action):
        if action not in ("credit", "reject"):
            return 400, {"error": "Invalid payment action."}
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deposit = db.execute(row_lock("SELECT * FROM deposit_requests WHERE id = ?"), (deposit_id,)).fetchone()
            if not deposit:
                db.rollback()
                return 404, {"error": "Deposit request not found."}
            if deposit["status"] != "pending":
                db.rollback()
                return 409, {"error": "This deposit request has already been reviewed."}
            reviewed_at = int(time.time())
            if action == "reject":
                db.execute("UPDATE deposit_requests SET status = 'rejected', reviewed_at = ? WHERE id = ?",
                           (reviewed_at, deposit_id))
                db.commit()
                return 200, {"ok": True, "deposit_id": deposit_id, "status": "rejected"}
            wallet = db.execute(row_lock("SELECT currency, balance_micros FROM users WHERE email = ?"),
                                (deposit["email"],)).fetchone()
            prior_activity = db.execute("SELECT 1 FROM wallet_transactions WHERE email = ? LIMIT 1",
                                        (deposit["email"],)).fetchone()
            if prior_activity and wallet["currency"] != deposit["currency"]:
                db.rollback()
                return 409, {"error": "Customer wallet already uses " + wallet["currency"] + "."}
            balance = wallet["balance_micros"] + deposit["amount_micros"]
            db.execute("UPDATE users SET currency = ?, balance_micros = ? WHERE email = ?",
                       (deposit["currency"], balance, deposit["email"]))
            db.execute("INSERT INTO wallet_transactions VALUES (NULL, ?, ?, ?, ?, 'deposit', ?)",
                       (deposit["email"], reviewed_at, deposit["currency"], deposit["amount_micros"],
                        f"deposit-request:{deposit_id}"))
            db.execute("UPDATE deposit_requests SET status = 'credited', reviewed_at = ? WHERE id = ?",
                       (reviewed_at, deposit_id))
            db.commit()
        return 200, {"ok": True, "deposit_id": deposit_id, "status": "credited",
                     "currency": deposit["currency"], "balance_micros": balance}

    def telegram_webhook(self, update):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        received_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not token or not chat_id or not secrets.compare_digest(
                received_secret, telegram_webhook_secret(token)):
            self.respond(403, {"error": "Unauthorized."})
            return

        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            self.respond(200, {"ok": True})
            return
        callback_id = callback.get("id")
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if (str(sender.get("id", "")) != chat_id
                or str(chat.get("id", "")) != chat_id
                or chat.get("type") != "private"):
            if isinstance(callback_id, str):
                telegram_api_call(token, "answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "This action is only available to the configured admin.",
                    "show_alert": True,
                })
            self.respond(200, {"ok": True})
            return

        match = re.fullmatch(r"deposit:(approve|reject):(\d{1,18})", str(callback.get("data", "")))
        if not match or not isinstance(callback_id, str):
            if isinstance(callback_id, str):
                telegram_api_call(token, "answerCallbackQuery", {
                    "callback_query_id": callback_id, "text": "This payment button is invalid.",
                    "show_alert": True,
                })
            self.respond(200, {"ok": True})
            return

        button_action, deposit_id = match.groups()
        action = "credit" if button_action == "approve" else "reject"
        status, result = self.apply_deposit_review(int(deposit_id), action)
        if status == 200:
            outcome = "approved and credited" if action == "credit" else "rejected"
            telegram_api_call(token, "answerCallbackQuery", {
                "callback_query_id": callback_id, "text": f"Payment request {outcome}.",
            })
            message_text = message.get("text", f"R-SMM payment request #{deposit_id}")
            telegram_api_call(token, "editMessageText", {
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "text": message_text + f"\n\nPayment {outcome} from Telegram.",
                "reply_markup": {"inline_keyboard": []},
            })
        else:
            already_reviewed = status == 409 and "already been reviewed" in result.get("error", "")
            telegram_api_call(token, "answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": result.get("error", "Could not update this payment request.")[:190],
                "show_alert": True,
            })
            if already_reviewed or status == 404:
                telegram_api_call(token, "editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": message.get("message_id"),
                    "reply_markup": {"inline_keyboard": []},
                })
        self.respond(200, {"ok": True})

    def logout(self):
        cookies = self.headers.get("Cookie", "")
        token = next((part.split("=", 1)[1] for part in cookies.split("; ")
                      if part.startswith("session=")), None)
        if token:
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?",
                           (hashlib.sha256(token.encode()).hexdigest(),))
        secure = "; Secure" if os.environ.get("VERCEL") else ""
        self.respond(200, {"ok": True}, f"session=; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age=0")


if __name__ == "__main__":
    ensure_db()
    print("R-SMM is running at http://127.0.0.1:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), App).serve_forever()
