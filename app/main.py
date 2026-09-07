from contextlib import asynccontextmanager
import os
import platform
import time
import aiohttp
import psutil
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import (
    init_db,
    get_user_by_username,
    hash_password,
    get_all_bots,
    get_bot_by_id,
    create_bot,
    update_bot,
    delete_bot,
    set_bot_status,
    get_recent_logs,
    get_stats,
    get_all_telegram_users,
    toggle_user_unlimited,
    set_user_vip,
    toggle_user_ban,
    delete_telegram_user,
    get_broadcast_recipients,
    update_admin_profile,
    get_storage_stats,
    purge_storage_temp_files,
    get_analytics_chart_data,
    format_date_id,
    parse_datetime_flexible
)
from app.auth import create_session_token, get_current_user, require_auth, COOKIE_NAME
from app.bot_manager import bot_manager

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def format_jakarta_time(val):
    if not val:
        return "-"
    dt = parse_datetime_flexible(val)
    if not dt:
        return str(val)
    return format_date_id(dt)

templates.env.filters["jakarta_time"] = format_jakarta_time


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    # Call cpu_percent once to initialize psutil baseline
    psutil.cpu_percent(interval=None)
    await bot_manager.load_and_start_all()
    yield
    # Shutdown
    await bot_manager.stop_all()


app = FastAPI(title="Telegram Bot Hub Portal", lifespan=lifespan)


# --- AUTHENTICATION ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "user": None}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = await get_user_by_username(username.strip())
    if not user or user["password_hash"] != hash_password(password.strip()):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Username atau password salah!", "user": None},
            status_code=status.HTTP_401_UNAUTHORIZED
        )

    token = create_session_token(user["username"])
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=86400 * 7,
        samesite="lax"
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- DASHBOARD MAIN PAGE ---

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, user: dict = Depends(require_auth)):
    bots = await get_all_bots()
    for b in bots:
        b["is_running"] = bot_manager.is_running(b["id"])
        b["is_maintenance"] = bot_manager.is_in_maintenance(b["id"])

    stats = await get_stats()
    stats["active_bots"] = sum(1 for b in bots if b["is_running"])

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "bots": bots,
            "stats": stats,
            "active_page": "dashboard"
        }
    )


# --- USERS & QUOTA MANAGEMENT ---

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, bot_id: int = None, search: str = None, user: dict = Depends(require_auth)):
    bots = await get_all_bots()
    telegram_users = await get_all_telegram_users(bot_id=bot_id, search=search)

    total_users = len(telegram_users)
    active_vip_count = sum(1 for u in telegram_users if u.get("vip_info", {}).get("is_vip"))
    expired_vip_count = sum(1 for u in telegram_users if u.get("vip_info", {}).get("is_expired"))
    standard_count = total_users - active_vip_count - expired_vip_count

    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "user": user,
            "bots": bots,
            "telegram_users": telegram_users,
            "selected_bot_id": bot_id,
            "search": search,
            "total_users": total_users,
            "active_vip_count": active_vip_count,
            "expired_vip_count": expired_vip_count,
            "standard_count": standard_count,
            "active_page": "users"
        }
    )


@app.post("/api/users/{user_id}/toggle-unlimited")
async def toggle_unlimited_endpoint(user_id: int, request: Request, user: dict = Depends(require_auth)):
    await toggle_user_unlimited(user_id)
    referer = request.headers.get("referer")
    if referer and "/users" in referer:
        return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


@app.post("/api/users/{user_id}/set-vip")
async def set_vip_endpoint(
    user_id: int,
    request: Request,
    duration_type: str = Form(...),
    days: int = Form(30),
    custom_until: str = Form(""),
    extend: str = Form("true"),
    user: dict = Depends(require_auth)
):
    dt_type = duration_type
    target_days = days

    if duration_type.endswith("d") and duration_type[:-1].isdigit():
        dt_type = "days"
        target_days = int(duration_type[:-1])

    is_extend = (extend.lower() in ("true", "1", "yes", "on"))

    await set_user_vip(
        user_id=user_id,
        duration_type=dt_type,
        days=target_days,
        custom_until=custom_until.strip() or None,
        extend=is_extend
    )

    referer = request.headers.get("referer")
    if referer and "/users" in referer:
        return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


@app.post("/api/users/{user_id}/toggle-ban")
async def toggle_ban_endpoint(user_id: int, user: dict = Depends(require_auth)):
    await toggle_user_ban(user_id)
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


@app.post("/api/users/{user_id}/delete")
async def delete_user_endpoint(user_id: int, user: dict = Depends(require_auth)):
    await delete_telegram_user(user_id)
    return RedirectResponse(url="/users", status_code=status.HTTP_302_FOUND)


