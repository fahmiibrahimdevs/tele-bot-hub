from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import shutil
import aiosqlite

JAKARTA_TZ = ZoneInfo("Asia/Jakarta")

def now_jakarta() -> datetime:
    """Mengembalikan objek datetime saat ini dalam zona waktu Indonesia Jakarta (WIB)."""
    return datetime.now(JAKARTA_TZ)

def now_jakarta_str() -> str:
    """Mengembalikan string tanggal & jam saat ini format 'YYYY-MM-DD HH:MM:SS' (WIB)."""
    return datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d %H:%M:%S")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bots.db")
STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")


def hash_password(password: str) -> str:
    salt = "tele_hub_salt_2026"
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    async with get_db() as db:
        # 1. Users table (Admin portal)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Bots table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                bot_type TEXT NOT NULL, -- 'img2pdf', 'mp3', 'mp4'
                is_active INTEGER DEFAULT 1,
                config TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. Logs table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                bot_username TEXT,
                user_telegram_id INTEGER,
                user_name TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL, -- 'success', 'error'
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrasi kolom bila tabel lama belum punya kolom user_telegram_id
        cursor = await db.execute("PRAGMA table_info(activity_logs)")
        cols = [r["name"] for r in await cursor.fetchall()]
        if "user_telegram_id" not in cols:
            await db.execute("ALTER TABLE activity_logs ADD COLUMN user_telegram_id INTEGER")
        if "user_name" not in cols:
            await db.execute("ALTER TABLE activity_logs ADD COLUMN user_name TEXT")

        # 4. Telegram Users Directory & Quota table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                is_unlimited INTEGER DEFAULT 0, -- 1: bypass daily quota
                is_banned INTEGER DEFAULT 0,    -- 1: diblokir
                total_ops INTEGER DEFAULT 0,
                vip_started_at TIMESTAMP,       -- Waktu mulai/pembelian VIP
                vip_until TIMESTAMP,            -- Tanggal/waktu kedaluwarsa VIP
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(bot_id, telegram_id)
            )
        """)

        # Migrasi kolom bila tabel lama belum punya kolom VIP
        cursor_tu = await db.execute("PRAGMA table_info(telegram_users)")
        tu_cols = [r["name"] for r in await cursor_tu.fetchall()]
        if "vip_until" not in tu_cols:
            await db.execute("ALTER TABLE telegram_users ADD COLUMN vip_until TIMESTAMP")
        if "vip_started_at" not in tu_cols:
            await db.execute("ALTER TABLE telegram_users ADD COLUMN vip_started_at TIMESTAMP")

        # Sesuaikan timestamp historis yang masih tersimpan dalam format UTC (kurang dari jam 10 pagi hari ini)
        try:
            await db.execute("""
                UPDATE activity_logs 
                SET created_at = datetime(created_at, '+7 hours') 
                WHERE created_at < '2026-09-07 10:00:00'
            """)
            await db.execute("""
                UPDATE telegram_users 
                SET last_active = datetime(last_active, '+7 hours'), 
                    created_at = datetime(created_at, '+7 hours') 
                WHERE last_active < '2026-09-07 10:00:00'
            """)
            await db.commit()
        except Exception:
            pass

        # Penyelarasan awal status VIP & Ban global untuk setiap telegram_id unik
        try:
            cursor_users = await db.execute("SELECT DISTINCT telegram_id FROM telegram_users")
            all_tids = [r["telegram_id"] for r in await cursor_users.fetchall()]
            for tid in all_tids:
                c_vip = await db.execute(
                    """SELECT is_unlimited, vip_started_at, vip_until, is_banned 
                       FROM telegram_users 
                       WHERE telegram_id = ? 
                       ORDER BY is_unlimited DESC, is_banned DESC LIMIT 1""",
                    (tid,)
                )
                best_row = await c_vip.fetchone()
                if best_row and (best_row["is_unlimited"] or best_row["is_banned"]):
                    await db.execute(
                        """UPDATE telegram_users 
                           SET is_unlimited = ?, vip_started_at = ?, vip_until = ?, is_banned = ?
                           WHERE telegram_id = ?""",
                        (best_row["is_unlimited"], best_row["vip_started_at"], best_row["vip_until"], best_row["is_banned"], tid)
                    )
            await db.commit()
        except Exception:
            pass

        # Seed default admin user if no users exist
        default_admin = os.getenv("ADMIN_USERNAME", "admin")
        default_pass = os.getenv("ADMIN_PASSWORD", "admin123")
        cursor = await db.execute("SELECT id FROM users LIMIT 1")
        if not await cursor.fetchone():
            await db.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (default_admin, hash_password(default_pass))
            )

        await db.commit()


# --- USER AUTHENTICATION ---

async def get_user_by_username(username: str):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_admin_profile(current_username: str, new_username: str, new_password: str = None) -> tuple[bool, str]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (current_username,))
        user = await cursor.fetchone()
        if not user:
            return False, "Pengguna tidak ditemukan."

        if new_username != current_username:
            chk = await db.execute("SELECT id FROM users WHERE username = ?", (new_username,))
            if await chk.fetchone():
                return False, "Username baru sudah digunakan."

        if new_password and new_password.strip():
            new_hash = hash_password(new_password.strip())
            await db.execute(
                "UPDATE users SET username = ?, password_hash = ? WHERE username = ?",
                (new_username, new_hash, current_username)
            )
        else:
            await db.execute(
                "UPDATE users SET username = ? WHERE username = ?",
                (new_username, current_username)
            )

        await db.commit()
        return True, "Profil admin berhasil diperbarui."


# --- BOTS MANAGEMENT ---

async def get_all_bots():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM bots ORDER BY id DESC")
        rows = await cursor.fetchall()
        bots = []
        for r in rows:
            d = dict(r)
            try:
                d["config_parsed"] = json.loads(d["config"]) if d["config"] else {}
            except Exception:
                d["config_parsed"] = {}
            bots.append(d)
        return bots


async def get_bot_by_id(bot_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["config_parsed"] = json.loads(d["config"]) if d["config"] else {}
        except Exception:
            d["config_parsed"] = {}
        return d


async def create_bot(name: str, username: str, token: str, bot_type: str, config: dict = None):
    config_str = json.dumps(config or {})
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO bots (name, username, token, bot_type, is_active, config) VALUES (?, ?, ?, ?, 1, ?)",
            (name, username, token, bot_type, config_str)
        )
        await db.commit()
        return cursor.lastrowid


async def update_bot(bot_id: int, name: str, token: str, bot_type: str, config: dict):
    config_str = json.dumps(config or {})
    async with get_db() as db:
        await db.execute(
            "UPDATE bots SET name = ?, token = ?, bot_type = ?, config = ? WHERE id = ?",
            (name, token, bot_type, config_str, bot_id)
        )
        await db.commit()


async def delete_bot(bot_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        await db.execute("DELETE FROM telegram_users WHERE bot_id = ?", (bot_id,))
        await db.commit()


async def set_bot_status(bot_id: int, is_active: int):
    async with get_db() as db:
        await db.execute("UPDATE bots SET is_active = ? WHERE id = ?", (is_active, bot_id))
        await db.commit()


# --- ACTIVITY LOGS ---

async def log_activity(
    bot_id: int,
    bot_username: str,
    action: str,
    status: str,
    details: str = "",
    user_telegram_id: int = None,
    user_name: str = None
):
    ts = now_jakarta_str()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO activity_logs (bot_id, bot_username, user_telegram_id, user_name, action, status, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (bot_id, bot_username, user_telegram_id, user_name, action, status, details, ts)
        )
        await db.commit()


async def get_recent_logs(limit: int = 50, offset: int = 0):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM activity_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_logs_count():
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM activity_logs")
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


# --- TELEGRAM USERS & QUOTA SYSTEM ---

async def track_or_update_telegram_user(
    bot_id: int,
    telegram_id: int,
    first_name: str = "",
    last_name: str = "",
    username: str = ""
) -> dict:
    ts = now_jakarta_str()
    async with get_db() as db:
        # Periksa apakah row untuk (bot_id, telegram_id) sudah ada
        cursor = await db.execute(
            "SELECT id, is_unlimited, is_banned, vip_started_at, vip_until FROM telegram_users WHERE bot_id = ? AND telegram_id = ?",
            (bot_id, telegram_id)
        )
        existing = await cursor.fetchone()

        # Ambil status global terbaik dari telegram_id ini jika sudah ada di bot lain
        c_global = await db.execute(
            """SELECT is_unlimited, vip_started_at, vip_until, is_banned 
               FROM telegram_users 
               WHERE telegram_id = ? 
               ORDER BY is_unlimited DESC, is_banned DESC LIMIT 1""",
            (telegram_id,)
        )
        global_rec = await c_global.fetchone()

        if existing:
            # Baris sudah ada, update profile & pastikan status VIP/Ban sinkron dengan status global jika global lebih aktif
            target_unlimited = existing["is_unlimited"]
            target_started = existing["vip_started_at"]
            target_until = existing["vip_until"]
            target_banned = existing["is_banned"]

            if global_rec:
                if global_rec["is_unlimited"] and not target_unlimited:
                    target_unlimited = global_rec["is_unlimited"]
                    target_started = global_rec["vip_started_at"]
                    target_until = global_rec["vip_until"]
                if global_rec["is_banned"] and not target_banned:
                    target_banned = global_rec["is_banned"]

            await db.execute(
                """UPDATE telegram_users 
                   SET first_name = ?, last_name = ?, username = ?, last_active = ?,
                       is_unlimited = ?, vip_started_at = ?, vip_until = ?, is_banned = ?
                   WHERE bot_id = ? AND telegram_id = ?""",
                (first_name or "", last_name or "", username or "", ts,
                 target_unlimited, target_started, target_until, target_banned,
                 bot_id, telegram_id)
            )
        else:
            # Baris baru, warisi status VIP & Ban global jika ada
            init_unlimited = global_rec["is_unlimited"] if global_rec else 0
            init_started = global_rec["vip_started_at"] if global_rec else None
            init_until = global_rec["vip_until"] if global_rec else None
            init_banned = global_rec["is_banned"] if global_rec else 0

            await db.execute(
                """INSERT INTO telegram_users 
                   (bot_id, telegram_id, first_name, last_name, username, is_unlimited, is_banned, vip_started_at, vip_until, last_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (bot_id, telegram_id, first_name or "", last_name or "", username or "",
                 init_unlimited, init_banned, init_started, init_until, ts, ts)
            )

        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM telegram_users WHERE bot_id = ? AND telegram_id = ?",
            (bot_id, telegram_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}


