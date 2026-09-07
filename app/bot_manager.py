import asyncio
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery

from app.database import get_all_bots, get_bot_by_id, log_activity
from app.handlers import img2pdf, mp3_downloader, mp4_downloader

logger = logging.getLogger("bot_manager")
logging.basicConfig(level=logging.INFO)


def get_maintenance_router(custom_msg: str = None) -> Router:
    """
    Router khusus yang membalas semua pesan saat bot dalam status STOPPED/MAINTENANCE,
    sehingga pengguna Telegram mengetahui bahwa bot sedang dalam pemeliharaan.
    """
    router = Router()
    maint_text = custom_msg or (
        "🛠️ <b>Layanan Sedang Dalam Pemeliharaan (Maintenance)</b>\n\n"
        "Mohon maaf atas ketidaknyamanannya, bot ini sedang dinonaktifkan sementara oleh administrator untuk pemeliharaan/perbaikan sistem.\n\n"
        "⏳ Silakan coba kembali beberapa saat lagi. Terima kasih atas pengertiannya! 🙏"
    )

    async def on_maintenance_message(message: Message, bot: Bot):
        await message.answer(maint_text, parse_mode="HTML")

    async def on_maintenance_callback(callback: CallbackQuery, bot: Bot):
        await callback.answer(
            "⚠️ Bot saat ini sedang dalam pemeliharaan (Maintenance). Silakan coba lagi nanti.",
            show_alert=True
        )

    router.message.register(on_maintenance_message)
    router.callback_query.register(on_maintenance_callback)
    return router


class BotInstance:
    def __init__(self, bot_id: int, token: str, bot_type: str, bot_info: dict, is_maintenance: bool = False):
        self.bot_id = bot_id
        self.token = token
        self.bot_type = bot_type
        self.bot_info = bot_info
        self.is_maintenance = is_maintenance
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.task: asyncio.Task | None = None
        self._is_stopped: bool = False

    async def start(self):
        self._is_stopped = False
        bot_session = AiohttpSession(timeout=300.0)
        self.bot = Bot(token=self.token, session=bot_session)
        self.dp = Dispatcher(storage=MemoryStorage())

        # Inject context variables into Dispatcher
        self.dp["bot_id"] = self.bot_id
        self.dp["bot_info"] = self.bot_info
        config = self.bot_info.get("config_parsed", {})
        self.dp["bot_config"] = config

        if self.is_maintenance:
            # Mode Maintenance / Stopped: respons pesan pemeliharaan
            custom_msg = config.get("custom_maintenance_msg")
            self.dp.include_router(get_maintenance_router(custom_msg))
            logger.info(f"Bot #{self.bot_id} (@{self.bot_info.get('username')}) started in [MAINTENANCE MODE].")
        else:
            # Mode Operasional Normal
            if self.bot_type == "img2pdf":
                self.dp.include_router(img2pdf.get_router())
            elif self.bot_type == "mp3":
                self.dp.include_router(mp3_downloader.get_router())
            elif self.bot_type == "mp4":
                self.dp.include_router(mp4_downloader.get_router())
            else:
                self.dp.include_router(img2pdf.get_router())
            logger.info(f"Bot #{self.bot_id} (@{self.bot_info.get('username')}) started in [NORMAL MODE].")

        # Start polling in background
        async def run_polling():
            try:
                await self.bot.delete_webhook(drop_pending_updates=True)
                mode_str = "MAINTENANCE" if self.is_maintenance else "NORMAL"
                logger.info(f"Bot #{self.bot_id} polling started [{mode_str}].")
                await self.dp.start_polling(self.bot, handle_as_tasks=False)
            except asyncio.CancelledError:
                logger.info(f"Bot #{self.bot_id} polling stopped (cancelled).")
            except Exception as e:
                if not self._is_stopped:
                    logger.error(f"Error in Bot #{self.bot_id}: {e}")
            finally:
                if self.bot and self.bot.session:
                    try:
                        await self.bot.session.close()
                    except Exception:
                        pass

        self.task = asyncio.create_task(run_polling())

    async def stop(self):
        self._is_stopped = True
        logger.info(f"Stopping Bot #{self.bot_id} polling...")

        if self.dp:
            try:
                await self.dp.stop_polling()
            except Exception as e:
                logger.warning(f"Error calling dp.stop_polling for Bot #{self.bot_id}: {e}")

        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        if self.bot and self.bot.session:
            try:
                await self.bot.session.close()
            except Exception:
                pass

        self.dp = None
        self.bot = None
        self.task = None
        logger.info(f"Bot #{self.bot_id} cleanly and completely stopped.")