# --- BROADCAST SYSTEM ---

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, user: dict = Depends(require_auth)):
    bots = await get_all_bots()
    return templates.TemplateResponse(
        request=request,
        name="broadcast.html",
        context={
            "user": user,
            "bots": bots,
            "alert": None,
            "active_page": "broadcast"
        }
    )


@app.post("/api/broadcast", response_class=HTMLResponse)
async def handle_broadcast(
    request: Request,
    bot_id: str = Form(...),
    message_text: str = Form(...),
    user: dict = Depends(require_auth)
):
    bots = await get_all_bots()
    target_bot_id = int(bot_id) if bot_id != "all" else None
    recipients = await get_broadcast_recipients(target_bot_id)

    if not recipients:
        alert = {
            "type": "error",
            "title": "Tidak Ada Penerima",
            "message": "Belum ada pengguna aktif terdaftar yang cocok dengan target ini."
        }
    else:
        result = await bot_manager.broadcast_message(
            recipients=recipients,
            message_text=message_text.strip(),
            default_bot_id=target_bot_id
        )
        alert = {
            "type": "success",
            "title": "Broadcast Selesai Dikirim!",
            "message": f"Berhasil terkirim ke {result['success']} pengguna. Gagal: {result['failed']} (Total target: {result['total']})."
        }

    return templates.TemplateResponse(
        request=request,
        name="broadcast.html",
        context={
            "user": user,
            "bots": bots,
            "alert": alert,
            "active_page": "broadcast"
        }
    )


# --- ADMIN PROFILE & PASSWORD ---

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: dict = Depends(require_auth)):
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "user": user,
            "msg": None,
            "active_page": "profile"
        }
    )


@app.post("/profile", response_class=HTMLResponse)
async def profile_submit(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    user: dict = Depends(require_auth)
):
    new_username = new_username.strip()
    new_password = new_password.strip()

    if new_password:
        if new_password != confirm_password.strip():
            return templates.TemplateResponse(
                request=request,
                name="profile.html",
                context={
                    "user": user,
                    "msg": {"success": False, "text": "Konfirmasi password baru tidak cocok!"},
                    "active_page": "profile"
                }
            )
        if len(new_password) < 6:
            return templates.TemplateResponse(
                request=request,
                name="profile.html",
                context={
                    "user": user,
                    "msg": {"success": False, "text": "Password minimal harus 6 karakter!"},
                    "active_page": "profile"
                }
            )

    success, msg = await update_admin_profile(
        current_username=user["username"],
        new_username=new_username,
        new_password=new_password if new_password else None
    )

    if success:
        # Re-issue cookie if username changed
        response = templates.TemplateResponse(
            request=request,
            name="profile.html",
            context={
                "user": {"username": new_username},
                "msg": {"success": True, "text": msg},
                "active_page": "profile"
            }
        )
        if new_username != user["username"]:
            new_token = create_session_token(new_username)
            response.set_cookie(key=COOKIE_NAME, value=new_token, httponly=True, max_age=86400 * 7, samesite="lax")
        return response
    else:
        return templates.TemplateResponse(
            request=request,
            name="profile.html",
            context={
                "user": user,
                "msg": {"success": False, "text": msg},
                "active_page": "profile"
            }
        )


# --- ACTIVITY LOGS ---

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, user: dict = Depends(require_auth)):
    logs = await get_recent_logs(limit=100)
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "user": user,
            "logs": logs,
            "active_page": "logs"
        }
    )


# --- BOTS MANAGEMENT CRUD ---

@app.get("/bots/{bot_id}/edit", response_class=HTMLResponse)
async def edit_bot_page(bot_id: int, request: Request, user: dict = Depends(require_auth)):
    bot = await get_bot_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot tidak ditemukan")
    return templates.TemplateResponse(
        request=request,
        name="bot_edit.html",
        context={
            "user": user,
            "bot": bot,
            "active_page": "dashboard"
        }
    )


@app.post("/bots/{bot_id}/edit")
async def edit_bot_submit(
    bot_id: int,
    name: str = Form(...),
    token: str = Form(...),
    bot_type: str = Form(...),
    daily_limit: int = Form(0),
    default_prefix: str = Form("Document"),
    custom_start_msg: str = Form(""),
    custom_maintenance_msg: str = Form(""),
    btn_text: str = Form(""),
    btn_url: str = Form(""),
    user: dict = Depends(require_auth)
):
    bot = await get_bot_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot tidak ditemukan")

    config = bot.get("config_parsed", {})
    config["default_prefix"] = default_prefix.strip()
    config["custom_start_msg"] = custom_start_msg.strip()
    config["custom_maintenance_msg"] = custom_maintenance_msg.strip()
    config["daily_limit"] = max(0, daily_limit)

    custom_buttons = []
    if btn_text.strip() and btn_url.strip():
        custom_buttons.append({"text": btn_text.strip(), "url": btn_url.strip()})
    config["custom_buttons"] = custom_buttons

    await update_bot(bot_id, name.strip(), token.strip(), bot_type, config)

    # Reload bot with updated config
    updated_bot = await get_bot_by_id(bot_id)
    await bot_manager.restart_bot(updated_bot)

    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@app.post("/api/bots")
