import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
import yt_dlp

from app.handlers.common import (
    STORAGE_DIR,
    sanitize_filename,
    format_duration,
    cleanup_user_dir,
    clean_and_validate_media_url,
    fetch_content_length,
    parse_duration_seconds,
    safe_chat_action
)
from app.database import (
    log_activity,
    track_or_update_telegram_user,
    get_user_quota_info,
    increment_user_ops
)

logger = logging.getLogger("mp3_downloader")

user_mp3_sessions: dict[int, dict[int, dict]] = {}
user_last_quota_alert_mp3: dict[int, dict[int, float]] = {}


def get_user_session(bot_id: int, user_id: int) -> dict:
    if bot_id not in user_mp3_sessions:
        user_mp3_sessions[bot_id] = {}
    
    now = time.time()
    if user_id in user_mp3_sessions[bot_id]:
        sess = user_mp3_sessions[bot_id][user_id]
        if now - sess.get("last_activity", now) > 3600 and not sess.get("is_downloading"):
            user_dir = os.path.join(STORAGE_DIR, f"mp3_{bot_id}_{user_id}")
            cleanup_user_dir(user_dir)
            sess["current_url"] = None
            sess["video_info"] = None
            sess["custom_filename"] = ""
            sess["state"] = "idle"
            sess["info_msg_id"] = None
            sess["prompt_msg_id"] = None
        sess["last_activity"] = now
        return sess

    user_mp3_sessions[bot_id][user_id] = {
        "current_url": None,
        "video_info": None,
        "custom_filename": "",
        "state": "idle",
        "info_msg_id": None,
        "prompt_msg_id": None,
        "is_downloading": False,
        "last_activity": now
    }
    return user_mp3_sessions[bot_id][user_id]


def estimate_audio_sizes(duration_sec: float = 0, info: dict = None) -> dict:
    """
    Menghitung perkiraan ukuran file MP3 (dalam MB) berdasarkan durasi dan bitrate (128, 192, 320 kbps).
    Mendukung YouTube, Facebook, Instagram, TikTok, Twitter/X, SoundCloud, dll.
    """
    if not duration_sec or duration_sec <= 0:
        if info:
            raw_dur = info.get('duration') or info.get('duration_string')
            duration_sec = parse_duration_seconds(raw_dur)

    if duration_sec and duration_sec > 0:
        res = {}
        for br in [128, 192, 320]:
            mb = (br * 1000 / 8) * duration_sec / (1024 * 1024)
            res[br] = round(mb, 1)
        return res

    # Fallback jika durasi tidak terdeteksi dari metadata platform
    if info:
        formats = info.get('formats') or []
        for f in formats[:3]:
            sz = f.get('filesize') or f.get('filesize_approx')
            if not sz and f.get('url'):
                sz = fetch_content_length(f.get('url'), timeout=2.0)
            if sz:
                base_mb = sz / (1024 * 1024)
                return {
                    128: round(max(0.5, base_mb * 0.2), 1),
                    192: round(max(0.7, base_mb * 0.3), 1),
                    320: round(max(1.0, base_mb * 0.45), 1)
                }

    return {}


def build_mp3_keyboard(session_id: str, estimated_sizes: dict = None) -> InlineKeyboardMarkup:
    estimated_sizes = estimated_sizes or {}

    mb_128 = estimated_sizes.get(128)
    badge_128 = f" (~{mb_128:.1f} MB)" if mb_128 else " (Standar)"
    text_128 = f"🎵 128 kbps{badge_128}"

    mb_192 = estimated_sizes.get(192)
    badge_192 = f" (~{mb_192:.1f} MB)" if mb_192 else " (Medium)"
    text_192 = f"🎧 192 kbps{badge_192}"

    mb_320 = estimated_sizes.get(320)
    if mb_320:
        badge_320 = f" (~{mb_320:.1f} MB)" if mb_320 <= 49.5 else f" (~{mb_320:.1f} MB ⚠️ >50MB)"
    else:
        badge_320 = " (HQ)"
    text_320 = f"🔊 320 kbps{badge_320}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text_128, callback_data=f"mp3:dl:128:{session_id}"),
                InlineKeyboardButton(text=text_192, callback_data=f"mp3:dl:192:{session_id}")
            ],
            [
                InlineKeyboardButton(text=text_320, callback_data=f"mp3:dl:320:{session_id}")
            ],
            [
                InlineKeyboardButton(text="✏️ Ubah Nama File (Opsional)", callback_data=f"mp3:rename:{session_id}"),
                InlineKeyboardButton(text="❌ Batal", callback_data=f"mp3:cancel:{session_id}")
            ]
        ]
    )