def parse_datetime_flexible(dt_val: str | datetime | None) -> datetime | None:
    """Mengubah berbagai variasi format tanggal string menjadi datetime object (Asia/Jakarta)."""
    if not dt_val:
        return None
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            return dt_val.replace(tzinfo=JAKARTA_TZ)
        return dt_val.astimezone(JAKARTA_TZ)
    s = str(dt_val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=JAKARTA_TZ)
        except ValueError:
            pass
    return None


def format_date_id(dt: datetime | None) -> str:
    """Format tanggal Indonesia Jakarta yang rapi: DD MMM YYYY, HH:MM WIB"""
    if not dt:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JAKARTA_TZ)
    else:
        dt = dt.astimezone(JAKARTA_TZ)
    months = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    return f"{dt.day:02d} {months[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d} WIB"


def get_vip_status_info(is_unlimited: int, vip_started_at: str | None, vip_until: str | None) -> dict:
    """
    Menghitung status VIP secara real-time berdasarkan zona waktu Asia/Jakarta:
    - is_vip: bool (apakah saat ini berstatus VIP aktif)
    - is_lifetime: bool (apakah VIP permanen tanpa batas tanggal)
    - is_expired: bool (apakah masa VIP sudah berakhir)
    - started_str: str (format tanggal mulai beli/aktif)
    - until_str: str (format tanggal expired)
    - remaining_text: str (misal '29 hari lagi', '12 jam lagi', 'Kedaluwarsa')
    - remaining_days: int
    - status_label: str
    - badge_color: str ('emerald', 'amber', 'red', 'gray')
    """
    now = now_jakarta()
    started_dt = parse_datetime_flexible(vip_started_at)
    until_dt = parse_datetime_flexible(vip_until)

    # 1. Jika bukan unlimited dan tidak pernah diset tanggal expired
    if not is_unlimited and not until_dt:
        return {
            "is_vip": False,
            "is_lifetime": False,
            "is_expired": False,
            "status_label": "Standar",
            "started_str": "-",
            "until_str": "-",
            "remaining_text": "-",
            "remaining_days": 0,
            "remaining_hours": 0,
            "badge_color": "gray"
        }

    # 2. Jika is_unlimited aktif TANPA batas waktu (Lifetime)
    if is_unlimited and not until_dt:
        return {
            "is_vip": True,
            "is_lifetime": True,
            "is_expired": False,
            "status_label": "VIP Lifetime",
            "started_str": format_date_id(started_dt) if started_dt else "Permanen",
            "until_str": "Selamanya (Permanen)",
            "remaining_text": "♾️ Permanen (Lifetime)",
            "remaining_days": 999999,
            "remaining_hours": 0,
            "badge_color": "emerald"
        }

    # 3. Jika memiliki batas waktu until_dt
    if until_dt:
        if now < until_dt:
            # VIP Masih Aktif
            delta = until_dt - now
            days = delta.days
            hours = delta.seconds // 3600
            minutes = (delta.seconds % 3600) // 60

            if days > 1:
                remaining_text = f"{days} hari lagi"
            elif days == 1:
                remaining_text = f"1 hari {hours} jam lagi"
            elif hours > 0:
                remaining_text = f"{hours} jam {minutes} menit lagi"
            else:
                remaining_text = f"{max(1, minutes)} menit lagi"

            return {
                "is_vip": True,
                "is_lifetime": False,
                "is_expired": False,
                "status_label": "VIP Aktif",
                "started_str": format_date_id(started_dt) if started_dt else "-",
                "until_str": format_date_id(until_dt),
                "remaining_text": remaining_text,
                "remaining_days": days,
                "remaining_hours": hours,
                "badge_color": "amber"
            }
        else:
            # Masa VIP sudah kedaluwarsa
            return {
                "is_vip": False,
                "is_lifetime": False,
                "is_expired": True,
                "status_label": "VIP Kedaluwarsa",
                "started_str": format_date_id(started_dt) if started_dt else "-",
                "until_str": format_date_id(until_dt),
                "remaining_text": "Kedaluwarsa (Expired)",
                "remaining_days": 0,
                "remaining_hours": 0,
                "badge_color": "red"
            }

    return {
        "is_vip": False,
        "is_lifetime": False,
        "is_expired": False,
        "status_label": "Standar",
        "started_str": "-",
        "until_str": "-",
        "remaining_text": "-",
        "remaining_days": 0,
        "remaining_hours": 0,
        "badge_color": "gray"
    }


