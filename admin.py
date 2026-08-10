import os
import json
import hashlib
import secrets
import logging
from datetime import datetime, timezone
from collections import deque

from aiohttp import web
from dotenv import load_dotenv

import database

load_dotenv()
logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_PORT = int(os.getenv("PORT", "8080"))
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# ─── Activity Log (in-memory ring buffer) ─────────────────────────────────────

MAX_LOG_ENTRIES = 200
_activity_log = deque(maxlen=MAX_LOG_ENTRIES)


def log_activity(event_type, message, chat_id=None, url=None):
    """
    Log an activity event. Called from main.py during stock checks.
    event_type: 'in_stock', 'out_of_stock', 'blocked', 'error', 'check', 'info'
    """
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "message": message,
        "chat_id": str(chat_id) if chat_id else None,
        "url": url,
    }
    _activity_log.appendleft(entry)


# ─── Auth Tokens ──────────────────────────────────────────────────────────────

_valid_tokens = set()


def _generate_token():
    token = secrets.token_hex(32)
    _valid_tokens.add(token)
    return token


def _verify_token(request):
    # Check Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        return token in _valid_tokens
    # Check cookie
    token = request.cookies.get("admin_token", "")
    return token in _valid_tokens


# ─── Shared State (set by main.py) ───────────────────────────────────────────

_shared_state = {
    "bot_start_time": None,
    "last_check_time": None,
    "check_interval": 120,
    "proxy_count": 0,
}


def set_shared_state(key, value):
    _shared_state[key] = value


# ─── Route Handlers ──────────────────────────────────────────────────────────

async def handle_index(request):
    """Serve the admin dashboard HTML."""
    html_path = os.path.join(TEMPLATES_DIR, "admin.html")
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Admin template not found", status=500)


async def handle_login(request):
    """Authenticate and return a token."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    password = data.get("password", "")
    if password == ADMIN_PASSWORD:
        token = _generate_token()
        resp = web.json_response({"success": True, "token": token})
        resp.set_cookie("admin_token", token, httponly=True, samesite="Strict", max_age=86400)
        return resp
    return web.json_response({"error": "Invalid password"}, status=401)


async def handle_logout(request):
    """Invalidate the token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        _valid_tokens.discard(auth[7:])
    token = request.cookies.get("admin_token", "")
    _valid_tokens.discard(token)
    resp = web.json_response({"success": True})
    resp.del_cookie("admin_token")
    return resp


async def handle_stats(request):
    """Return bot statistics."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    stats = database.get_stats()
    now = datetime.now(timezone.utc)

    uptime_seconds = 0
    if _shared_state["bot_start_time"]:
        uptime_seconds = int((now - _shared_state["bot_start_time"]).total_seconds())

    last_check_ago = None
    if _shared_state["last_check_time"]:
        last_check_ago = int((now - _shared_state["last_check_time"]).total_seconds())

    return web.json_response({
        "status": "running",
        "uptime_seconds": uptime_seconds,
        "total_users": stats["total_users"],
        "total_urls": stats["total_urls"],
        "check_interval": _shared_state["check_interval"],
        "last_check_ago": last_check_ago,
        "proxy_count": _shared_state["proxy_count"],
    })


async def handle_users(request):
    """Return all users with their URLs and stock states."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    user_data = database.get_full_user_data()
    return web.json_response({"users": user_data})


async def handle_logs(request):
    """Return recent activity log entries."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    limit = int(request.query.get("limit", "50"))
    entries = list(_activity_log)[:limit]
    return web.json_response({"logs": entries})


async def handle_remove_url(request):
    """Admin action: remove a URL from a user's watchlist."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    chat_id = data.get("chat_id")
    url = data.get("url")

    if not chat_id or not url:
        return web.json_response({"error": "chat_id and url required"}, status=400)

    if database.remove_url(int(chat_id), url):
        log_activity("info", f"Admin removed URL from user {chat_id}", chat_id=chat_id, url=url)
        return web.json_response({"success": True})
    return web.json_response({"error": "URL not found"}, status=404)


async def handle_remove_user(request):
    """Admin action: remove a user entirely."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    chat_id = data.get("chat_id")
    if not chat_id:
        return web.json_response({"error": "chat_id required"}, status=400)

    if database.remove_user(int(chat_id)):
        log_activity("info", f"Admin removed user {chat_id}", chat_id=chat_id)
        return web.json_response({"success": True})
    return web.json_response({"error": "User not found"}, status=404)


async def handle_clear_logs(request):
    """Admin action: clear the activity log."""
    if not _verify_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    _activity_log.clear()
    return web.json_response({"success": True})


# ─── User Dashboard Routes ───────────────────────────────────────────────────

async def handle_user_dashboard(request):
    """Serve the public user dashboard HTML."""
    html_path = os.path.join(TEMPLATES_DIR, "dashboard.html")
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Dashboard template not found", status=500)


async def handle_user_data(request):
    """Return a user's watchlist data, authenticated by user token."""
    token = request.query.get("token", "")
    if not token:
        return web.json_response({"error": "Token required"}, status=400)

    chat_id = database.verify_user_token(token)
    if chat_id is None:
        return web.json_response({"error": "Invalid or expired token"}, status=401)

    items = database.get_user_dashboard_data(chat_id)
    check_interval = _shared_state.get("check_interval", 120)

    last_check_ago = None
    if _shared_state["last_check_time"]:
        now = datetime.now(timezone.utc)
        last_check_ago = int((now - _shared_state["last_check_time"]).total_seconds())

    return web.json_response({
        "chat_id": chat_id,
        "items": items,
        "total": len(items),
        "in_stock_count": sum(1 for i in items if i["in_stock"] is True),
        "out_of_stock_count": sum(1 for i in items if i["in_stock"] is False),
        "check_interval": check_interval,
        "last_check_ago": last_check_ago,
    })


# ─── App Factory ──────────────────────────────────────────────────────────────

def create_admin_app():
    """Create and return the aiohttp application."""
    app = web.Application()

    # Admin routes
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/users", handle_users)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_post("/api/remove-url", handle_remove_url)
    app.router.add_post("/api/remove-user", handle_remove_user)
    app.router.add_post("/api/clear-logs", handle_clear_logs)

    # User dashboard routes (public, token-based auth)
    app.router.add_get("/dashboard", handle_user_dashboard)
    app.router.add_get("/api/user/data", handle_user_data)

    return app


async def start_admin_server():
    """Start the admin web server. Call from the main asyncio loop."""
    app = create_admin_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", ADMIN_PORT)
    await site.start()
    logger.info(f"🌐 Admin panel running on http://0.0.0.0:{ADMIN_PORT}")
    log_activity("info", f"Admin panel started on port {ADMIN_PORT}")
    return runner