def build_start_keyboard(custom_buttons: list) -> InlineKeyboardMarkup | None:
    if not custom_buttons:
        return None
    rows = []
    for btn in custom_buttons:
        text = btn.get("text", "Link")
        url = btn.get("url")
        if url:
            rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def notify_quota_exceeded(message: Message, bot_id: int, user_id: int, alert_text: str):
    now = time.time()
    last = user_last_quota_alert_mp3.get(bot_id, {}).get(user_id, 0)
    if now - last > 4.0:
        if bot_id not in user_last_quota_alert_mp3:
            user_last_quota_alert_mp3[bot_id] = {}
        user_last_quota_alert_mp3[bot_id][user_id] = now
        await message.answer(alert_text, parse_mode="HTML")


async def cmd_start(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    await track_or_update_telegram_user(
        bot_id=bot_id,
        telegram_id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=message.from_user.username
    )

    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, message.from_user.id, limit)

    if quota_info.get("is_banned"):
        await message.answer("⛔ <i>Akun Anda telah diblokir oleh administrator bot.</i>", parse_mode="HTML")
        return

    session = get_user_session(bot_id, message.from_user.id)
    if session.get("info_msg_id"):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=session["info_msg_id"])
        except Exception:
            pass
        session["info_msg_id"] = None

    config = bot_config or {}
    base_msg = config.get(
        "custom_start_msg",
        "🎧 <b>Halo! Selamat datang di MP3 Downloader Bot.</b>\n\n"
        "Kirimkan URL/link lagu atau video (YouTube, TikTok, Soundcloud, Instagram, dll), "
        "lalu pilih kualitas audio yang kamu inginkan!\n\n"
        "✨ <i>Fitur:</i>\n"
        "• Pilihan bitrate: 128k, 192k, hingga 320k\n"
        "• Bisa custom nama file lagu sesuai keinginan\n"
        "• Validasi link tunggal & pembersihan otomatis kartu tombol\n"
        "• Tombol interaktif modern!"
    )

    vip_info = quota_info.get("vip_info", {})
    if vip_info.get("is_vip"):
        if vip_info.get("is_lifetime"):
            vip_text = (
                "👑 <b>Status Akun:</b> <code>VIP Member (Lifetime)</code>\n"
                "♾️ <b>Masa Berlaku:</b> Permanen (Tanpa Batas Waktu & Kuota)"
            )
        else:
            vip_text = (
                f"👑 <b>Status Akun:</b> <code>VIP Member (Aktif)</code>\n"
                f"📅 <b>Periode Aktif:</b> <code>{vip_info['started_str']} s/d {vip_info['until_str']}</code>\n"
                f"⏳ <b>Sisa Masa Aktif:</b> <b>{vip_info['remaining_text']}</b>\n"
                f"♾️ <b>Akses:</b> Kuota Unduhan Tanpa Batas"
            )
    elif vip_info.get("is_expired"):
        vip_text = (
            f"📊 <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n"
            f"⚠️ <i>Masa VIP Anda telah kedaluwarsa pada {vip_info['until_str']}. Hubungi admin untuk perpanjangan!</i>"
        )
    else:
        vip_text = f"📊 <b>Batas Kuota Anda:</b> <code>{quota_info['quota_text']}</code>"

    start_msg = f"{base_msg}\n\n{vip_text}"

    custom_btns = config.get("custom_buttons", [])
    kb = build_start_keyboard(custom_btns)
    await message.answer(start_msg, parse_mode="HTML", reply_markup=kb)


