import asyncio
from datetime import datetime
import io
import logging
import os
import shutil
import time
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
import img2pdf
from PIL import Image

from app.handlers.common import STORAGE_DIR, sanitize_filename
from app.database import (
    log_activity,
    track_or_update_telegram_user,
    get_user_quota_info,
    increment_user_ops,
    now_jakarta
)

logger = logging.getLogger("img2pdf")

ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif'}

user_sessions: dict[int, dict[int, dict]] = {}
user_last_quota_alert: dict[int, dict[int, float]] = {}


def get_user_session(bot_id: int, user_id: int) -> dict:
    if bot_id not in user_sessions:
        user_sessions[bot_id] = {}
    
    now = time.time()
    if user_id in user_sessions[bot_id]:
        sess = user_sessions[bot_id][user_id]
        # Jika sesi ditinggalkan (idle) lebih dari 2 jam, bersihkan storage & reset antrean
        if now - sess.get("last_activity", now) > 7200:
            user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{user_id}")
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)
            sess["images"].clear()
            sess["custom_filename"] = ""
            sess["state"] = "idle"
            sess["active_card_id"] = None
            sess["all_card_ids"].clear()
            sess["prompt_msg_id"] = None
            sess["pending_downloads"] = 0
        sess["last_activity"] = now
        return sess

    user_sessions[bot_id][user_id] = {
        "images": {},           # {message_id: file_path}
        "custom_filename": "",
        "state": "idle",
        "active_card_id": None, # ID kartu pesan aktif (loading / status card)
        "all_card_ids": set(),  # Set dari seluruh ID pesan kartu untuk pembersihan tuntas
        "prompt_msg_id": None,
        "pending_downloads": 0,
        "debounce_task": None,
        "lock": asyncio.Lock(),
        "last_activity": now
    }
    return user_sessions[bot_id][user_id]


