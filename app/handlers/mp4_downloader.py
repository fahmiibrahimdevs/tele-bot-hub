import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    infer_effective_height,
    parse_duration_seconds,
    safe_chat_action
)
from app.database import (
    log_activity,
    track_or_update_telegram_user,
    get_user_quota_info,
    increment_user_ops
)

logger = logging.getLogger("mp4_downloader")

user_mp4_sessions: dict[int, dict[int, dict]] = {}
user_last_quota_alert_mp4: dict[int, dict[int, float]] = {}


def get_user_session(bot_id: int, user_id: int) -> dict:
    if bot_id not in user_mp4_sessions:
        user_mp4_sessions[bot_id] = {}
    
    now = time.time()
    if user_id in user_mp4_sessions[bot_id]:
        sess = user_mp4_sessions[bot_id][user_id]
        if now - sess.get("last_activity", now) > 3600 and not sess.get("is_downloading"):
            user_dir = os.path.join(STORAGE_DIR, f"mp4_{bot_id}_{user_id}")
            cleanup_user_dir(user_dir)
            sess["current_url"] = None
            sess["video_info"] = None
            sess["custom_filename"] = ""
            sess["state"] = "idle"
            sess["info_msg_id"] = None
            sess["prompt_msg_id"] = None
        sess["last_activity"] = now
        return sess

    user_mp4_sessions[bot_id][user_id] = {
        "current_url": None,
        "video_info": None,
        "custom_filename": "",
        "state": "idle",
        "info_msg_id": None,
        "prompt_msg_id": None,
        "is_downloading": False,
        "last_activity": now
    }
    return user_mp4_sessions[bot_id][user_id]


def estimate_video_sizes(info: dict) -> dict:
    """
    Menghitung perkiraan ukuran file (dalam MB) untuk masing-masing target resolusi (360p, 720p, 1080p).
    Mendukung YouTube, Facebook, Instagram, TikTok, Twitter/X, Reddit, dan platform lainnya.
    """
    formats = info.get('formats') or []
    duration = parse_duration_seconds(info.get('duration') or info.get('duration_string'))
    single_filesize = info.get('filesize') or info.get('filesize_approx') or 0

    # 1. Cek format yang belum punya filesize & bitrate, ambil Content-Length via HTTP HEAD cepat
    to_fetch = []
    for f in formats:
        sz = f.get('filesize') or f.get('filesize_approx')
        if not sz and not (f.get('tbr') or f.get('vbr')) and f.get('url'):
            to_fetch.append(f)

    if to_fetch:
        subset = to_fetch[:6]
        urls = [f.get('url') for f in subset]
        with ThreadPoolExecutor(max_workers=min(len(urls), 6)) as pool:
            lengths = list(pool.map(fetch_content_length, urls))
        for f, length in zip(subset, lengths):
            if length:
                f['filesize'] = length

    if not single_filesize and info.get('url'):
        cl = fetch_content_length(info.get('url'))
        if cl:
            single_filesize = cl

    # 2. Ukuran audio terbaik untuk stream video+audio terpisah (DASH / YouTube)
    audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    best_audio_size = 0
    if audio_formats:
        audio_formats.sort(key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), reverse=True)
        best_a = audio_formats[0]
        best_audio_size = best_a.get('filesize') or best_a.get('filesize_approx') or 0
        if not best_audio_size and best_a.get('abr') and duration:
            best_audio_size = int((best_a.get('abr') * 1000 / 8) * duration)
        elif not best_audio_size and duration:
            best_audio_size = int((128 * 1000 / 8) * duration)

    video_formats = [f for f in formats if f.get('vcodec') != 'none'] or formats
    annotated = []
    for f in video_formats:
        h = infer_effective_height(f)
        annotated.append((h, f))

    estimates = {}
    for target in [360, 720, 1080]:
        candidates = [f for h, f in annotated if h and h <= target]
        if not candidates:
            candidates = [f for h, f in annotated if h is None]
        if not candidates and annotated:
            candidates = [f for h, f in annotated]

        if candidates:
            candidates.sort(
                key=lambda f: (
                    infer_effective_height(f) or 0,
                    f.get('filesize') or f.get('filesize_approx') or 0,
                    f.get('tbr') or f.get('vbr') or 0
                ),
                reverse=True
            )
            chosen = candidates[0]
            v_size = chosen.get('filesize') or chosen.get('filesize_approx') or 0
            if not v_size and (chosen.get('vbr') or chosen.get('tbr')) and duration:
                rate = chosen.get('vbr') or chosen.get('tbr')
                v_size = int((rate * 1000 / 8) * duration)

            tot = v_size
            if chosen.get('acodec') == 'none' and best_audio_size:
                tot += best_audio_size
            if not tot and single_filesize:
                tot = single_filesize

            if tot > 0:
                estimates[target] = round(tot / (1024 * 1024), 1)
            else:
                estimates[target] = None
        elif single_filesize:
            estimates[target] = round(single_filesize / (1024 * 1024), 1)
        else:
            estimates[target] = None

    return estimates