async def get_user_quota_info(bot_id: int, telegram_id: int, daily_limit: int = 0) -> dict:
    """
    Mengembalikan data lengkap kuota user:
    - allowed: bool (apakah diizinkan melakukan aksi)
    - is_banned: bool
    - is_unlimited: bool
    - today_used: int (pemakaian hari ini)
    - daily_limit: int (batas harian)
    - remaining: int (sisa operasi)
    - quota_text: str (misal '1/1x per hari', '👑 VIP Lifetime (Tanpa Batas)', 'Standar (Tanpa Batas Kuota Harian)')
    - alert_message: str (pesan penolakan yang rapi jika kuota habis)
    - vip_info: dict (informasi lengkap durasi dan masa aktif VIP global)
    """
    async with get_db() as db:
        # 1. Ambil data baris bot ini
        cursor = await db.execute(
            "SELECT * FROM telegram_users WHERE bot_id = ? AND telegram_id = ?",
            (bot_id, telegram_id)
        )
        user = await cursor.fetchone()
        u = dict(user) if user else {}

        # 2. Ambil status global terbaik (di seluruh bot) untuk telegram_id ini
        c_global = await db.execute(
            """SELECT is_unlimited, vip_started_at, vip_until, is_banned 
               FROM telegram_users 
               WHERE telegram_id = ? 
               ORDER BY is_banned DESC, is_unlimited DESC LIMIT 1""",
            (telegram_id,)
        )
        g_row = await c_global.fetchone()

        if g_row:
            # Selaraskan status jika status global lebih tinggi/aktif
            need_sync = False
            target_banned = u.get("is_banned", 0)
            target_unlimited = u.get("is_unlimited", 0)
            target_started = u.get("vip_started_at")
            target_until = u.get("vip_until")

            if g_row["is_banned"] and not target_banned:
                target_banned = 1
                need_sync = True
            if g_row["is_unlimited"] and not target_unlimited:
                target_unlimited = 1
                target_started = g_row["vip_started_at"]
                target_until = g_row["vip_until"]
                need_sync = True

            if need_sync and u:
                u["is_banned"] = target_banned
                u["is_unlimited"] = target_unlimited
                u["vip_started_at"] = target_started
                u["vip_until"] = target_until
                await db.execute(
                    """UPDATE telegram_users 
                       SET is_banned = ?, is_unlimited = ?, vip_started_at = ?, vip_until = ?
                       WHERE bot_id = ? AND telegram_id = ?""",
                    (target_banned, target_unlimited, target_started, target_until, bot_id, telegram_id)
                )
                await db.commit()

        # 3. Cek Status Banned
        is_banned_val = u.get("is_banned", 0) or (g_row["is_banned"] if g_row else 0)
        if is_banned_val:
            return {
                "allowed": False,
                "is_banned": True,
                "is_unlimited": False,
                "daily_limit": daily_limit,
                "today_used": 0,
                "remaining": 0,
                "quota_text": "🚫 Diblokir",
                "alert_message": "⛔ <b>Akses Ditolak</b>\n\nAkun Telegram Anda telah diblokir oleh administrator bot.",
                "vip_info": get_vip_status_info(0, None, None),
                "user": u
            }

        # 4. Hitung Status VIP Global
        chk_unlimited = u.get("is_unlimited", 0) or (g_row["is_unlimited"] if g_row else 0)
        chk_started = u.get("vip_started_at") or (g_row["vip_started_at"] if g_row else None)
        chk_until = u.get("vip_until") or (g_row["vip_until"] if g_row else None)

        vip_info = get_vip_status_info(chk_unlimited, chk_started, chk_until)

        # Jika VIP Masih Aktif (Lifetime maupun Berdurasi)
        if vip_info["is_vip"]:
            if vip_info["is_lifetime"]:
                quota_text = "👑 VIP Lifetime (Tanpa Batas)"
            else:
                quota_text = f"👑 VIP Member (Sisa {vip_info['remaining_text']})"

            return {
                "allowed": True,
                "is_banned": False,
                "is_unlimited": True,
                "daily_limit": daily_limit,
                "today_used": 0,
                "remaining": 999999,
                "quota_text": quota_text,
                "alert_message": "",
                "vip_info": vip_info,
                "user": u
            }

        # Catatan tambahan jika VIP sudah expired
        vip_note = ""
        if vip_info["is_expired"]:
            vip_note = f"\n\n⚠️ <i>Masa VIP Anda telah kedaluwarsa pada {vip_info['until_str']}. Hubungi admin untuk memperpanjang langganan VIP!</i>"

        # 5. Cek Daily Limit (Waktu Jakarta) untuk Pengguna Standar
        if daily_limit and daily_limit > 0:
            today_jkt = now_jakarta().strftime("%Y-%m-%d")
            c = await db.execute(
                """SELECT COUNT(*) as cnt FROM activity_logs 
                   WHERE bot_id = ? AND user_telegram_id = ? AND status = 'success' 
                   AND DATE(created_at) = ?""",
                (bot_id, telegram_id, today_jkt)
            )
            res = await c.fetchone()
            today_used = res["cnt"] if res else 0
            remaining = max(0, daily_limit - today_used)
            quota_text = f"{today_used}/{daily_limit}x per hari"

            if today_used >= daily_limit:
                alert = (
                    f"⛔ <b>Kuota Harian Anda Telah Habis ({today_used}/{daily_limit}x per hari)</b>\n\n"
                    f"📊 <b>Pemakaian Hari Ini:</b> {today_used} dari {daily_limit} kali\n"
                    f"⏳ <b>Reset Kuota:</b> Besok hari (00:00 WIB){vip_note}\n\n"
                    f"<i>Silakan coba lagi besok, atau hubungi admin untuk upgrade ke <b>VIP Member</b>!</i>"
                )
                return {
                    "allowed": False,
                    "is_banned": False,
                    "is_unlimited": False,
                    "daily_limit": daily_limit,
                    "today_used": today_used,
                    "remaining": 0,
                    "quota_text": quota_text,
                    "alert_message": alert,
                    "vip_info": vip_info,
                    "user": u
                }

            return {
                "allowed": True,
                "is_banned": False,
                "is_unlimited": False,
                "daily_limit": daily_limit,
                "today_used": today_used,
                "remaining": remaining,
                "quota_text": quota_text,
                "alert_message": "",
                "vip_info": vip_info,
                "user": u
            }

        # Default: Pengguna Standar pada bot tanpa batasan kuota harian (daily limit <= 0)
        return {
            "allowed": True,
            "is_banned": False,
            "is_unlimited": False,
            "daily_limit": 0,
            "today_used": 0,
            "remaining": 999999,
            "quota_text": "Standar (Tanpa Batas Kuota Harian)",
            "alert_message": "",
            "vip_info": vip_info,
            "user": u
        }