async def cmd_reset(message: Message, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, message.from_user.id)
    if session.get("info_msg_id"):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=session["info_msg_id"])
        except Exception:
            pass
    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass

    session["current_url"] = None
    session["video_info"] = None
    session["custom_filename"] = ""
    session["state"] = "idle"
    session["info_msg_id"] = None
    session["prompt_msg_id"] = None
    session["is_downloading"] = False

    user_dir = os.path.join(STORAGE_DIR, f"mp3_{bot_id}_{message.from_user.id}")
    shutil.rmtree(user_dir, ignore_errors=True)

    await message.answer("🔄 Sesi MP3 berhasil direset. Silakan kirimkan link baru.", parse_mode="HTML")


async def handle_url(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    await track_or_update_telegram_user(
        bot_id=bot_id,
        telegram_id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=message.from_user.username
    )

    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, message.from_user.id, limit)

    if not quota_info["allowed"]:
        await notify_quota_exceeded(message, bot_id, message.from_user.id, quota_info["alert_message"])
        return

    # 1. Validasi: Cari semua URL yang dikirim di dalam teks
    urls = re.findall(r'https?://[^\s]+', message.text)
    if not urls:
        await message.answer(
            "⚠️ <b>Tautan Tidak Valid!</b>\n\n"
            "Pastikan link yang Anda kirimkan diawali dengan <code>http://</code> atau <code>https://</code>.",
            parse_mode="HTML"
        )
        return

    # 2. Validasi Ketat: Hanya izinkan tepat 1 link per pesan
    if len(urls) > 1:
        links_preview = "\n".join([f"• <code>{u[:50]}...</code>" if len(u) > 50 else f"• <code>{u}</code>" for u in urls[:5]])
        await message.answer(
            f"⚠️ <b>Hanya Boleh Mengirim 1 Link Saja!</b>\n\n"
            f"Terdeteksi <b>{len(urls)} tautan</b> sekaligus dalam satu pesan:\n"
            f"{links_preview}\n\n"
            f"<i>Mohon kirimkan <b>1 link audio/video saja</b> per pesan agar proses konversi berjalan lancar.</i>",
            parse_mode="HTML"
        )
        return

    raw_url = urls[0]
    clean_url, is_pure_playlist, playlist_err = clean_and_validate_media_url(raw_url)
    if is_pure_playlist:
        await message.answer(playlist_err, parse_mode="HTML")
        return

    url = clean_url
    session = get_user_session(bot_id, message.from_user.id)

    # 3. Validasi: Cek apakah user sedang mengunduh lagu lain
    if session.get("is_downloading"):
        await message.answer(
            "⏳ <b>Sedang Memproses Unduhan Lain!</b>\n\n"
            "Mohon tunggu hingga proses konversi & download sebelumnya selesai sebelum mengirimkan link baru.",
            parse_mode="HTML"
        )
        return

    # Hapus kartu informasi lama jika ada
    if session.get("info_msg_id"):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=session["info_msg_id"])
        except Exception:
            pass
        session["info_msg_id"] = None

    session_id = str(uuid.uuid4())[:8]

    # Kirim chat action typing di header chat (non-blocking & safe)
    asyncio.create_task(safe_chat_action(bot, message.chat.id, "typing"))

    status_msg = await message.answer("🔍 <b>Mengambil informasi audio...</b>\n<i>Mohon tunggu sebentar...</i>", parse_mode="HTML")
    session["info_msg_id"] = status_msg.message_id

    def extract_info():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'geo_bypass': True,
            'nocheckcertificate': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(extract_info), timeout=25.0)
        title = info.get('title', 'Audio Track')
        duration = format_duration(info.get('duration'))
        duration_sec = parse_duration_seconds(info.get('duration') or info.get('duration_string'))
        uploader = info.get('uploader', 'Unknown Artist')
        estimated_sizes = estimate_audio_sizes(duration_sec, info)

        session["current_url"] = url
        session["video_info"] = {
            "title": title,
            "duration": duration,
            "duration_sec": duration_sec,
            "uploader": uploader,
            "session_id": session_id,
            "estimated_sizes": estimated_sizes
        }
        session["custom_filename"] = ""
        session["state"] = "idle"

        display_name = session["custom_filename"] or title

        text = (
            f"🎵 <b>Informasi Audio Ditemukan:</b>\n\n"
            f"📌 <b>Judul:</b> {title}\n"
            f"👤 <b>Artis/Uploader:</b> {uploader}\n"
            f"⏱️ <b>Durasi:</b> {duration}\n"
            f"📁 <b>Nama File:</b> <code>{display_name}.mp3</code>\n"
            f"🏷️ <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Silakan pilih kualitas bitrate atau ubah nama file:</i>"
        )

        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=build_mp3_keyboard(session_id, estimated_sizes))

    except asyncio.TimeoutError:
        session["info_msg_id"] = None
        await status_msg.edit_text(
            "⏱️ <b>Waktu Permintaan Habis (Timeout)!</b>\n\n"
            "Server platform audio tidak merespons dalam 25 detik atau link terlalu lambat diproses.\n\n"
            "💡 <i>Silakan coba kirim ulang tautan atau pastikan tautan audio dapat diakses publik.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        session["info_msg_id"] = None
        await status_msg.edit_text(
            f"❌ <b>Gagal Mengambil Informasi Media!</b>\n\n"
            f"<code>{str(e)[:200]}</code>\n\n"
            f"💡 <i>Pastikan tautan valid, dapat diakses publik, dan video/lagu tidak bersifat privat atau berbayar.</i>",
            parse_mode="HTML"
        )


async def cb_rename(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    if not session.get("video_info"):
        await callback.answer("⚠️ Sesi unduhan telah kedaluwarsa. Silakan kirimkan link kembali.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    session["state"] = "waiting_filename"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Batal Ubah Nama", callback_data="mp3:cancel_rename")]]
    )

    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass

    msg = await callback.message.answer(
        "✏️ <b>Ubah Nama File MP3:</b>\n\n"
        "Ketik nama file audio yang kamu inginkan (tanpa <code>.mp3</code>):\n"
        "<i>Contoh: Lagu Favorit - Fahmi</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    session["prompt_msg_id"] = msg.message_id
    await callback.answer()


async def cb_cancel_rename(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    session["state"] = "idle"
    try:
        await callback.message.delete()
    except Exception:
        pass
    session["prompt_msg_id"] = None
    await callback.answer("Pengubahan nama dibatalkan.")


async def cb_cancel_all(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    session["current_url"] = None
    session["video_info"] = None
    session["custom_filename"] = ""
    session["state"] = "idle"
    session["is_downloading"] = False

    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass
        session["prompt_msg_id"] = None

    # Hapus kartu informasi audio sepenuhnya saat dibatalkan
    try:
        await callback.message.delete()
    except Exception:
        pass
    session["info_msg_id"] = None

    await callback.answer("Proses unduhan dibatalkan.")


async def handle_text_mp3(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    session = get_user_session(bot_id, message.from_user.id)

    if session["state"] == "waiting_filename" and session.get("video_info"):
        clean_name = sanitize_filename(message.text, strip_ext="mp3")
        session["custom_filename"] = clean_name
        session["state"] = "idle"

        if session.get("prompt_msg_id"):
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=session["prompt_msg_id"])
            except Exception:
                pass
            session["prompt_msg_id"] = None

        try:
            await message.delete()
        except Exception:
            pass

        info = session["video_info"]
        session_id = info["session_id"]
        limit = (bot_config or {}).get("daily_limit", 0)
        quota_info = await get_user_quota_info(bot_id, message.from_user.id, limit)

        text = (
            f"🎵 <b>Informasi Audio Ditemukan:</b>\n\n"
            f"📌 <b>Judul:</b> {info['title']}\n"
            f"👤 <b>Artis/Uploader:</b> {info['uploader']}\n"
            f"⏱️ <b>Durasi:</b> {info['duration']}\n"
            f"📁 <b>Nama File:</b> <code>{clean_name}.mp3</code> <i>(Nama diubah)</i>\n"
            f"🏷️ <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Silakan pilih kualitas bitrate di bawah:</i>"
        )

        estimated_sizes = info.get("estimated_sizes") or {}

        # Update kartu yang sudah ada secara in-place agar tidak kedoublean
        if session.get("info_msg_id"):
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=session["info_msg_id"],
                    text=text,
                    parse_mode="HTML",
                    reply_markup=build_mp3_keyboard(session_id, estimated_sizes)
                )
                return
            except Exception:
                pass

        msg = await message.answer(text, parse_mode="HTML", reply_markup=build_mp3_keyboard(session_id, estimated_sizes))
        session["info_msg_id"] = msg.message_id
        return

    await message.answer(
        "ℹ️ <b>Kirimkan link/tautan audio atau video</b> (YouTube, TikTok, Soundcloud, Instagram, dll) yang ingin kamu unduh.",
        parse_mode="HTML"
    )


async def cb_download_mp3(callback: CallbackQuery, bot: Bot, bot_id: int = 0, bot_info: dict = None, bot_config: dict = None):
    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, callback.from_user.id, limit)

    if not quota_info["allowed"]:
        await callback.answer("Kuota habis atau akun diblokir!", show_alert=True)
        await callback.message.answer(quota_info["alert_message"], parse_mode="HTML")
        return

    parts = callback.data.split(":")
    bitrate = parts[2]
    session = get_user_session(bot_id, callback.from_user.id)

    url = session.get("current_url")
    info = session.get("video_info")

    if not url or not info:
        await callback.answer("⚠️ Sesi unduhan telah kedaluwarsa. Silakan kirimkan link kembali.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    # Validasi awal perkiraan ukuran file terhadap batas upload Telegram Bot API (50 MB)
    est_sizes = info.get("estimated_sizes") or {}
    try:
        target_br = int(bitrate)
    except Exception:
        target_br = 0
    est_mb = est_sizes.get(target_br)
    if est_mb and est_mb > 55.0:
        await callback.answer(
            f"⚠️ Audio kualitas {bitrate} kbps diperkirakan ~{est_mb:.1f} MB!\n\n"
            f"Batas upload Telegram Bot API adalah 50 MB. Mohon pilih bitrate yang lebih rendah.",
            show_alert=True
        )
        return

    if session.get("is_downloading"):
        await callback.answer("⏳ Proses download sedang berjalan!", show_alert=True)
        return

    session["is_downloading"] = True
    await callback.answer("Memulai download MP3...")

    # LANGSUNG UBAH PESAN KARTU DAN HILANGKAN TOMBOL-TOMBOLNYA SEKETIKA!
    try:
        await callback.message.edit_text(
            f"⏳ <b>Tahap 1/2: Mengunduh & Mengonversi Audio ({bitrate} kbps)...</b> 🎧\n\n"
            f"📌 <b>Judul:</b> {info['title']}\n"
            f"<i>Sedang mengambil audio dan mengekstrak ke format MP3...</i>",
            parse_mode="HTML",
            reply_markup=None # HAPUS SEMUA TOMBOL!
        )
    except Exception:
        pass

    # Kirim chat action 'upload_document' di Telegram
    try:
        await bot.send_chat_action(chat_id=callback.from_user.id, action="upload_document")
    except Exception:
        pass

    user_dir = os.path.join(STORAGE_DIR, f"mp3_{bot_id}_{callback.from_user.id}")
    os.makedirs(user_dir, exist_ok=True)

    final_name = session.get("custom_filename") or sanitize_filename(info["title"])
    output_template = os.path.join(user_dir, f"{final_name}.%(ext)s")
    final_mp3 = os.path.join(user_dir, f"{final_name}.mp3")

    bot_username = (bot_info or {}).get("username", "mp3_bot")
    user_name_str = f"{callback.from_user.first_name or ''} (@{callback.from_user.username or 'noname'})".strip()

    def run_download():
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': bitrate,
            }],
            'noplaylist': True,
            'socket_timeout': 30,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.wait_for(asyncio.to_thread(run_download), timeout=240.0)

        if not os.path.exists(final_mp3):
            mp3_files = [f for f in os.listdir(user_dir) if f.endswith(".mp3")]
            if mp3_files:
                final_mp3 = os.path.join(user_dir, mp3_files[0])
            else:
                raise Exception("File MP3 tidak ditemukan setelah proses konversi.")

        file_size_mb = os.path.getsize(final_mp3) / (1024 * 1024)
        if file_size_mb > 49.5:
            await callback.message.edit_text(
                f"⚠️ Ukuran file ({file_size_mb:.1f} MB) melebihi batas upload Telegram Bot API (50 MB).\n"
                f"Silakan coba lagu lain atau video dengan durasi lebih pendek.",
                parse_mode="HTML"
            )
            cleanup_user_dir(user_dir)
            return

        # Tahap 2: Update status kartu bahwa audio sedang diunggah ke Telegram
        try:
            await callback.message.edit_text(
                f"📤 <b>Tahap 2/2: Mengunggah Audio ke Telegram...</b> 🚀\n\n"
                f"📌 <b>Judul:</b> {final_name}\n"
                f"🎧 <b>Bitrate:</b> {bitrate} kbps | 📦 <b>Ukuran:</b> {file_size_mb:.1f} MB\n\n"
                f"<i>Sedang mengirimkan file audio ke obrolan Anda, mohon tunggu sebentar...</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        try:
            await bot.send_chat_action(chat_id=callback.from_user.id, action="upload_document")
        except Exception:
            pass

        audio_file = FSInputFile(final_mp3, filename=f"{final_name}.mp3")
        updated_quota = await get_user_quota_info(bot_id, callback.from_user.id, limit)

        caption = (
            f"🎵 <b>{final_name}</b>\n"
            f"🎧 Kualitas: {bitrate} kbps | 📦 Ukuran: {file_size_mb:.1f} MB\n"
            f"📊 <b>Sisa Kuota:</b> <code>{updated_quota['quota_text']}</code>"
        )

        await bot.send_audio(
            chat_id=callback.from_user.id,
            audio=audio_file,
            title=final_name,
            performer=info.get("uploader", "Bot"),
            caption=caption,
            parse_mode="HTML",
            request_timeout=300
        )

        # HAPUS KARTU INFORMASI AUDIO HANYA SETELAH AUDIO BERHASIL TERKIRIM
        try:
            await callback.message.delete()
        except Exception:
            pass
        session["info_msg_id"] = None

        await increment_user_ops(bot_id, callback.from_user.id)
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="download_mp3",
            status="success",
            details=f"File: {final_name}.mp3 ({bitrate}k, {file_size_mb:.1f} MB)",
            user_telegram_id=callback.from_user.id,
            user_name=user_name_str
        )

        cleanup_user_dir(user_dir)
        session["current_url"] = None
        session["video_info"] = None
        session["custom_filename"] = ""

    except asyncio.TimeoutError:
        cleanup_user_dir(user_dir)
        try:
            await callback.message.edit_text(
                "⏱️ <b>Waktu Unduhan Habis (Timeout)!</b>\n\n"
                "Server audio membutuhkan waktu terlalu lama (melebihi batas waktu maksimal). Silakan coba kembali atau pilih bitrate yang lebih rendah.",
                parse_mode="HTML"
            )
        except Exception:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="⏱️ <b>Waktu Unduhan Habis (Timeout)!</b>\n\nPengunduhan audio membutuhkan waktu terlalu lama. Silakan coba kembali.",
                parse_mode="HTML"
            )
    except Exception as e:
        cleanup_user_dir(user_dir)
        err_msg = str(e)[:250]
        try:
            await callback.message.edit_text(
                f"❌ <b>Terjadi kesalahan saat download MP3:</b>\n<code>{err_msg}</code>\n\n"
                f"<i>Silakan coba kirim ulang tautan atau pilih bitrate lain.</i>",
                parse_mode="HTML"
            )
        except Exception:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text=f"❌ <b>Terjadi kesalahan saat download MP3:</b>\n<code>{err_msg}</code>",
                parse_mode="HTML"
            )
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="download_mp3",
            status="error",
            details=f"Error: {str(e)[:200]}",
            user_telegram_id=callback.from_user.id,
            user_name=user_name_str
        )
    finally:
        session["is_downloading"] = False