async def create_bot_submit(
    token: str = Form(...),
    name: str = Form(...),
    username: str = Form(...),
    bot_type: str = Form(...),
    custom_start_msg: str = Form(""),
    user: dict = Depends(require_auth)
):
    token = token.strip()
    config = {
        "custom_start_msg": custom_start_msg.strip(),
        "default_prefix": "Document" if bot_type == "img2pdf" else "Media",
        "daily_limit": 0,
        "custom_buttons": []
    }

    clean_username = username.strip().lstrip("@")
    if not clean_username:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        clean_username = data["result"].get("username", "bot")
                    else:
                        clean_username = "unknown_bot"
            except Exception:
                clean_username = "unknown_bot"

    bot_id = await create_bot(
        name=name.strip(),
        username=clean_username,
        token=token,
        bot_type=bot_type,
        config=config
    )

    new_bot = await get_bot_by_id(bot_id)
    await bot_manager.start_bot(new_bot)

    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@app.post("/api/bots/{bot_id}/toggle")
async def toggle_bot(bot_id: int, user: dict = Depends(require_auth)):
    bot = await get_bot_by_id(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot tidak ditemukan")

    try:
        is_normal = bot_manager.is_running(bot_id)
        if is_normal:
            # Dari NORMAL -> ubah ke MAINTENANCE (is_active = 0)
            await set_bot_status(bot_id, 0)
            bot["is_active"] = 0
            await bot_manager.set_bot_mode(bot, maintenance=True)
        else:
            # Dari MAINTENANCE -> ubah ke NORMAL (is_active = 1)
            await set_bot_status(bot_id, 1)
            bot["is_active"] = 1
            await bot_manager.set_bot_mode(bot, maintenance=False)
    except Exception as e:
        import logging
        logging.getLogger("main").error(f"Error in toggle_bot #{bot_id}: {e}")

    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@app.post("/api/bots/{bot_id}/delete")
async def delete_bot_submit(bot_id: int, user: dict = Depends(require_auth)):
    await bot_manager.stop_bot(bot_id)
    await delete_bot(bot_id)
    return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)


@app.get("/api/check-token")
async def check_token(token: str, user: dict = Depends(require_auth)):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"https://api.telegram.org/bot{token.strip()}/getMe", timeout=8) as resp:
                data = await resp.json()
                return JSONResponse(content=data)
        except Exception as e:
            return JSONResponse(content={"ok": False, "description": str(e)}, status_code=400)


# --- SYSTEM HEALTH & MONITORING API ---

@app.get("/api/system/health")
async def get_system_health(user: dict = Depends(require_auth)):
    try:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count(logical=True)

        # RAM
        ram = psutil.virtual_memory()
        ram_used_gb = round(ram.used / (1024 ** 3), 2)
        ram_total_gb = round(ram.total / (1024 ** 3), 2)

        # Disk
        disk = psutil.disk_usage('/')
        disk_used_gb = round(disk.used / (1024 ** 3), 2)
        disk_total_gb = round(disk.total / (1024 ** 3), 2)

        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)
        hours, rem = divmod(uptime_seconds, 3600)
        mins, _ = divmod(rem, 60)
        uptime_str = f"{hours}h {mins}m"

        return JSONResponse(content={
            "ok": True,
            "cpu": {
                "percent": cpu_percent,
                "count": cpu_count
            },
            "ram": {
                "percent": ram.percent,
                "used_gb": ram_used_gb,
                "total_gb": ram_total_gb
            },
            "disk": {
                "percent": disk.percent,
                "used_gb": disk_used_gb,
                "total_gb": disk_total_gb
            },
            "system": {
                "platform": f"{platform.system()} ({platform.release()[:20]})",
                "uptime": uptime_str
            }
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)


# --- STORAGE CACHE CLEANER API ---

@app.get("/api/storage/stats")
async def api_storage_stats(user: dict = Depends(require_auth)):
    stats = get_storage_stats()
    return JSONResponse(content={"ok": True, "stats": stats})


@app.post("/api/storage/purge")
async def api_storage_purge(user: dict = Depends(require_auth)):
    res = purge_storage_temp_files()
    return JSONResponse(content={
        "ok": True,
        "purged_bytes": res["purged_bytes"],
        "purged_items": res["purged_items"]
    })


# --- ANALYTICS CHARTS API ---

@app.get("/api/analytics/charts")
async def api_analytics_charts(user: dict = Depends(require_auth)):
    data = await get_analytics_chart_data()
    return JSONResponse(content={
        "ok": True,
        "labels": data["labels"],
        "activity_counts": data["activity_counts"],
        "bot_types": data["bot_types"]
    })