async def check_user_quota_allowed(bot_id: int, telegram_id: int, daily_limit: int = 0) -> tuple[bool, str, dict]:
    """Wrapper untuk backward compatibility."""
    info = await get_user_quota_info(bot_id, telegram_id, daily_limit)
    return info["allowed"], info["alert_message"], info["user"]


async def increment_user_ops(bot_id: int, telegram_id: int):
    ts = now_jakarta_str()
    async with get_db() as db:
        await db.execute(
            """UPDATE telegram_users 
               SET total_ops = total_ops + 1, last_active = ? 
               WHERE bot_id = ? AND telegram_id = ?""",
            (ts, bot_id, telegram_id)
        )
        await db.commit()


async def get_all_telegram_users(bot_id: int = None, search: str = None):
    """
    Mengambil direktori pengguna terkonsolidasi per telegram_id unik.
    Setiap entri menggabungkan seluruh bot yang digunakan pengguna tersebut
    beserta total akumulasi operasi dan status VIP global.
    """
    async with get_db() as db:
        query = """
            SELECT tu.*, b.name as bot_name, b.username as bot_username, b.bot_type
            FROM telegram_users tu
            LEFT JOIN bots b ON tu.bot_id = b.id
            ORDER BY tu.last_active DESC
        """
        cursor = await db.execute(query)
        rows = await cursor.fetchall()

        grouped: dict[int, dict] = {}
        for r in rows:
            d = dict(r)
            tid = d["telegram_id"]
            if tid not in grouped:
                grouped[tid] = {
                    "id": d["id"],
                    "telegram_id": tid,
                    "first_name": d.get("first_name") or "",
                    "last_name": d.get("last_name") or "",
                    "username": d.get("username") or "",
                    "is_banned": d.get("is_banned", 0),
                    "is_unlimited": d.get("is_unlimited", 0),
                    "vip_started_at": d.get("vip_started_at"),
                    "vip_until": d.get("vip_until"),
                    "total_ops": 0,
                    "last_active": d.get("last_active"),
                    "bots": []
                }

            user_entry = grouped[tid]

            # Simpan nama/username yang paling lengkap
            if d.get("first_name") and not user_entry["first_name"]:
                user_entry["first_name"] = d["first_name"]
            if d.get("last_name") and not user_entry["last_name"]:
                user_entry["last_name"] = d["last_name"]
            if d.get("username") and not user_entry["username"]:
                user_entry["username"] = d["username"]

            # Sinkronisasi status VIP global terbaik
            if d.get("is_unlimited") and not user_entry["is_unlimited"]:
                user_entry["is_unlimited"] = 1
                user_entry["vip_started_at"] = d.get("vip_started_at")
                user_entry["vip_until"] = d.get("vip_until")
            elif d.get("vip_until") and (not user_entry["vip_until"] or str(d.get("vip_until")) > str(user_entry["vip_until"])):
                user_entry["vip_until"] = d.get("vip_until")
                if not user_entry["vip_started_at"]:
                    user_entry["vip_started_at"] = d.get("vip_started_at")

            # Banned global jika salah satu banned
            if d.get("is_banned"):
                user_entry["is_banned"] = 1

            # Akumulasi total operasi
            user_entry["total_ops"] += (d.get("total_ops") or 0)
            if str(d.get("last_active", "")) > str(user_entry.get("last_active", "")):
                user_entry["last_active"] = d.get("last_active")

            # Tambahkan detail bot yang digunakan
            user_entry["bots"].append({
                "bot_id": d.get("bot_id"),
                "bot_name": d.get("bot_name") or f"Bot #{d.get('bot_id')}",
                "bot_username": d.get("bot_username") or "",
                "bot_type": d.get("bot_type") or "unknown",
                "ops": d.get("total_ops") or 0,
                "last_active": d.get("last_active")
            })

        result = []
        for tid, u in grouped.items():
            u["vip_info"] = get_vip_status_info(
                u.get("is_unlimited", 0),
                u.get("vip_started_at"),
                u.get("vip_until")
            )

            # Filter spesifik bot_id jika user memilih dropdown bot
            if bot_id:
                bot_ids = [b["bot_id"] for b in u["bots"]]
                if bot_id not in bot_ids:
                    continue

            # Filter search
            if search:
                s = search.strip().lower()
                haystack = f"{u['first_name']} {u['last_name']} {u['username']} {u['telegram_id']}".lower()
                if s not in haystack:
                    continue

            result.append(u)

        # Urutkan berdasarkan waktu aktif terbaru
        result.sort(key=lambda x: str(x.get("last_active") or ""), reverse=True)
        return result