async def cmd_vip_status(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    """Menampilkan detail akun, tanggal beli VIP, tanggal expired, dan sisa hari/waktu."""
    await track_or_update_telegram_user(
        bot_id=bot_id,
        telegram_id=message.from_user.id,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=message.from_user.username
    )

    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, message.from_user.id, limit)
    vip_info = quota_info.get("vip_info", {})
    user_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip() or "User"

    if vip_info.get("is_vip"):
        if vip_info.get("is_lifetime"):
            detail = (
                "👑 <b>STATUS KEANGGOTAAN: VIP LIFETIME (GLOBAL)</b>\n"
                "─────────────────────────\n"
                f"👤 <b>Nama:</b> {user_name}\n"
                f"🆔 <b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
                f"⭐ <b>Tipe Akses:</b> VIP Permanen (Seluruh Bot Hub)\n"
                f"♾️ <b>Masa Berlaku:</b> Selamanya (Tanpa Batas Waktu)\n"
                f"📊 <b>Akses Kuota:</b> Bebas Akses Tanpa Batas Kuota"
            )
        else:
            detail = (
                "👑 <b>STATUS KEANGGOTAAN: VIP MEMBER AKTIF (GLOBAL)</b>\n"
                "─────────────────────────\n"
                f"👤 <b>Nama:</b> {user_name}\n"
                f"🆔 <b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
                f"⭐ <b>Tipe Akses:</b> VIP Berdurasi (Seluruh Bot Hub)\n"
                f"📅 <b>Tanggal Beli / Aktif:</b> <code>{vip_info['started_str']}</code>\n"
                f"⌛ <b>Tanggal Expired:</b> <code>{vip_info['until_str']}</code>\n"
                f"⏳ <b>Sisa Masa Aktif:</b> <b>{vip_info['remaining_text']}</b>\n"
                f"📊 <b>Akses Kuota:</b> Bebas Akses Tanpa Batas Kuota"
            )
    elif vip_info.get("is_expired"):
        detail = (
            "⚠️ <b>STATUS KEANGGOTAAN: VIP KEDALUWARSA</b>\n"
            "─────────────────────────\n"
            f"👤 <b>Nama:</b> {user_name}\n"
            f"🆔 <b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
            f"⌛ <b>Berakhir Pada:</b> <code>{vip_info['until_str']}</code>\n"
            f"📊 <b>Status Kuota Saat Ini:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Masa VIP Anda telah habis. Hubungi admin untuk memperpanjang paket VIP Anda!</i>"
        )
    else:
        detail = (
            "👤 <b>STATUS KEANGGOTAAN: PENGGUNA STANDAR</b>\n"
            "─────────────────────────\n"
            f"👤 <b>Nama:</b> {user_name}\n"
            f"🆔 <b>Telegram ID:</b> <code>{message.from_user.id}</code>\n"
            f"📊 <b>Status Kuota Saat Ini:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Ingin akses tanpa batas kuota di semua bot? Hubungi admin untuk upgrade ke <b>VIP Member</b>!</i>"
        )

    await message.answer(detail, parse_mode="HTML")


