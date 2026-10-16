# SPDX-License-Identifier: AGPL-3.0-or-later
# Heroku module: one polling task, optional aiohttp API, SQLite.
import asyncio
import contextlib
import hashlib
import html
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sqlite3
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
from aiohttp import web

try:
    from .. import loader, utils
except ImportError:  # Import the store and API from ordinary Python tests.
    loader = utils = None

ADMIN_ID = 964554907
PRODUCT = "peroxide"


class LicenseError(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expiry_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=3))).strftime(
        "%d.%m.%Y %H:%M МСК"
    )


class LicenseStore:
    """Every mutation is one SQLite transaction, including key redemption."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '', banned INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS licenses(
                    key_hash TEXT PRIMARY KEY, seconds INTEGER NOT NULL,
                    created INTEGER NOT NULL, redeemed_by INTEGER, redeemed_at INTEGER);
                CREATE TABLE IF NOT EXISTS subscriptions(
                    user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(
                    device_hash TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
                    expires INTEGER NOT NULL, user_id INTEGER, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, actor INTEGER NOT NULL,
                    action TEXT NOT NULL, target INTEGER);
                CREATE TABLE IF NOT EXISTS bot_offsets(
                    bot_id INTEGER PRIMARY KEY, next_offset INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS login_keys(
                    user_id INTEGER PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
                    created INTEGER NOT NULL);
            """)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def now(self):
        return int(self.clock())

    def bot_offset(self, bot_id, next_offset=None):
        with self.connection() as db:
            if next_offset is not None:
                db.execute("INSERT INTO bot_offsets VALUES(?,?) ON CONFLICT(bot_id) DO UPDATE "
                           "SET next_offset=MAX(next_offset,excluded.next_offset)", (bot_id, next_offset))
            row = db.execute("SELECT next_offset FROM bot_offsets WHERE bot_id=?", (bot_id,)).fetchone()
            return row[0] if row else 0

    def user(self, user_id, username="", name=""):
        with self.connection() as db:
            db.execute("INSERT INTO users(id,username,name) VALUES(?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name",
                       (user_id, username[:64], name[:128]))

    def _profile(self, db, user_id):
        row = db.execute("SELECT u.*,COALESCE(s.expires,0) AS expires FROM users u "
                         "LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.id=?", (user_id,)).fetchone()
        if row is None:
            raise LicenseError("Профиль не найден")
        return dict(row)

    def profile(self, user_id):
        with self.connection() as db:
            p = self._profile(db, user_id)
        p["active"] = not p["banned"] and p["expires"] > self.now()
        p["server_time"] = self.now()
        return p

    def _active(self, db, user_id):
        p = self._profile(db, user_id)
        if p["banned"] or p["expires"] <= self.now():
            raise LicenseError("Нет действующей подписки")
        return p

    def generate(self, actor, days, count=1):
        if actor != ADMIN_ID:
            raise LicenseError("Нет доступа")
        if not 1 <= days <= 3650 or not 1 <= count <= 20:
            raise LicenseError("Срок: 1–3650 дней; количество: 1–20")
        keys = ["PX-" + secrets.token_hex(16).upper() for _ in range(count)]
        with self.connection() as db:
            db.executemany("INSERT INTO licenses(key_hash,seconds,created) VALUES(?,?,?)",
                           [(digest(k), days * 86400, self.now()) for k in keys])
            db.execute("INSERT INTO audit(at,actor,action) VALUES(?,?,?)",
                       (self.now(), actor, "generate:" + str(count)))
        return keys

    def redeem(self, user_id, key):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            p = self._profile(db, user_id)
            if p["banned"]:
                raise LicenseError("Доступ заблокирован")
            row = db.execute("SELECT * FROM licenses WHERE key_hash=?", (digest(key.strip().upper()),)).fetchone()
            if not row or row["redeemed_by"] is not None:
                raise LicenseError("Ключ неверен или уже активирован")
            expires = max(self.now(), p["expires"]) + row["seconds"]
            db.execute("INSERT INTO subscriptions VALUES(?,?) ON CONFLICT(user_id) "
                       "DO UPDATE SET expires=excluded.expires", (user_id, expires))
            db.execute("UPDATE licenses SET redeemed_by=?,redeemed_at=? WHERE key_hash=?",
                       (user_id, self.now(), row["key_hash"]))
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)",
                       (self.now(), user_id, "redeem", user_id))
            return expires

    def moderate(self, actor, user_id, action, days=0):
        if actor != ADMIN_ID:
            raise LicenseError("Нет доступа")
        if not isinstance(user_id, int) or not 1 <= user_id < 2 ** 63:
            raise LicenseError("Неверный Telegram ID")
        if action not in ("ban", "unban", "revoke", "grant"):
            raise LicenseError("Неизвестное действие")
        if action == "grant" and not 1 <= days <= 3650:
            raise LicenseError("Срок: 1–3650 дней")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (user_id,))
            p = self._profile(db, user_id)
            if action in ("ban", "unban"):
                db.execute("UPDATE users SET banned=? WHERE id=?", (int(action == "ban"), user_id))
            else:
                expires = self.now() if action == "revoke" else max(self.now(), p["expires"]) + days * 86400
                db.execute("INSERT INTO subscriptions VALUES(?,?) ON CONFLICT(user_id) "
                           "DO UPDATE SET expires=excluded.expires", (user_id, expires))
            if action in ("ban", "revoke"):
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM devices WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM login_keys WHERE user_id=?", (user_id,))
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)",
                       (self.now(), actor, action, user_id))

    def issue_login_key(self, user_id):
        """Called only for the authenticated Telegram sender, never by the API."""
        key = "PXL-" + secrets.token_hex(24).upper()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._active(db, user_id)
            db.execute("INSERT INTO login_keys VALUES(?,?,?) ON CONFLICT(user_id) "
                       "DO UPDATE SET key_hash=excluded.key_hash,created=excluded.created",
                       (user_id, digest(key), self.now()))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM devices WHERE user_id=?", (user_id,))
            db.execute("INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)",
                       (self.now(), user_id, "rotate_login_key", user_id))
        return key

    def key_login(self, key):
        key = key.strip().upper()
        if len(key) != 52 or not key.startswith("PXL-") or any(c not in "0123456789ABCDEF" for c in key[4:]):
            raise LicenseError("Ключ входа недействителен")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT user_id FROM login_keys WHERE key_hash=?", (digest(key),)).fetchone()
            if row is None:
                raise LicenseError("Ключ входа недействителен")
            p = self._active(db, row["user_id"])
            # One active session per key login; replay replaces the old session.
            db.execute("DELETE FROM sessions WHERE user_id=? OR expires<=?", (p["id"], self.now()))
            token = secrets.token_urlsafe(32)
            expires = min(self.now() + 86400, p["expires"])
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), p["id"], expires))
        return {"access_token": token, "token_expires_at": expires}

    def end_session(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def start_device(self):
        code, device = secrets.token_hex(6).upper(), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM devices WHERE expires<=? OR used=1", (self.now(),))
            db.execute("DELETE FROM sessions WHERE expires<=?", (self.now(),))
            if db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= 1000:
                raise LicenseError("Слишком много запросов входа")
            db.execute("INSERT INTO devices(device_hash,code_hash,expires) VALUES(?,?,?)",
                       (digest(device), digest(code), self.now() + 300))
        return {"device_code": device, "user_code": code, "expires_in": 300, "interval": 5}

    def approve(self, user_id, code):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._active(db, user_id)
            result = db.execute("UPDATE devices SET user_id=? WHERE code_hash=? AND expires>? "
                                "AND user_id IS NULL AND used=0", (user_id, digest(code.upper()), self.now()))
            if result.rowcount != 1:
                raise LicenseError("Код неверен, уже подтверждён или истёк")

    def poll(self, device):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM devices WHERE device_hash=?", (digest(device),)).fetchone()
            if not row or row["expires"] <= self.now() or row["used"]:
                raise LicenseError("Запрос входа истёк или уже использован")
            if row["user_id"] is None:
                return {"status": "pending"}
            p = self._active(db, row["user_id"])
            token = secrets.token_urlsafe(32)
            expires = min(self.now() + 86400, p["expires"])
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), p["id"], expires))
            db.execute("UPDATE devices SET used=1 WHERE device_hash=?", (row["device_hash"],))
            return {"status": "approved", "access_token": token, "token_expires_at": expires}

    def authenticate(self, token):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?",
                             (digest(token), self.now())).fetchone()
            if row is None:
                raise LicenseError("Сессия недействительна")
            p = self._active(db, row["user_id"])
        return {"telegram_id": p["id"], "username": p["username"], "display_name": p["name"],
                "product": PRODUCT, "expires_at": p["expires"], "server_time": self.now(), "active": True}

    def logout(self, user_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM devices WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM login_keys WHERE user_id=?", (user_id,))

    def stats(self, actor):
        if actor != ADMIN_ID:
            raise LicenseError("Нет доступа")
        with self.connection() as db:
            return {"users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                    "unused_keys": db.execute("SELECT COUNT(*) FROM licenses WHERE redeemed_by IS NULL").fetchone()[0],
                    "active": db.execute("SELECT COUNT(*) FROM subscriptions s JOIN users u ON u.id=s.user_id "
                                         "WHERE s.expires>? AND u.banned=0", (self.now(),)).fetchone()[0]}


class LicenseService:
    def __init__(self, store, token, bot_name="", session=None, bot_id=0, trusted_proxy=""):
        self.store, self.token, self.bot_name = store, token, bot_name
        self.session = session
        self.bot_id, self.trusted_proxy = bot_id, trusted_proxy
        self.limits = OrderedDict()
        self.avatar_cache = OrderedDict()
        self.status = "не запущен"

    def allow(self, identity, category, limit):
        now = time.monotonic()
        key = (identity, category)
        q = self.limits.pop(key, deque())
        while q and q[0] < now - 60:
            q.popleft()
        accepted = len(q) < limit
        if accepted:
            q.append(now)
        self.limits[key] = q
        while len(self.limits) > 4096:
            self.limits.popitem(last=False)
        return accepted

    async def telegram(self, method, payload=None, timeout=45):
        # Never log request URLs: they contain the bot credential.
        async with self.session.post("https://api.telegram.org/bot" + self.token + "/" + method,
                                     json=payload or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            result = await response.json()
            if response.status != 200 or not result.get("ok"):
                raise LicenseError("Telegram API: код " + str(result.get("error_code", response.status)))
            return result["result"]

    async def reply(self, user_id, text, reply_markup=None):
        payload = {"chat_id": user_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self.telegram("sendMessage", payload)

    async def edit_or_reply(self, chat_id, message_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return await self.telegram("editMessageText", payload)
        except Exception:
            return await self.reply(chat_id, text, reply_markup)

    def _main_markup(self, uid):
        keyboard = [
            [
                {"text": "👤 Мой профиль", "callback_data": "menu_profile"},
                {"text": "🔑 Ключ входа", "callback_data": "menu_key"},
            ],
            [
                {"text": "⚡ Активировать ключ", "callback_data": "menu_activate"},
                {"text": "🚪 Выйти из сессий", "callback_data": "menu_logout"},
            ],
        ]
        if uid == ADMIN_ID:
            keyboard.append([
                {"text": "📊 Статистика", "callback_data": "menu_stats"},
                {"text": "➕ Создать ключ", "callback_data": "menu_gen"},
            ])
        return {"inline_keyboard": keyboard}

    def _profile_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "🔑 Получить ключ входа", "callback_data": "menu_key"},
                    {"text": "⚡ Активировать ключ", "callback_data": "menu_activate"},
                ],
                [
                    {"text": "🔄 Обновить", "callback_data": "menu_profile"},
                    {"text": "◀️ В меню", "callback_data": "menu_main"},
                ],
            ]
        }

    def _stats_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "➕ Создать ключ", "callback_data": "menu_gen"}],
                [
                    {"text": "🔄 Обновить", "callback_data": "menu_stats"},
                    {"text": "◀️ В меню", "callback_data": "menu_main"},
                ],
            ]
        }

    def _gen_menu_markup(self):
        return {
            "inline_keyboard": [
                [
                    {"text": "1 день", "callback_data": "gen_1"},
                    {"text": "7 дней", "callback_data": "gen_7"},
                    {"text": "14 дней", "callback_data": "gen_14"},
                ],
                [
                    {"text": "30 дней (1 мес)", "callback_data": "gen_30"},
                    {"text": "90 дней (3 мес)", "callback_data": "gen_90"},
                ],
                [
                    {"text": "365 дней (1 год)", "callback_data": "gen_365"},
                ],
                [
                    {"text": "◀️ В меню", "callback_data": "menu_main"},
                ],
            ]
        }

    def _key_result_markup(self, days):
        return {
            "inline_keyboard": [
                [{"text": f"➕ Ещё ключ на {days} дн.", "callback_data": f"gen_{days}"}],
                [
                    {"text": "◀️ К срокам", "callback_data": "menu_gen"},
                    {"text": "🏠 В главное меню", "callback_data": "menu_main"},
                ],
            ]
        }

    def _confirm_login_markup(self, code):
        return {
            "inline_keyboard": [
                [{"text": "✅ Подтвердить вход", "callback_data": f"conf_{code}"}],
                [{"text": "❌ Отклонить", "callback_data": "menu_main"}],
            ]
        }

    def _back_main_markup(self):
        return {
            "inline_keyboard": [
                [{"text": "◀️ В главное меню", "callback_data": "menu_main"}]
            ]
        }

    def _render_main_text(self, uid):
        text = (
            "<b>⚡ Панель управления Peroxide</b>\n\n"
            "Выберите нужное действие кнопками ниже или используйте команды:\n\n"
            "▫️ <b>/profile</b> — информация о подписке\n"
            "▫️ <b>/activate</b> <code>КЛЮЧ</code> — активация ключа\n"
            "▫️ <b>/key</b> — ключ авторизации в лоадере\n"
            "▫️ <b>/confirm</b> <code>КОД</code> — подтвердить вход с ПК\n"
            "▫️ <b>/logout</b> — завершить все сессии\n"
        )
        if uid == ADMIN_ID:
            text += (
                "\n👑 <b>Администрирование:</b>\n"
                "▫️ <code>/gen ДНИ [КОЛ-ВО]</code> — сгенерировать ключи\n"
                "▫️ <code>/grant ID ДНИ</code> — выдать дни пользователю\n"
                "▫️ <code>/ban ID</code> / <code>/unban ID</code> — бан / разбан\n"
                "▫️ <code>/revoke ID</code> — сбросить подписку\n"
                "▫️ <code>/stats</code> — статистика пользователей\n"
            )
        return text

    def _render_profile_text(self, p, uid):
        status = "⛔ Заблокирован" if p["banned"] else ("🟢 Активна" if p["active"] else "🔴 Не активна")
        expires = expiry_text(p["expires"]) if p["expires"] else "подписка не активирована"
        name = html.escape(p.get("name") or "—")
        username = ("@" + html.escape(p["username"])) if p.get("username") else "—"
        return (
            "<b>👤 Профиль пользователя</b>\n\n"
            f"▫️ <b>Имя:</b> {name}\n"
            f"▫️ <b>Username:</b> {username}\n"
            f"▫️ <b>Telegram ID:</b> <code>{uid}</code>\n"
            f"▫️ <b>Статус:</b> {status}\n"
            f"▫️ <b>Подписка до:</b> <code>{expires}</code>\n"
        )

    def _render_stats_text(self, stats):
        return (
            "<b>📊 Статистика Peroxide</b>\n\n"
            f"👥 <b>Всего пользователей:</b> <code>{stats['users']}</code>\n"
            f"⚡ <b>Активных подписок:</b> <code>{stats['active']}</code>\n"
            f"🔑 <b>Неактивированных ключей:</b> <code>{stats['unused_keys']}</code>\n"
        )

    async def dispatch_callback(self, cb):
        cb_id = cb.get("id")
        sender = cb.get("from", {})
        uid = sender.get("id")
        if not isinstance(uid, int) or sender.get("is_bot"):
            return
        if not self.allow(str(uid), "bot", 30):
            with contextlib.suppress(Exception):
                await self.telegram("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Слишком много запросов!"})
            return

        data = cb.get("data", "")
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id", uid)
        msg_id = msg.get("message_id")

        with contextlib.suppress(Exception):
            await self.telegram("answerCallbackQuery", {"callback_query_id": cb_id})

        await asyncio.to_thread(self.store.user, uid, sender.get("username", ""), sender.get("first_name", ""))

        try:
            if data == "menu_main":
                await self.edit_or_reply(chat_id, msg_id, self._render_main_text(uid), self._main_markup(uid))
            elif data == "menu_profile":
                p = await asyncio.to_thread(self.store.profile, uid)
                await self.edit_or_reply(chat_id, msg_id, self._render_profile_text(p, uid), self._profile_markup())
            elif data == "menu_key":
                key = await asyncio.to_thread(self.store.issue_login_key, uid)
                text = (
                    "<b>🔑 Ваш персональный ключ входа:</b>\n\n"
                    f"<code>{key}</code>\n\n"
                    "<i>👆 Нажмите на ключ, чтобы скопировать его.\n"
                    "Вставьте его в поле ключа в лоадере. Старые сессии завершены.</i>"
                )
                await self.edit_or_reply(chat_id, msg_id, text, self._back_main_markup())
            elif data == "menu_activate":
                text = (
                    "<b>⚡ Активация лицензионного ключа</b>\n\n"
                    "Просто <b>отправьте ключ сообщением в этот чат</b> (или используйте команду <code>/activate КЛЮЧ</code>).\n\n"
                    "Формат: <code>PX-XXXX-XXXX...</code>"
                )
                await self.edit_or_reply(chat_id, msg_id, text, self._back_main_markup())
            elif data == "menu_logout":
                await asyncio.to_thread(self.store.logout, uid)
                text = "<b>🚪 Все активные сессии и ключ входа отозваны.</b>\nДля нового входа получите /key."
                await self.edit_or_reply(chat_id, msg_id, text, self._back_main_markup())
            elif data == "menu_stats" and uid == ADMIN_ID:
                stats = await asyncio.to_thread(self.store.stats, uid)
                await self.edit_or_reply(chat_id, msg_id, self._render_stats_text(stats), self._stats_markup())
            elif data == "menu_gen" and uid == ADMIN_ID:
                text = "<b>➕ Создание лицензионного ключа</b>\n\nВыберите срок действия ключа:"
                await self.edit_or_reply(chat_id, msg_id, text, self._gen_menu_markup())
            elif data.startswith("gen_") and uid == ADMIN_ID:
                days = int(data[4:])
                keys = await asyncio.to_thread(self.store.generate, uid, days, 1)
                text = (
                    f"<b>✅ Сгенерирован ключ на {days} дн.:</b>\n\n"
                    f"<code>{keys[0]}</code>\n\n"
                    "<i>👆 Нажмите на ключ, чтобы скопировать. Время пойдёт после активации.</i>"
                )
                await self.edit_or_reply(chat_id, msg_id, text, self._key_result_markup(days))
            elif data.startswith("conf_"):
                code = data[5:]
                await asyncio.to_thread(self.store.approve, uid, code)
                text = "<b>✅ Вход успешно подтверждён!</b>\n\nМожете вернуться в приложение лоадера."
                await self.edit_or_reply(chat_id, msg_id, text, self._back_main_markup())
        except Exception as error:
            err_msg = str(error) if isinstance(error, LicenseError) else "Произошла ошибка при выполнении"
            await self.reply(chat_id, f"<b>⚠️ Ошибка:</b> {html.escape(err_msg)}")

    async def dispatch(self, message):
        if message.get("chat", {}).get("type") != "private":
            return
        sender = message.get("from", {})
        uid = sender.get("id")
        if not isinstance(uid, int) or sender.get("is_bot"):
            return
        if not self.allow(str(uid), "bot", 30):
            return
        await asyncio.to_thread(self.store.user, uid, sender.get("username", ""), sender.get("first_name", ""))
        text_raw = message.get("text", "").strip()
        parts = text_raw.split()
        if not parts:
            return
        command = parts[0].split("@")[0].lower()
        args = parts[1:]

        # Deep link confirmation: /start login_XXXXXX
        if command == "/start" and args and args[0].startswith("login_"):
            code = args[0][6:]
            text = (
                "<b>🔐 Запрос авторизации лоадера</b>\n\n"
                f"Код подтверждения: <code>{code}</code>\n\n"
                "Сверьте код с окном на вашем ПК и нажмите кнопку ниже:"
            )
            await self.reply(uid, text, self._confirm_login_markup(code))
            return

        # Direct key activation if user pastes "PX-..."
        if text_raw.upper().startswith("PX-") and len(parts) == 1:
            try:
                expires = await asyncio.to_thread(self.store.redeem, uid, text_raw)
                text = (
                    "<b>🎉 Подписка успешно активирована!</b>\n\n"
                    f"⏳ Срок действия: до <b>{expiry_text(expires)}</b>"
                )
                await self.reply(uid, text, self._profile_markup())
            except Exception as e:
                err = str(e) if isinstance(e, LicenseError) else "Неверный ключ"
                await self.reply(uid, f"<b>⚠️ Ошибка активации:</b> {html.escape(err)}")
            return

        try:
            if command in ("/start", "/help", "/menu"):
                await self.reply(uid, self._render_main_text(uid), self._main_markup(uid))
            elif command == "/profile":
                p = await asyncio.to_thread(self.store.profile, uid)
                await self.reply(uid, self._render_profile_text(p, uid), self._profile_markup())
            elif command == "/activate" and len(args) == 1:
                expires = await asyncio.to_thread(self.store.redeem, uid, args[0])
                text = (
                    "<b>🎉 Подписка успешно активирована!</b>\n\n"
                    f"⏳ Срок действия: до <b>{expiry_text(expires)}</b>"
                )
                await self.reply(uid, text, self._profile_markup())
            elif command == "/confirm" and len(args) == 1:
                await asyncio.to_thread(self.store.approve, uid, args[0])
                await self.reply(uid, "<b>✅ Вход подтверждён!</b> Вернитесь в лоадер.")
            elif command == "/key" and not args:
                key = await asyncio.to_thread(self.store.issue_login_key, uid)
                text = (
                    "<b>🔑 Ваш персональный ключ входа:</b>\n\n"
                    f"<code>{key}</code>\n\n"
                    "<i>👆 Нажмите на ключ, чтобы скопировать.\n"
                    "Вставьте его в окно лоадера. Старые сессии завершены.</i>"
                )
                await self.reply(uid, text, self._back_main_markup())
            elif command == "/logout":
                await asyncio.to_thread(self.store.logout, uid)
                await self.reply(uid, "<b>🚪 Все сессии и ключ входа отозваны.</b>\nДля нового входа получите /key.", self._back_main_markup())
            elif command == "/gen" and uid == ADMIN_ID and 1 <= len(args) <= 2:
                days = int(args[0])
                count = int(args[1]) if len(args) == 2 else 1
                keys = await asyncio.to_thread(self.store.generate, uid, days, count)
                text = (
                    f"<b>✅ Сгенерировано ключей: {len(keys)} (на {days} дн.)</b>\n\n"
                    + "\n".join(f"▫️ <code>{k}</code>" for k in keys)
                    + "\n\n<i>👆 Нажмите на ключ, чтобы скопировать.</i>"
                )
                await self.reply(uid, text, self._key_result_markup(days) if count == 1 else self._back_main_markup())
            elif command in ("/ban", "/unban", "/revoke", "/grant") and uid == ADMIN_ID and args:
                if len(args) != (2 if command == "/grant" else 1):
                    raise LicenseError("Проверьте количество аргументов")
                target = int(args[0])
                days = int(args[1]) if command == "/grant" else 0
                await asyncio.to_thread(self.store.moderate, uid, target, command[1:], days)
                action_names = {
                    "ban": "⛔ Пользователь заблокирован",
                    "unban": "🟢 Пользователь разблокирован",
                    "revoke": "⚠️ Подписка аннулирована",
                    "grant": f"⚡ Выдано {days} дн. подписки",
                }
                text = f"<b>{action_names.get(command[1:], 'Готово')}:</b> <code>{target}</code>"
                await self.reply(uid, text, self._back_main_markup())
            elif command == "/stats" and uid == ADMIN_ID:
                stats = await asyncio.to_thread(self.store.stats, uid)
                await self.reply(uid, self._render_stats_text(stats), self._stats_markup())
            else:
                await self.reply(uid, "<b>⚠️ Неизвестная команда.</b> Используйте кнопки меню ниже:", self._main_markup(uid))
        except (LicenseError, ValueError) as error:
            err = str(error) if isinstance(error, LicenseError) else "ID, срок и количество должны быть числами"
            await self.reply(uid, f"<b>⚠️ Ошибка:</b> {html.escape(err)}")

    async def poll_bot(self):
        offset, backoff = await asyncio.to_thread(self.store.bot_offset, self.bot_id), 2
        while True:
            try:
                updates = await self.telegram("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
                self.status = "бот подключён"
                for update in updates:
                    offset = update["update_id"] + 1
                    # At-most-once command execution: a restart must not repeat
                    # /grant. If interrupted before execution, resend the command.
                    await asyncio.to_thread(self.store.bot_offset, self.bot_id, offset)
                    if "message" in update:
                        await self.dispatch(update["message"])
                    elif "callback_query" in update:
                        await self.dispatch_callback(update["callback_query"])
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = "ошибка подключения/обработки; повтор через " + str(backoff) + " с"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def application(self):
        @web.middleware
        async def guard(request, handler):
            if request.content_length is not None and request.content_length > 4096:
                return web.json_response({"error": "request_too_large"}, status=413,
                                         headers={"Cache-Control": "no-store"})
            category = ("start" if request.path.endswith("/start") else
                        "login" if request.path == "/v1/key/login" else "api")
            peer = request.remote or "unknown"
            if self.trusted_proxy and peer == self.trusted_proxy:
                with contextlib.suppress(ValueError):
                    peer = str(ipaddress.ip_address(request.headers.get("X-Real-IP", "")))
            if not self.allow(peer, category, 5 if category in ("start", "login") else 60):
                return web.json_response({"error": "rate_limited"}, status=429,
                                         headers={"Cache-Control": "no-store"})
            try:
                response = await handler(request)
            except LicenseError:
                response = web.json_response({"error": "access_denied_or_expired"}, status=403)
            except (json.JSONDecodeError, ValueError, TypeError):
                response = web.json_response({"error": "invalid_request"}, status=400)
            except web.HTTPException as error:
                response = error
            except asyncio.CancelledError:
                raise
            except Exception:
                # aiohttp errors may embed Telegram URLs with credentials.
                # Do not send them to responses or framework access/error logs.
                response = web.json_response({"error": "service_unavailable"}, status=503)
            response.headers["Cache-Control"] = "no-store"
            return response

        app = web.Application(client_max_size=4096, middlewares=[guard])

        async def start(request):
            result = await asyncio.to_thread(self.store.start_device)
            result["verification_uri"] = "https://t.me/" + self.bot_name + "?start=login_" + result["user_code"]
            return web.json_response(result)

        async def poll(request):
            body = await request.json()
            device = body.get("device_code") if isinstance(body, dict) else None
            if not isinstance(device, str) or not 20 <= len(device) <= 128:
                raise ValueError()
            return web.json_response(await asyncio.to_thread(self.store.poll, device))

        async def identity(request):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not 20 <= len(auth[7:]) <= 128:
                raise LicenseError()
            return await asyncio.to_thread(self.store.authenticate, auth[7:])

        async def me(request):
            return web.json_response(await identity(request))

        async def key_login(request):
            body = await request.json()
            key = body.get("key") if isinstance(body, dict) else None
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise ValueError()
            result = await asyncio.to_thread(self.store.key_login, key)
            return web.json_response(result)

        async def logout(request):
            await identity(request)
            await asyncio.to_thread(self.store.end_session, request.headers["Authorization"][7:])
            return web.json_response({"status": "signed_out"})

        async def avatar(request):
            p = await identity(request)
            uid = p["telegram_id"]
            cached = self.avatar_cache.get(uid)
            if cached and time.monotonic() - cached[0] < 900:
                return web.Response(body=cached[1], content_type="image/jpeg")
            photos = await self.telegram("getUserProfilePhotos", {"user_id": uid, "limit": 1})
            if not photos.get("photos"):
                raise web.HTTPNotFound()
            variants = photos["photos"][0]
            photo = min(variants, key=lambda item: abs(item["width"] - 160))
            info = await self.telegram("getFile", {"file_id": photo["file_id"]})
            path = info.get("file_path", "")
            if not path or info.get("file_size", 0) > 262144:
                raise web.HTTPNotFound()
            async with self.session.get("https://api.telegram.org/file/bot" + self.token + "/" + path,
                                        timeout=aiohttp.ClientTimeout(total=10)) as result:
                if result.status != 200:
                    raise web.HTTPNotFound()
                data = await result.content.readexactly(int(info["file_size"])) if info.get("file_size") else await result.content.read(262145)
            if len(data) > 262144 or not data.startswith(b"\xff\xd8"):
                raise web.HTTPNotFound()
            self.avatar_cache.pop(uid, None)
            self.avatar_cache[uid] = (time.monotonic(), data)
            while len(self.avatar_cache) > 16:
                self.avatar_cache.popitem(last=False)
            return web.Response(body=data, content_type="image/jpeg")

        app.router.add_post("/v1/device/start", start)
        app.router.add_post("/v1/key/login", key_login)
        app.router.add_post("/v1/logout", logout)
        app.router.add_post("/v1/device/poll", poll)
        app.router.add_get("/v1/me", me)
        app.router.add_get("/v1/avatar", avatar)
        return app


if loader is not None:
    @loader.tds
    class PeroxideLicenseMod(loader.Module):
        """Ключи, Telegram-профиль и API лицензий Peroxide."""

        strings = {"name": "PeroxideLicense"}

        def __init__(self):
            self.config = loader.ModuleConfig(
                loader.ConfigValue("BOT_TOKEN", "8848119941:AAFz5utHNxdRjKl-KYAz-agI-esvcMZaHP0", "Новый токен отдельного бота", validator=loader.validators.Hidden()),
                loader.ConfigValue("DB_PATH", "", "Путь к SQLite на постоянном диске; пусто = папка Heroku"),
                loader.ConfigValue("API_ENABLED", False, "Запустить HTTP API", validator=loader.validators.Boolean()),
                loader.ConfigValue("BIND", "127.0.0.1", "Адрес HTTP: localhost для HTTPS reverse proxy"),
                loader.ConfigValue("TRUSTED_PROXY_IP", "", "IP доверенного HTTPS-прокси; он должен перезаписывать X-Real-IP"),
                loader.ConfigValue("PORT", 8787, "Порт HTTP API", validator=loader.validators.Integer(minimum=1024, maximum=65535)),
            )
            self._task = self._runner = self._session = self.service = self.store = None

        async def client_ready(self):
            if self.tg_id != ADMIN_ID:
                raise RuntimeError("Модуль настроен только для владельца 964554907")
            self._path = Path(self.config["DB_PATH"] or str(Path(utils.get_base_dir()) / "peroxide-data" / "licenses.sqlite3"))
            self.store = await asyncio.to_thread(LicenseStore, self._path)
            token = (
                os.environ.get("PEROXIDE_BOT_TOKEN")
                or self.config["BOT_TOKEN"]
                or "8848119941:AAFz5utHNxdRjKl-KYAz-agI-esvcMZaHP0"
            )
            if not token:
                return  # Diagnostics remain available before credentials are entered.
            self._session = aiohttp.ClientSession()
            self.service = LicenseService(self.store, token, session=self._session)
            self.service.trusted_proxy = self.config["TRUSTED_PROXY_IP"]
            try:
                me = await self.service.telegram("getMe")
                self.service.bot_name = me["username"]
                self.service.bot_id = me["id"]
                webhook = await self.service.telegram("getWebhookInfo")
                if webhook.get("url"):
                    raise RuntimeError("У бота уже установлен webhook; нужен отдельный свободный бот")
                if self.config["API_ENABLED"]:
                    self._runner = web.AppRunner(self.service.application(), access_log=None)
                    await self._runner.setup()
                    await web.TCPSite(self._runner, self.config["BIND"], self.config["PORT"]).start()
                self._task = asyncio.create_task(self.service.poll_bot())
            except Exception:
                await self.on_unload()
                # Suppress exception chains which might contain the token URL.
                raise RuntimeError("Не удалось запустить PeroxideLicense: проверь токен, webhook и порт") from None

        async def on_unload(self):
            if self._task:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            if self._runner:
                await self._runner.cleanup()
                self._runner = None
            if self._session:
                await self._session.close()
                self._session = None

        @loader.command()
        async def licensehost(self, message):
            """Проверить место, память и локальный API. Не раскрывает токены."""
            if message.sender_id != ADMIN_ID:
                return
            disk = shutil.disk_usage(self._path.parent)
            memory = "не определена"
            with contextlib.suppress(ImportError):
                import psutil
                memory = str(round(psutil.Process().memory_info().rss / 1048576, 1)) + " MiB (весь юзербот)"
            api = "выключен"
            if self._runner:
                api = html.escape(self.config["BIND"]) + ":" + str(self.config["PORT"])
            else:
                try:
                    with socket.socket() as probe:
                        probe.bind((self.config["BIND"], self.config["PORT"]))
                    api += "; локальный порт доступен"
                except OSError:
                    api += "; локальный порт занят или запрещён"
            text = ("<b>PeroxideLicense</b>\nSQLite: создана, запись доступна\nСвободно: " +
                    str(round(disk.free / 1073741824, 2)) + " GiB\nПамять: " + memory +
                    "\nБаза: <code>" + html.escape(str(self._path)) + "</code>" +
                    "\nAPI: " + api + "\nБот: " + (self.service.status if self.service else "токен не настроен") +
                    "\nПостоянство диска и внешний HTTPS этим тестом не подтверждаются.")
            await utils.answer(message, text)


async def _standalone_main():
    token = os.environ.get("BOT_TOKEN") or os.environ.get("PEROXIDE_BOT_TOKEN") or "8848119941:AAFz5utHNxdRjKl-KYAz-agI-esvcMZaHP0"
    port = int(os.environ.get("PORT", "8787"))
    bind = os.environ.get("BIND", "0.0.0.0")
    db_path = os.environ.get("DB_PATH", "peroxide-data/licenses.sqlite3")

    print(f"Starting Peroxide License Server on {bind}:{port}...")
    store = LicenseStore(Path(db_path))
    async with aiohttp.ClientSession() as session:
        service = LicenseService(store, token, session=session)
        service.trusted_proxy = os.environ.get("TRUSTED_PROXY_IP", "")
        me = await service.telegram("getMe")
        service.bot_name = me.get("username", "")
        service.bot_id = me.get("id", 0)
        print(f"Telegram Bot connected: @{service.bot_name} (ID: {service.bot_id})")

        runner = web.AppRunner(service.application(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, bind, port)
        await site.start()
        print(f"HTTP API listening on http://{bind}:{port}")

        await service.poll_bot()


if __name__ == "__main__":
    try:
        asyncio.run(_standalone_main())
    except (KeyboardInterrupt, SystemExit):
        pass