async def set_user_vip(
    user_id: int,
    duration_type: str,
    days: int = 0,
    custom_until: str = None,
    extend: bool = True
) -> dict:
    """
    Mengatur paket VIP user secara GLOBAL untuk seluruh bot:
    - user_id: ID primary key di tabel telegram_users ATAU telegram_id
    - duration_type: 'days', 'lifetime', 'custom', 'remove'
    - days: jumlah hari (misal 7, 30, 90, 365)
    - custom_until: string tanggal/jam kustom (YYYY-MM-DD HH:MM:SS atau YYYY-MM-DDTHH:MM)
    - extend: jika True dan user masih punya sisa VIP, durasi ditambahkan dari tanggal expired sebelumnya
    """
    async with get_db() as db:
        c = await db.execute(
            "SELECT telegram_id, vip_started_at, vip_until FROM telegram_users WHERE id = ? OR telegram_id = ? LIMIT 1",
            (user_id, user_id)
        )
        u = await c.fetchone()
        if not u:
            return {"success": False, "error": "User tidak ditemukan"}

        telegram_id = u["telegram_id"]
        now = now_jakarta()

        if duration_type == "remove":
            await db.execute(
                """UPDATE telegram_users 
                   SET is_unlimited = 0, vip_until = NULL, vip_started_at = NULL 
                   WHERE telegram_id = ?""",
                (telegram_id,)
            )
            await db.commit()
            return {"success": True, "action": "removed"}

        if duration_type == "lifetime":
            started_at = u["vip_started_at"] or now.strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                """UPDATE telegram_users 
                   SET is_unlimited = 1, vip_until = NULL, vip_started_at = ? 
                   WHERE telegram_id = ?""",
                (started_at, telegram_id)
            )
            await db.commit()
            return {"success": True, "action": "lifetime"}

        if duration_type == "custom":
            dt_until = parse_datetime_flexible(custom_until)
            if not dt_until:
                return {"success": False, "error": "Format tanggal kustom tidak valid"}
            started_at = u["vip_started_at"] or now.strftime("%Y-%m-%d %H:%M:%S")
            until_str = dt_until.strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                """UPDATE telegram_users 
                   SET is_unlimited = 1, vip_until = ?, vip_started_at = ? 
                   WHERE telegram_id = ?""",
                (until_str, started_at, telegram_id)
            )
            await db.commit()
            return {"success": True, "action": "custom", "until": until_str}

        # duration_type == 'days'
        base_dt = now
        curr_until = parse_datetime_flexible(u["vip_until"])
        if extend and curr_until and curr_until > now:
            # Perpanjang dari tanggal expired sebelumnya
            base_dt = curr_until

        new_until = base_dt + timedelta(days=days)
        started_at = u["vip_started_at"] or now.strftime("%Y-%m-%d %H:%M:%S")
        until_str = new_until.strftime("%Y-%m-%d %H:%M:%S")

        await db.execute(
            """UPDATE telegram_users 
               SET is_unlimited = 1, vip_until = ?, vip_started_at = ? 
               WHERE telegram_id = ?""",
            (until_str, started_at, telegram_id)
        )
        await db.commit()
        return {"success": True, "action": "days", "days": days, "until": until_str}