def build_mp4_keyboard(session_id: str, estimated_sizes: dict = None) -> InlineKeyboardMarkup:
    estimated_sizes = estimated_sizes or {}

    mb_360 = estimated_sizes.get(360)
    if mb_360:
        badge_360 = f" (~{mb_360:.1f} MB)" if mb_360 <= 49.5 else f" (~{mb_360:.1f} MB ⚠️)"
    else:
        badge_360 = " (Ringan)"
    text_360 = f"🎬 360p{badge_360}"

    mb_720 = estimated_sizes.get(720)
    if mb_720:
        badge_720 = f" (~{mb_720:.1f} MB)" if mb_720 <= 49.5 else f" (~{mb_720:.1f} MB ⚠️)"
    else:
        badge_720 = " HD"
    text_720 = f"🎬 720p{badge_720}"

    mb_1080 = estimated_sizes.get(1080)
    if mb_1080:
        badge_1080 = f" (~{mb_1080:.1f} MB)" if mb_1080 <= 49.5 else f" (~{mb_1080:.1f} MB ⚠️ >50MB)"
    else:
        badge_1080 = " FHD"
    text_1080 = f"🎬 1080p{badge_1080}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text_360, callback_data=f"mp4:dl:360:{session_id}"),
                InlineKeyboardButton(text=text_720, callback_data=f"mp4:dl:720:{session_id}")
            ],
            [
                InlineKeyboardButton(text=text_1080, callback_data=f"mp4:dl:1080:{session_id}")
            ],
            [
                InlineKeyboardButton(text="✏️ Ubah Nama File (Opsional)", callback_data=f"mp4:rename:{session_id}"),
                InlineKeyboardButton(text="❌ Batal", callback_data=f"mp4:cancel:{session_id}")
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
    last = user_last_quota_alert_mp4.get(bot_id, {}).get(user_id, 0)
    if now - last > 4.0:
        if bot_id not in user_last_quota_alert_mp4:
            user_last_quota_alert_mp4[bot_id] = {}
        user_last_quota_alert_mp4[bot_id][user_id] = now
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
        "🎬 <b>Halo! Selamat datang di MP4 Video Downloader Bot.</b>\n\n"
        "Kirimkan URL/link video (YouTube, TikTok, Twitter/X, Instagram, dll), "
        "lalu pilih resolusi video yang kamu inginkan!\n\n"
        "✨ <i>Fitur:</i>\n"
        "• Pilihan resolusi: 360p, 720p HD, hingga 1080p FHD\n"
        "• Bisa custom nama file video sesukamu\n"
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

    user_dir = os.path.join(STORAGE_DIR, f"mp4_{bot_id}_{message.from_user.id}")
    shutil.rmtree(user_dir, ignore_errors=True)

    await message.answer("🔄 Sesi MP4 berhasil direset. Silakan kirimkan link baru.", parse_mode="HTML")


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
            f"<i>Mohon kirimkan <b>1 link video saja</b> per pesan agar proses konversi berjalan lancar.</i>",
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

    # 3. Validasi: Cek apakah user sedang mengunduh video lain
    if session.get("is_downloading"):
        await message.answer(
            "⏳ <b>Sedang Memproses Unduhan Video Lain!</b>\n\n"
            "Mohon tunggu hingga proses download video sebelumnya selesai sebelum mengirimkan link baru.",
            parse_mode="HTML"
        )
        return

    # Hapus kartu info lama jika ada
    if session.get("info_msg_id"):
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=session["info_msg_id"])
        except Exception:
            pass
        session["info_msg_id"] = None

    session_id = str(uuid.uuid4())[:8]

    # Kirim chat action typing di header chat (non-blocking & safe)
    asyncio.create_task(safe_chat_action(bot, message.chat.id, "typing"))

    status_msg = await message.answer("🔍 <b>Mengambil informasi video...</b>\n<i>Mohon tunggu sebentar...</i>", parse_mode="HTML")
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
        title = info.get('title', 'Video Track')
        duration = format_duration(info.get('duration'))
        uploader = info.get('uploader', 'Unknown Channel')
        estimated_sizes = estimate_video_sizes(info)

        session["current_url"] = url
        session["video_info"] = {
            "title": title,
            "duration": duration,
            "uploader": uploader,
            "session_id": session_id,
            "estimated_sizes": estimated_sizes
        }
        session["custom_filename"] = ""
        session["state"] = "idle"

        display_name = session["custom_filename"] or title

        text = (
            f"🎬 <b>Informasi Video Ditemukan:</b>\n\n"
            f"📌 <b>Judul:</b> {title}\n"
            f"👤 <b>Channel/Uploader:</b> {uploader}\n"
            f"⏱️ <b>Durasi:</b> {duration}\n"
            f"📁 <b>Nama File:</b> <code>{display_name}.mp4</code>\n"
            f"🏷️ <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Silakan pilih resolusi video atau ubah nama file:</i>"
        )

        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=build_mp4_keyboard(session_id, estimated_sizes))

    except asyncio.TimeoutError:
        session["info_msg_id"] = None
        await status_msg.edit_text(
            "⏱️ <b>Waktu Permintaan Habis (Timeout)!</b>\n\n"
            "Server platform video tidak merespons dalam 25 detik atau link terlalu lambat diproses.\n\n"
            "💡 <i>Silakan coba kirim ulang tautan atau pastikan tautan video dapat diakses publik.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        session["info_msg_id"] = None
        await status_msg.edit_text(
            f"❌ <b>Gagal Mengambil Informasi Video!</b>\n\n"
            f"<code>{str(e)[:200]}</code>\n\n"
            f"💡 <i>Pastikan tautan valid, dapat diakses publik, dan video tidak bersifat privat atau dibatasi usia.</i>",
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
        inline_keyboard=[[InlineKeyboardButton(text="❌ Batal", callback_data="mp4:cancel_rename")]]
    )
    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass

    msg = await callback.message.answer(
        "✏️ <b>Ubah Nama File Video MP4:</b>\n\n"
        "Ketik nama file video yang kamu inginkan (tanpa <code>.mp4</code>):\n"
        "<i>Contoh: Video Tutorial Praktis</i>",
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

    # Hapus kartu informasi video sepenuhnya saat dibatalkan
    try:
        await callback.message.delete()
    except Exception:
        pass
    session["info_msg_id"] = None

    await callback.answer("Proses unduhan dibatalkan.")


async def handle_text_mp4(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    session = get_user_session(bot_id, message.from_user.id)

    if session["state"] == "waiting_filename" and session.get("video_info"):
        clean_name = sanitize_filename(message.text, strip_ext="mp4")
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
            f"🎬 <b>Informasi Video Ditemukan:</b>\n\n"
            f"📌 <b>Judul:</b> {info['title']}\n"
            f"👤 <b>Channel/Uploader:</b> {info['uploader']}\n"
            f"⏱️ <b>Durasi:</b> {info['duration']}\n"
            f"📁 <b>Nama File:</b> <code>{clean_name}.mp4</code> <i>(Nama diubah)</i>\n"
            f"🏷️ <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Silakan pilih resolusi video di bawah:</i>"
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
                    reply_markup=build_mp4_keyboard(session_id, estimated_sizes)
                )
                return
            except Exception:
                pass

        msg = await message.answer(text, parse_mode="HTML", reply_markup=build_mp4_keyboard(session_id, estimated_sizes))
        session["info_msg_id"] = msg.message_id
        return

    await message.answer(
        "ℹ️ <b>Kirimkan link/tautan video</b> (YouTube, TikTok, Twitter/X, Instagram, dll) yang ingin kamu unduh.",
        parse_mode="HTML"
    )


async def cb_download_mp4(callback: CallbackQuery, bot: Bot, bot_id: int = 0, bot_info: dict = None, bot_config: dict = None):
    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, callback.from_user.id, limit)

    if not quota_info["allowed"]:
        await callback.answer("Kuota habis atau akun diblokir!", show_alert=True)
        await callback.message.answer(quota_info["alert_message"], parse_mode="HTML")
        return

    parts = callback.data.split(":")
    res_code = parts[2]
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
        target_res = int(res_code)
    except Exception:
        target_res = 0
    est_mb = est_sizes.get(target_res)
    if est_mb and est_mb > 55.0:
        await callback.answer(
            f"⚠️ Resolusi {res_code}p diperkirakan ~{est_mb:.1f} MB!\n\n"
            f"Batas upload Telegram Bot API adalah 50 MB. Mohon pilih resolusi yang lebih rendah (misal 720p atau 360p).",
            show_alert=True
        )
        return

    if session.get("is_downloading"):
        await callback.answer("⏳ Proses download video sedang berjalan!", show_alert=True)
        return

    session["is_downloading"] = True
    await callback.answer(f"Memulai download MP4 {res_code}p...")

    # LANGSUNG UBAH PESAN KARTU DAN HILANGKAN TOMBOL-TOMBOLNYA SEKETIKA!
    try:
        await callback.message.edit_text(
            f"⏳ <b>Tahap 1/2: Mengunduh Video ({res_code}p)...</b> 🎬\n\n"
            f"📌 <b>Judul:</b> {info['title']}\n"
            f"<i>Sedang mengambil dan merender file video dari server sumber...</i>",
            parse_mode="HTML",
            reply_markup=None # HAPUS SEMUA TOMBOL!
        )
    except Exception:
        pass

    # Kirim chat action 'upload_video' di Telegram
    try:
        await bot.send_chat_action(chat_id=callback.from_user.id, action="upload_video")
    except Exception:
        pass

    user_dir = os.path.join(STORAGE_DIR, f"mp4_{bot_id}_{callback.from_user.id}")
    os.makedirs(user_dir, exist_ok=True)

    final_name = session.get("custom_filename") or sanitize_filename(info["title"])
    output_template = os.path.join(user_dir, f"{final_name}.%(ext)s")
    final_mp4 = os.path.join(user_dir, f"{final_name}.mp4")

    bot_username = (bot_info or {}).get("username", "mp4_bot")
    user_name_str = f"{callback.from_user.first_name or ''} (@{callback.from_user.username or 'noname'})".strip()

    format_selector = f"bestvideo[height<={res_code}][ext=mp4]+bestaudio[ext=m4a]/best[height<={res_code}][ext=mp4]/best[height<={res_code}]/best"

    def run_download():
        ydl_opts = {
            'format': format_selector,
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
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
        await asyncio.wait_for(asyncio.to_thread(run_download), timeout=300.0)

        if not os.path.exists(final_mp4):
            mp4_files = [f for f in os.listdir(user_dir) if f.endswith(".mp4")]
            if mp4_files:
                final_mp4 = os.path.join(user_dir, mp4_files[0])
            else:
                raise Exception("File video MP4 tidak ditemukan setelah proses render.")

        file_size_mb = os.path.getsize(final_mp4) / (1024 * 1024)
        if file_size_mb > 49.5:
            await callback.message.edit_text(
                f"⚠️ Ukuran file video ({file_size_mb:.1f} MB) melebihi batas upload Telegram Bot API (50 MB).\n"
                f"Silakan coba unduh dengan resolusi lebih rendah (misal 360p atau 720p).",
                parse_mode="HTML"
            )
            cleanup_user_dir(user_dir)
            return

        # Tahap 2: Update status kartu bahwa video sedang diunggah ke Telegram
        try:
            await callback.message.edit_text(
                f"📤 <b>Tahap 2/2: Mengunggah Video ke Telegram...</b> 🚀\n\n"
                f"📌 <b>Judul:</b> {final_name}\n"
                f"📺 <b>Resolusi:</b> {res_code}p | 📦 <b>Ukuran:</b> {file_size_mb:.1f} MB\n\n"
                f"<i>Sedang mengirimkan file video ke obrolan Anda, mohon tunggu sebentar...</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        try:
            await bot.send_chat_action(chat_id=callback.from_user.id, action="upload_video")
        except Exception:
            pass

        video_file = FSInputFile(final_mp4, filename=f"{final_name}.mp4")
        updated_quota = await get_user_quota_info(bot_id, callback.from_user.id, limit)

        caption = (
            f"🎬 <b>{final_name}</b>\n"
            f"📺 Resolusi: {res_code}p | 📦 Ukuran: {file_size_mb:.1f} MB\n"
            f"📊 <b>Sisa Kuota:</b> <code>{updated_quota['quota_text']}</code>"
        )

        await bot.send_video(
            chat_id=callback.from_user.id,
            video=video_file,
            caption=caption,
            supports_streaming=True,
            parse_mode="HTML",
            request_timeout=300
        )

        # HAPUS KARTU INFORMASI VIDEO HANYA SETELAH VIDEO BERHASIL TERKIRIM
        try:
            await callback.message.delete()
        except Exception:
            pass
        session["info_msg_id"] = None

        await increment_user_ops(bot_id, callback.from_user.id)
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="download_mp4",
            status="success",
            details=f"File: {final_name}.mp4 ({res_code}p, {file_size_mb:.1f} MB)",
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
                "Server video membutuhkan waktu terlalu lama (melebihi batas waktu maksimal). Silakan coba pilih resolusi yang lebih rendah (misal 360p / 480p).",
                parse_mode="HTML"
            )
        except Exception:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text="⏱️ <b>Waktu Unduhan Habis (Timeout)!</b>\n\nPengunduhan memakan waktu terlalu lama. Silakan coba pilih resolusi yang lebih rendah.",
                parse_mode="HTML"
            )
    except Exception as e:
        cleanup_user_dir(user_dir)
        err_msg = str(e)[:250]
        try:
            await callback.message.edit_text(
                f"❌ <b>Terjadi kesalahan saat download Video:</b>\n<code>{err_msg}</code>\n\n"
                f"<i>Silakan coba kirim ulang tautan atau pilih resolusi lain.</i>",
                parse_mode="HTML"
            )
        except Exception:
            await bot.send_message(
                chat_id=callback.from_user.id,
                text=f"❌ <b>Terjadi kesalahan saat download Video:</b>\n<code>{err_msg}</code>",
                parse_mode="HTML"
            )
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="download_mp4",
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
    """Fallback handler jika user mengirim foto, dokumen, sticker, atau audio ke bot MP4."""
    await message.answer(
        "ℹ️ <b>Format Pesan Tidak Sesuai:</b>\n\n"
        "Bot ini khusus melayani pengunduhan video dari <b>Tautan / Link URL</b> (YouTube, TikTok, Twitter/X, Instagram, dll).\n\n"
        "Silakan salin dan kirimkan tautan video yang ingin kamu unduh menjadi MP4.",
        parse_mode="HTML"
    )


def get_router() -> Router:
    router = Router()
    router.message.register(cmd_start, F.text == "/start")
    router.message.register(cmd_reset, F.text == "/reset")
    router.message.register(cmd_vip_status, F.text.in_({"/vip", "/status", "/kuota", "/limit"}))
    router.message.register(handle_url, F.text.regexp(r'https?://[^\s]+'))
    router.callback_query.register(cb_rename, F.data.startswith("mp4:rename:"))
    router.callback_query.register(cb_cancel_rename, F.data == "mp4:cancel_rename")
    router.callback_query.register(cb_cancel_all, F.data.startswith("mp4:cancel:"))
    router.callback_query.register(cb_download_mp4, F.data.startswith("mp4:dl:"))
    router.message.register(handle_text_mp4, F.text & ~F.text.startswith("/"))
    router.message.register(handle_unsupported_media, F.photo | F.document | F.video | F.audio | F.voice | F.sticker | F.animation)
    return router