def build_status_keyboard(has_images: bool) -> InlineKeyboardMarkup:
    if not has_images:
        return InlineKeyboardMarkup(inline_keyboard=[])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Buat PDF Sekarang", callback_data="i2p:generate")
            ],
            [
                InlineKeyboardButton(text="✏️ Atur Nama File", callback_data="i2p:edit_name"),
                InlineKeyboardButton(text="🗑️ Reset Foto", callback_data="i2p:reset")
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


async def cleanup_all_cards(bot: Bot, chat_id: int, session: dict, keep_id: int | None = None):
    """Menghapus semua kartu/pesan status bot sebelumnya secara tuntas agar tidak ada kartu yang kedoublean."""
    to_delete = set(session.get("all_card_ids", set()))
    if session.get("active_card_id"):
        to_delete.add(session["active_card_id"])
    if keep_id:
        to_delete.discard(keep_id)

    session["all_card_ids"].clear()
    if keep_id:
        session["all_card_ids"].add(keep_id)
        session["active_card_id"] = keep_id
    else:
        session["active_card_id"] = None

    for mid in to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def ensure_loading_feedback(bot: Bot, chat_id: int, user_id: int, bot_id: int, is_new_batch: bool = False):
    """
    Menampilkan animasi feedback loading seketika di obrolan Telegram.
    Menggunakan chat action 'upload_photo' + 1 loading card di bawah foto.
    """
    session = get_user_session(bot_id, user_id)

    # 1. Native chat action 'upload_photo'
    try:
        await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    except Exception:
        pass

    async with session["lock"]:
        # Jika bukan batch baru dan sudah ada kartu aktif, jangan buat kartu loading baru
        if not is_new_batch and session.get("active_card_id"):
            return

        # Bersihkan kartu-kartu lama dari batch sebelumnya
        await cleanup_all_cards(bot, chat_id, session)

        curr_count = len(session["images"])
        if curr_count > 0:
            text = f"⏳ <b>Menambahkan foto baru...</b> 📥\n<i>Total sementara: {curr_count} gambar</i>"
        else:
            text = "⏳ <b>Menerima & memproses foto...</b> 📥\n<i>Sedang menyimpan ke antrean dokumen</i>"

        try:
            msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            session["active_card_id"] = msg.message_id
            session["all_card_ids"].add(msg.message_id)
        except Exception as e:
            logger.warning(f"Gagal mengirim loading card: {e}")


async def render_status_card(bot: Bot, chat_id: int, user_id: int, bot_id: int, bot_config: dict = None, force_new: bool = False):
    """
    Me-render atau meng-edit kartu status interaktif di posisi paling bawah.
    Secara default meng-edit loading card yang sudah ada secara in-place sehingga tidak pernah flicker atau kedoublean.
    """
    session = get_user_session(bot_id, user_id)

    async with session["lock"]:
        count = len(session["images"])
        if count == 0:
            await cleanup_all_cards(bot, chat_id, session)
            return

        limit = (bot_config or {}).get("daily_limit", 0)
        quota_info = await get_user_quota_info(bot_id, user_id, limit)

        default_name = f"Doc_{now_jakarta().strftime('%Y%m%d_%H%M%S')}"
        display_name = session["custom_filename"] or default_name

        text = (
            f"🖼️ <b>Koleksi Foto Berhasil Disimpan!</b>\n\n"
            f"📊 <b>Total foto tersimpan:</b> {count} gambar\n"
            f"📁 <b>Nama File PDF:</b> <code>{display_name}.pdf</code>\n"
            f"🏷️ <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n\n"
            f"<i>Kirim foto lagi untuk menambahkan, atau klik tombol di bawah untuk membuat PDF:</i>"
        )
        markup = build_status_keyboard(True)

        target_id = session.get("active_card_id")
        success = False

        if target_id and not force_new:
            try:
                # SEAMLESS IN-PLACE EDIT: Mengubah loading card langsung menjadi kartu interaktif
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=target_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                success = True
            except Exception as e:
                if "message is not modified" in str(e).lower():
                    success = True
                else:
                    success = False

        if not success:
            await cleanup_all_cards(bot, chat_id, session)
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
                session["active_card_id"] = msg.message_id
                session["all_card_ids"].add(msg.message_id)
            except Exception as e:
                logger.error(f"Gagal mengirim status card: {e}")


def schedule_debounced_card_update(bot: Bot, chat_id: int, user_id: int, bot_id: int, bot_config: dict = None):
    session = get_user_session(bot_id, user_id)

    if session.get("debounce_task") and not session["debounce_task"].done():
        session["debounce_task"].cancel()

    async def _runner():
        try:
            # Tunggu 1.5 detik agar semua foto dalam batch/album selesai terkirim
            await asyncio.sleep(1.5)
            # Pastikan tidak ada proses download yang masih tertunda
            while session.get("pending_downloads", 0) > 0:
                await asyncio.sleep(0.3)
            # Render kartu dengan shield agar tidak terputus di tengah proses
            await asyncio.shield(render_status_card(bot, chat_id, user_id, bot_id, bot_config))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in debounced card update: {e}")
        finally:
            if session.get("debounce_task") == asyncio.current_task():
                session["debounce_task"] = None

    session["debounce_task"] = asyncio.create_task(_runner())


async def notify_quota_exceeded(message: Message, bot_id: int, user_id: int, alert_text: str):
    """Mencegah spam kartu teks ganda jika user mengirim batch/album foto bersamaan."""
    now = time.time()
    last = user_last_quota_alert.get(bot_id, {}).get(user_id, 0)
    if now - last > 4.0:
        if bot_id not in user_last_quota_alert:
            user_last_quota_alert[bot_id] = {}
        user_last_quota_alert[bot_id][user_id] = now
        await message.answer(alert_text, parse_mode="HTML")


async def cmd_start(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    # Track user
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
    await cleanup_all_cards(bot, message.chat.id, session)

    config = bot_config or {}
    base_start = config.get(
        "custom_start_msg",
        "👋 <b>Halo! Selamat datang di Image to PDF Bot.</b>\n\n"
        "Kirimkan satu atau beberapa foto/gambar ke bot ini, lalu saya akan menyusunnya menjadi 1 dokumen PDF rapi!\n\n"
        "✨ <i>Fitur:</i>\n"
        "• Penggabungan banyak foto sekaligus tanpa spam\n"
        "• Indikator animasi pemrosesan instan\n"
        "• Validasi ketat khusus format gambar (JPG, PNG, WEBP, BMP)\n"
        "• Tombol selalu muncul di posisi paling bawah\n"
        "• Bisa custom nama file PDF sesuai keinginanmu!"
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
                f"♾️ <b>Akses:</b> Kuota Konversi Tanpa Batas"
            )
    elif vip_info.get("is_expired"):
        vip_text = (
            f"📊 <b>Status Kuota:</b> <code>{quota_info['quota_text']}</code>\n"
            f"⚠️ <i>Masa VIP Anda telah kedaluwarsa pada {vip_info['until_str']}. Hubungi admin untuk perpanjangan!</i>"
        )
    else:
        vip_text = f"📊 <b>Batas Kuota Anda:</b> <code>{quota_info['quota_text']}</code>"

    start_msg = f"{base_start}\n\n{vip_text}"

    custom_btns = config.get("custom_buttons", [])
    kb = build_start_keyboard(custom_btns)

    await message.answer(start_msg, parse_mode="HTML", reply_markup=kb)


async def cmd_reset(message: Message, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, message.from_user.id)
    user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{message.from_user.id}")
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir, ignore_errors=True)

    await cleanup_all_cards(bot, message.chat.id, session)

    session["images"].clear()
    session["custom_filename"] = ""
    session["state"] = "idle"

    await message.answer("🔄 Sesi berhasil direset. Silakan kirimkan foto baru.", parse_mode="HTML")


async def handle_photo(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    # Track user
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

    session = get_user_session(bot_id, message.from_user.id)

    # Deteksi apakah ini awal dari batch foto baru
    is_new_batch = (
        session.get("pending_downloads", 0) == 0 and
        (session.get("debounce_task") is None or session["debounce_task"].done())
    )
    session["pending_downloads"] += 1

    # Tampilkan feedback loading seketika di posisi paling bawah
    await ensure_loading_feedback(bot, message.chat.id, message.from_user.id, bot_id, is_new_batch=is_new_batch)

    user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{message.from_user.id}")
    os.makedirs(user_dir, exist_ok=True)

    photo = message.photo[-1]
    file_name = f"{message.message_id:010d}_{photo.file_unique_id}.jpg"
    file_path = os.path.join(user_dir, file_name)

    try:
        await bot.download(photo, destination=file_path)
        session["images"][message.message_id] = file_path
    except Exception as e:
        logger.error(f"Gagal download foto: {e}")
    finally:
        session["pending_downloads"] = max(0, session["pending_downloads"] - 1)

    schedule_debounced_card_update(bot, message.chat.id, message.from_user.id, bot_id, bot_config)


async def handle_document(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    doc = message.document
    filename = doc.file_name or "file"
    ext = os.path.splitext(filename)[1].lower()

    # 1. Validasi: Tolak jika file sudah berformat PDF
    if ext == ".pdf":
        await message.answer(
            "⚠️ <b>File yang Anda kirim sudah berupa PDF!</b>\n\n"
            f"File <code>{filename}</code> sudah dalam format PDF.\n"
            "Bot ini khusus digunakan untuk menggabungkan <b>Foto / Gambar</b> (JPG, PNG, WEBP) menjadi dokumen PDF baru.",
            parse_mode="HTML"
        )
        return

    # 2. Validasi Ketat: Hanya izinkan ekstensi dan tipe gambar yang valid
    is_image_mime = bool(doc.mime_type and doc.mime_type.startswith("image/"))
    is_image_ext = ext in ALLOWED_IMAGE_EXTENSIONS

    if not (is_image_mime or is_image_ext):
        await message.answer(
            f"❌ <b>Format File Tidak Didukung!</b>\n\n"
            f"File yang dikirim: <code>{filename}</code> (tipe: <code>{ext or 'tidak diketahui'}</code>)\n\n"
            f"⚠️ Bot ini <b>hanya mengonversi Gambar ke PDF</b>, bukan dokumen lain (seperti Word, Excel, ZIP, MP3, MP4, dsb).\n\n"
            f"📸 <b>Format gambar yang diterima:</b>\n"
            f"• <b>JPG / JPEG</b>\n"
            f"• <b>PNG</b>\n"
            f"• <b>WEBP</b>\n"
            f"• <b>BMP</b>\n"
            f"• <b>TIFF</b>\n\n"
            f"<i>Silakan kirimkan file foto/gambar yang sesuai.</i>",
            parse_mode="HTML"
        )
        return

    # Track user
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

    session = get_user_session(bot_id, message.from_user.id)

    is_new_batch = (
        session.get("pending_downloads", 0) == 0 and
        (session.get("debounce_task") is None or session["debounce_task"].done())
    )
    session["pending_downloads"] += 1

    # Tampilkan feedback loading seketika di posisi paling bawah
    await ensure_loading_feedback(bot, message.chat.id, message.from_user.id, bot_id, is_new_batch=is_new_batch)

    user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{message.from_user.id}")
    os.makedirs(user_dir, exist_ok=True)

    file_name = f"{message.message_id:010d}_{doc.file_unique_id}{ext or '.jpg'}"
    file_path = os.path.join(user_dir, file_name)

    try:
        await bot.download(doc, destination=file_path)

        # 3. Validasi Integritas Gambar Menggunakan PIL
        try:
            with Image.open(file_path) as im:
                im.verify()
        except Exception:
            if os.path.exists(file_path):
                os.remove(file_path)
            # Bersihkan loading card jika rusak
            await cleanup_all_cards(bot, message.chat.id, session)
            await message.answer(
                f"⚠️ <b>File Rusak atau Bukan Gambar Valid!</b>\n\n"
                f"File <code>{filename}</code> tidak dapat dibaca sebagai format gambar yang valid.",
                parse_mode="HTML"
            )
            return

        session["images"][message.message_id] = file_path
    except Exception as e:
        logger.error(f"Gagal download dokumen gambar: {e}")
    finally:
        session["pending_downloads"] = max(0, session["pending_downloads"] - 1)

    schedule_debounced_card_update(bot, message.chat.id, message.from_user.id, bot_id, bot_config)


async def handle_unsupported_media(message: Message):
    """Fallback handler jika user mengirim video, audio, atau stiker ke bot Image to PDF."""
    await message.answer(
        "ℹ️ <b>Format Tidak Sesuai:</b>\n\n"
        "Bot ini khusus melayani pembuatan <b>Dokumen PDF dari Foto / Gambar</b> (JPG, PNG, WEBP).\n\n"
        "Untuk download audio MP3 atau video MP4, silakan gunakan bot yang sesuai.",
        parse_mode="HTML"
    )


async def cb_edit_name(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    if not session.get("images"):
        await callback.answer("⚠️ Sesi foto telah kedaluwarsa atau antrean kosong. Silakan kirimkan foto kembali.", show_alert=True)
        return

    session["state"] = "waiting_filename"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Batal Ubah Nama", callback_data="i2p:cancel_edit")]]
    )

    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass

    msg = await callback.message.answer(
        "✏️ <b>Ubah Nama File PDF:</b>\n\n"
        "Silakan ketik nama file yang kamu inginkan (tanpa <code>.pdf</code>):\n"
        "<i>Contoh: Laporan Kerja Praktek</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    session["prompt_msg_id"] = msg.message_id
    await callback.answer()


async def cb_cancel_edit(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    session["state"] = "idle"
    try:
        await callback.message.delete()
    except Exception:
        pass
    session["prompt_msg_id"] = None
    await callback.answer("Pengubahan nama dibatalkan.")


async def cb_reset(callback: CallbackQuery, bot: Bot, bot_id: int = 0):
    session = get_user_session(bot_id, callback.from_user.id)
    user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{callback.from_user.id}")
    shutil.rmtree(user_dir, ignore_errors=True)

    session["images"].clear()
    session["custom_filename"] = ""
    session["state"] = "idle"

    # Bersihkan kartu lama kecuali pesan callback yang akan menampilkan konfirmasi reset
    await cleanup_all_cards(bot, callback.message.chat.id, session, keep_id=callback.message.message_id)

    if session.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=session["prompt_msg_id"])
        except Exception:
            pass
        session["prompt_msg_id"] = None

    await callback.message.edit_text("🗑️ Semua foto dalam antrean telah direset.\nSilakan kirimkan foto baru.", parse_mode="HTML")
    await callback.answer("Antrean foto direset!")


async def handle_text_input(message: Message, bot: Bot, bot_id: int = 0, bot_config: dict = None):
    session = get_user_session(bot_id, message.from_user.id)

    if session["state"] == "waiting_filename":
        clean_name = sanitize_filename(message.text, strip_ext="pdf")
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

        # Update kartu yang sudah ada secara in-place dengan nama file baru
        await render_status_card(bot, message.chat.id, message.from_user.id, bot_id, bot_config)
        return

    await message.answer(
        "ℹ️ Kirimkan foto/gambar (JPG, PNG, WEBP) yang ingin dikonversi menjadi dokumen PDF.",
        parse_mode="HTML"
    )


async def cb_generate_pdf(callback: CallbackQuery, bot: Bot, bot_id: int = 0, bot_info: dict = None, bot_config: dict = None):
    limit = (bot_config or {}).get("daily_limit", 0)
    quota_info = await get_user_quota_info(bot_id, callback.from_user.id, limit)

    if not quota_info["allowed"]:
        await callback.answer("Kuota habis atau akun diblokir!", show_alert=True)
        await callback.message.answer(quota_info["alert_message"], parse_mode="HTML")
        return

    session = get_user_session(bot_id, callback.from_user.id)
    images_dict = session["images"]

    if not images_dict:
        await callback.answer("⚠️ Belum ada foto yang dikirim!", show_alert=True)
        return

    sorted_image_paths = [images_dict[mid] for mid in sorted(images_dict.keys())]

    # Kirim chat action 'upload_document' ke Telegram
    try:
        await bot.send_chat_action(chat_id=callback.from_user.id, action="upload_document")
    except Exception:
        pass

    # Step 1: Loading status
    await callback.message.edit_text(
        f"⚙️ <b>Sedang mengonversi {len(sorted_image_paths)} foto ke PDF...</b>\n"
        f"<i>1/3: Mengoptimalkan dan menyelaraskan gambar...</i>",
        parse_mode="HTML"
    )
    await callback.answer("Memulai konversi PDF...")

    user_dir = os.path.join(STORAGE_DIR, f"i2p_{bot_id}_{callback.from_user.id}")
    default_name = f"Doc_{now_jakarta().strftime('%Y%m%d_%H%M%S')}"
    final_name = session["custom_filename"] or default_name
    pdf_path = os.path.join(user_dir, f"{final_name}.pdf")

    bot_username = (bot_info or {}).get("username", "img2pdf_bot")
    user_name_str = f"{callback.from_user.first_name or ''} (@{callback.from_user.username or 'noname'})".strip()

    def process_and_render_pdf():
        processed_img_bytes = []
        for img_path in sorted_image_paths:
            if not os.path.exists(img_path):
                continue
            with Image.open(img_path) as im:
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=92)
                processed_img_bytes.append(buf.getvalue())

        if not processed_img_bytes:
            raise Exception("Tidak ada file gambar valid yang dapat dikonversi.")

        pdf_bytes = img2pdf.convert(processed_img_bytes)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return len(processed_img_bytes)

    try:
        page_count = await asyncio.wait_for(asyncio.to_thread(process_and_render_pdf), timeout=120.0)

        file_size_kb = os.path.getsize(pdf_path) / 1024
        doc_file = FSInputFile(pdf_path, filename=f"{final_name}.pdf")

        # Step 3: Kirim dokumen
        try:
            await callback.message.edit_text(
                f"📤 <b>Mengunggah dokumen PDF...</b>\n"
                f"<i>Mengirim file ke obrolan Telegram...</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        updated_quota = await get_user_quota_info(bot_id, callback.from_user.id, limit)

        caption = (
            f"🎉 <b>PDF Berhasil Dibuat!</b>\n\n"
            f"📄 <b>File:</b> <code>{final_name}.pdf</code>\n"
            f"🖼️ <b>Jumlah Foto:</b> {page_count} halaman\n"
            f"📦 <b>Ukuran:</b> {file_size_kb:.1f} KB\n"
            f"📊 <b>Sisa Kuota:</b> <code>{updated_quota['quota_text']}</code>\n\n"
            f"<i>Terima kasih telah menggunakan layanan bot kami!</i>"
        )

        try:
            await callback.message.delete()
        except Exception:
            pass

        await cleanup_all_cards(bot, callback.from_user.id, session)

        await bot.send_document(
            chat_id=callback.from_user.id,
            document=doc_file,
            caption=caption,
            parse_mode="HTML"
        )

        # Track usage in DB
        await increment_user_ops(bot_id, callback.from_user.id)
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="convert_pdf",
            status="success",
            details=f"File: {final_name}.pdf ({page_count} photos, {file_size_kb:.1f} KB)",
            user_telegram_id=callback.from_user.id,
            user_name=user_name_str
        )

        # Cleanup
        shutil.rmtree(user_dir, ignore_errors=True)
        session["images"].clear()
        session["custom_filename"] = ""
        session["state"] = "idle"
        session["active_card_id"] = None
        session["all_card_ids"].clear()

    except asyncio.TimeoutError:
        shutil.rmtree(user_dir, ignore_errors=True)
        await cleanup_all_cards(bot, callback.from_user.id, session, keep_id=callback.message.message_id)
        await callback.message.edit_text(
            "⏱️ <b>Waktu Konversi Habis (Timeout)!</b>\n\n"
            "Proses pembuatan dokumen PDF memakan waktu terlalu lama (melebihi batas waktu maksimal). Silakan coba kurangi jumlah foto sekaligus.",
            parse_mode="HTML"
        )
    except Exception as e:
        shutil.rmtree(user_dir, ignore_errors=True)
        await cleanup_all_cards(bot, callback.from_user.id, session, keep_id=callback.message.message_id)
        await callback.message.edit_text(
            f"❌ <b>Terjadi kesalahan saat membuat PDF:</b>\n<code>{str(e)}</code>",
            parse_mode="HTML"
        )
        await log_activity(
            bot_id=bot_id,
            bot_username=bot_username,
            action="convert_pdf",
            status="error",
            details=f"Error: {str(e)}",
            user_telegram_id=callback.from_user.id,
            user_name=user_name_str
        )


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


def get_router() -> Router:
    """Factory function: menghasilkan Router baru untuk setiap instance bot."""
    router = Router()
    router.message.register(cmd_start, F.text == "/start")
    router.message.register(cmd_reset, F.text == "/reset")
    router.message.register(cmd_vip_status, F.text.in_({"/vip", "/status", "/kuota", "/limit"}))
    router.message.register(handle_photo, F.photo)
    router.message.register(handle_document, F.document)
    router.callback_query.register(cb_edit_name, F.data == "i2p:edit_name")
    router.callback_query.register(cb_cancel_edit, F.data == "i2p:cancel_edit")
    router.callback_query.register(cb_reset, F.data == "i2p:reset")
    router.callback_query.register(cb_generate_pdf, F.data == "i2p:generate")
    router.message.register(handle_text_input, F.text & ~F.text.startswith("/"))
    router.message.register(handle_unsupported_media, F.video | F.audio | F.voice | F.video_note | F.animation | F.sticker)
    return router