async def toggle_user_unlimited(user_id: int) -> int:
    """Toggle status VIP secara global untuk seluruh bot."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT telegram_id, is_unlimited, vip_until FROM telegram_users WHERE id = ? OR telegram_id = ? LIMIT 1",
            (user_id, user_id)
        )
        row = await cursor.fetchone()
        if not row:
            return 0

        telegram_id = row["telegram_id"]
        now = now_jakarta()
        is_currently_active = False
        if row["is_unlimited"]:
            until_dt = parse_datetime_flexible(row["vip_until"])
            if not until_dt or until_dt > now:
                is_currently_active = True

        if is_currently_active:
            # Matikan VIP global
            await set_user_vip(telegram_id, duration_type="remove")
            return 0
        else:
            # Aktifkan default 30 Hari (1 Bulan) global
            await set_user_vip(telegram_id, duration_type="days", days=30)
            return 1


async def toggle_user_ban(user_id: int) -> int:
    """Toggle status banned secara global untuk seluruh bot."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT telegram_id, is_banned FROM telegram_users WHERE id = ? OR telegram_id = ? LIMIT 1",
            (user_id, user_id)
        )
        row = await cursor.fetchone()
        if not row:
            return 0
        telegram_id = row["telegram_id"]
        new_val = 0 if row["is_banned"] else 1
        await db.execute("UPDATE telegram_users SET is_banned = ? WHERE telegram_id = ?", (new_val, telegram_id))
        await db.commit()
        return new_val