async def handle_unsupported_media(message: Message):
    """Fallback handler jika user mengirim video, foto, audio, stiker atau dokumen ke bot MP3."""
    await message.answer(
        "ℹ️ <b>Format Pesan Tidak Sesuai:</b>\n\n"
        "Bot ini khusus melayani pengunduhan lagu dari <b>Tautan / Link URL</b> (YouTube, TikTok, Soundcloud, Instagram, dll).\n\n"
        "Silakan salin dan kirimkan tautan audio/video yang ingin kamu unduh menjadi MP3.",
        parse_mode="HTML"
    )


def get_router() -> Router:
    router = Router()
    router.message.register(cmd_start, F.text == "/start")
    router.message.register(cmd_reset, F.text == "/reset")
    router.message.register(cmd_vip_status, F.text.in_({"/vip", "/status", "/kuota", "/limit"}))
    router.message.register(handle_url, F.text.regexp(r'https?://[^\s]+'))
    router.callback_query.register(cb_rename, F.data.startswith("mp3:rename:"))
    router.callback_query.register(cb_cancel_rename, F.data == "mp3:cancel_rename")
    router.callback_query.register(cb_cancel_all, F.data.startswith("mp3:cancel:"))
    router.callback_query.register(cb_download_mp3, F.data.startswith("mp3:dl:"))
    router.message.register(handle_text_mp3, F.text & ~F.text.startswith("/"))
    router.message.register(handle_unsupported_media, F.photo | F.document | F.video | F.audio | F.voice | F.sticker | F.animation)
    return router