class BotManager:
    def __init__(self):
        self.instances: dict[int, BotInstance] = {}

    def is_running(self, bot_id: int) -> bool:
        """Mengembalikan True jika bot berjalan dalam mode operasional NORMAL."""
        instance = self.instances.get(bot_id)
        if not instance or instance._is_stopped or instance.is_maintenance:
            return False
        return instance.task is not None and not instance.task.done()

    def is_in_maintenance(self, bot_id: int) -> bool:
        """Mengembalikan True jika bot sedang dalam status MAINTENANCE / STOPPED."""
        instance = self.instances.get(bot_id)
        if not instance or instance._is_stopped:
            return False
        return instance.is_maintenance and instance.task is not None and not instance.task.done()

    def get_bot_client(self, bot_id: int) -> Bot | None:
        instance = self.instances.get(bot_id)
        if instance and not instance._is_stopped:
            return instance.bot
        return None

    async def start_bot(self, bot_data: dict, is_maintenance: bool = False) -> bool:
        bot_id = bot_data["id"]
        # Hentikan instance lama jika ada
        if bot_id in self.instances:
            await self.instances[bot_id].stop()
            del self.instances[bot_id]
            await asyncio.sleep(0.3)

        instance = BotInstance(
            bot_id=bot_id,
            token=bot_data["token"],
            bot_type=bot_data["bot_type"],
            bot_info=bot_data,
            is_maintenance=is_maintenance
        )
        try:
            await instance.start()
            self.instances[bot_id] = instance
            mode_desc = "maintenance" if is_maintenance else "normal"
            await log_activity(bot_id, bot_data.get("username", ""), "start_bot", "success", f"Bot worker started ({mode_desc})")
            return True
        except Exception as e:
            logger.error(f"Failed to start bot #{bot_id}: {e}")
            await log_activity(bot_id, bot_data.get("username", ""), "start_bot", "error", str(e))
            return False

    async def stop_bot(self, bot_id: int) -> bool:
        """Menghentikan worker secara penuh (misal saat bot dihapus)."""
        instance = self.instances.get(bot_id)
        if instance:
            await instance.stop()
            del self.instances[bot_id]
            await log_activity(bot_id, instance.bot_info.get("username", ""), "stop_bot", "success", "Bot worker stopped completely")
            return True
        return False

    async def set_bot_mode(self, bot_data: dict, maintenance: bool) -> bool:
        """Mengganti mode bot antara NORMAL dan MAINTENANCE dengan mulus."""
        return await self.start_bot(bot_data, is_maintenance=maintenance)

    async def restart_bot(self, bot_data: dict) -> bool:
        is_maint = not bool(bot_data.get("is_active", 1))
        return await self.start_bot(bot_data, is_maintenance=is_maint)

    async def load_and_start_all(self):
        bots = await get_all_bots()
        for b in bots:
            # Jika is_active == 1: start normal
            # Jika is_active == 0: start dalam maintenance mode agar bot tetap membalas pesan maintenance!
            is_maint = not bool(b.get("is_active", 1))
            logger.info(f"Loading bot #{b['id']} ({b['name']}) - Maintenance Mode: {is_maint}")
            await self.start_bot(b, is_maintenance=is_maint)

    async def stop_all(self):
        bot_ids = list(self.instances.keys())
        for bid in bot_ids:
            await self.stop_bot(bid)

    async def broadcast_message(self, recipients: list[dict], message_text: str, default_bot_id: int = None) -> dict:
        success = 0
        failed = 0
        bot_cache: dict[int, Bot] = {}

        for r in recipients:
            target_bot_id = default_bot_id or r.get("bot_id")
            if not target_bot_id:
                failed += 1
                continue

            bot = self.get_bot_client(target_bot_id)

            if not bot:
                if target_bot_id in bot_cache:
                    bot = bot_cache[target_bot_id]
                else:
                    b_data = await get_bot_by_id(target_bot_id)
                    if b_data and b_data.get("token"):
                        bot = Bot(token=b_data["token"])
                        bot_cache[target_bot_id] = bot

            if not bot:
                failed += 1
                continue

            try:
                await bot.send_message(chat_id=r["telegram_id"], text=message_text, parse_mode="HTML")
                success += 1
            except Exception as e:
                logger.warning(f"Broadcast failed to {r['telegram_id']}: {e}")
                failed += 1

            await asyncio.sleep(0.05)

        for b in bot_cache.values():
            if b.session:
                try:
                    await b.session.close()
                except Exception:
                    pass

        return {
            "total": len(recipients),
            "success": success,
            "failed": failed
        }


bot_manager = BotManager()