async def delete_telegram_user(user_id: int):
    """Hapus data pengguna secara global dari seluruh bot."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM telegram_users WHERE id = ? OR telegram_id = ? LIMIT 1",
            (user_id, user_id)
        )
        row = await cursor.fetchone()
        if not row:
            return
        telegram_id = row["telegram_id"]
        await db.execute("DELETE FROM telegram_users WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


# --- BROADCAST RECIPIENTS ---

async def get_broadcast_recipients(bot_id: int = None):
    async with get_db() as db:
        if bot_id:
            cursor = await db.execute(
                "SELECT DISTINCT telegram_id, bot_id, first_name, username FROM telegram_users WHERE bot_id = ? AND is_banned = 0",
                (bot_id,)
            )
        else:
            cursor = await db.execute(
                "SELECT DISTINCT telegram_id, bot_id, first_name, username FROM telegram_users WHERE is_banned = 0"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# --- ANALYTICS & STATS ---

async def get_stats():
    async with get_db() as db:
        c1 = await db.execute("SELECT COUNT(*) as total, SUM(is_active) as active FROM bots")
        r1 = await c1.fetchone()

        c2 = await db.execute("SELECT COUNT(*) as total_logs FROM activity_logs WHERE status = 'success'")
        r2 = await c2.fetchone()

        current_jkt = now_jakarta_str()
        today_date = now_jakarta().strftime("%Y-%m-%d")

        c3 = await db.execute("""
            SELECT COUNT(DISTINCT telegram_id) as total_users,
                   COUNT(DISTINCT CASE WHEN is_unlimited = 1 AND (vip_until IS NULL OR datetime(vip_until) > datetime(?)) THEN telegram_id ELSE NULL END) as total_unlimited
            FROM telegram_users
        """, (current_jkt,))
        r3 = await c3.fetchone()

        c4 = await db.execute(
            "SELECT COUNT(*) as today_ops FROM activity_logs WHERE status = 'success' AND DATE(created_at) = ?",
            (today_date,)
        )
        r4 = await c4.fetchone()

        return {
            "total_bots": r1["total"] or 0,
            "active_bots": r1["active"] or 0,
            "total_success_ops": r2["total_logs"] or 0,
            "total_users": r3["total_users"] or 0,
            "total_unlimited": r3["total_unlimited"] or 0,
            "today_ops": r4["today_ops"] or 0
        }


async def get_analytics_chart_data():
    async with get_db() as db:
        days = []
        counts = []
        for i in range(6, -1, -1):
            dt_target = now_jakarta() - timedelta(days=i)
            d = dt_target.strftime("%Y-%m-%d")
            c = await db.execute(
                "SELECT COUNT(*) as cnt FROM activity_logs WHERE status = 'success' AND DATE(created_at) = ?",
                (d,)
            )
            row = await c.fetchone()
            days.append(dt_target.strftime("%d %b"))
            counts.append(row["cnt"] if row else 0)

        c_dist = await db.execute("""
            SELECT b.bot_type, COUNT(l.id) as cnt
            FROM activity_logs l
            JOIN bots b ON l.bot_id = b.id
            WHERE l.status = 'success'
            GROUP BY b.bot_type
        """)
        dist_rows = await c_dist.fetchall()
        bot_types = {"img2pdf": 0, "mp3": 0, "mp4": 0}
        for dr in dist_rows:
            if dr["bot_type"] in bot_types:
                bot_types[dr["bot_type"]] = dr["cnt"]

        return {
            "labels": days,
            "activity_counts": counts,
            "bot_types": bot_types
        }


# --- STORAGE UTILITIES ---

def get_storage_stats():
    total_size = 0
    file_count = 0
    items = []
    if os.path.exists(STORAGE_DIR):
        for entry in os.scandir(STORAGE_DIR):
            if entry.is_file():
                size = entry.stat().st_size
                total_size += size
                file_count += 1
                items.append({"name": entry.name, "is_dir": False, "size": size})
            elif entry.is_dir():
                dir_size = 0
                dir_files = 0
                for root, _, files in os.walk(entry.path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            s = os.path.getsize(fp)
                            dir_size += s
                            dir_files += 1
                        except OSError:
                            pass
                total_size += dir_size
                file_count += dir_files
                items.append({"name": entry.name, "is_dir": True, "size": dir_size, "files": dir_files})

    return {
        "path": STORAGE_DIR,
        "total_size_bytes": total_size,
        "file_count": file_count,
        "items": items
    }


def purge_storage_temp_files():
    purged_bytes = 0
    purged_items = 0
    if os.path.exists(STORAGE_DIR):
        for entry in os.scandir(STORAGE_DIR):
            try:
                if entry.is_file():
                    purged_bytes += entry.stat().st_size
                    os.remove(entry.path)
                    purged_items += 1
                elif entry.is_dir():
                    for root, _, files in os.walk(entry.path):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                purged_bytes += os.path.getsize(fp)
                            except OSError:
                                pass
                    shutil.rmtree(entry.path)
                    purged_items += 1
            except Exception:
                pass
    return {"purged_bytes": purged_bytes, "purged_items": purged_items}
