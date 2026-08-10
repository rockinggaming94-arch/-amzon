import json
import os
import tempfile
import logging

logger = logging.getLogger(__name__)

DB_FILE = 'database.json'

# ─── File Locking (cross-platform) ───────────────────────────────────────────

def _lock_file(f):
    """Acquire an exclusive lock on the file."""
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except (ImportError, OSError):
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            pass  # Fallback: no locking available


def _unlock_file(f):
    """Release the lock on the file."""
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except (ImportError, OSError):
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass


# ─── Core DB Operations ──────────────────────────────────────────────────────

def load_db():
    """Load the database from disk. Returns the full DB dict."""
    if not os.path.exists(DB_FILE):
        return {"users": {}, "stock_state": {}}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            _lock_file(f)
            data = json.load(f)
            _unlock_file(f)
        # Migration: handle old format (flat dict of chat_id -> urls)
        if "users" not in data:
            migrated = {"users": data, "stock_state": {}}
            save_db(migrated)
            return migrated
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading database: {e}")
        return {"users": {}, "stock_state": {}}


def save_db(data):
    """Atomically save the database to disk."""
    try:
        # Write to a temp file in the same directory, then atomically replace
        dir_name = os.path.dirname(os.path.abspath(DB_FILE))
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='db_')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DB_FILE)
    except Exception as e:
        logger.error(f"Error saving database: {e}")
        # Clean up temp file if replace failed
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ─── URL Management ──────────────────────────────────────────────────────────

def add_url(chat_id, url):
    """Add a URL to a user's watchlist. Returns True if added, False if duplicate."""
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str not in db["users"]:
        db["users"][chat_id_str] = []

    if url not in db["users"][chat_id_str]:
        db["users"][chat_id_str].append(url)
        save_db(db)
        return True
    return False


def remove_url(chat_id, url):
    """Remove a URL from a user's watchlist. Also cleans up stock state."""
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str in db["users"] and url in db["users"][chat_id_str]:
        db["users"][chat_id_str].remove(url)
        # Clean up stock state for this user+url
        state_key = f"{chat_id_str}:{url}"
        db["stock_state"].pop(state_key, None)
        # Remove user entry if no URLs left
        if not db["users"][chat_id_str]:
            del db["users"][chat_id_str]
        save_db(db)
        return True
    return False


def clear_all_urls(chat_id):
    """Remove all URLs for a user. Returns count of removed URLs."""
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str not in db["users"]:
        return 0
    count = len(db["users"][chat_id_str])
    # Clean up stock state
    urls = db["users"][chat_id_str]
    for url in urls:
        state_key = f"{chat_id_str}:{url}"
        db["stock_state"].pop(state_key, None)
    del db["users"][chat_id_str]
    save_db(db)
    return count


def get_urls(chat_id):
    """Get all URLs for a specific user."""
    db = load_db()
    return db["users"].get(str(chat_id), [])


def get_all_users_and_urls():
    """Get the full users dict {chat_id_str: [urls]}."""
    db = load_db()
    return db["users"]


# ─── Stock State Management ──────────────────────────────────────────────────

def get_stock_state(chat_id, url):
    """
    Get the last known stock state for a user+url pair.
    Returns True (was in stock), False (was out of stock), or None (never checked).
    """
    db = load_db()
    key = f"{str(chat_id)}:{url}"
    return db["stock_state"].get(key, None)


def update_stock_state(chat_id, url, in_stock):
    """Update the stock state for a user+url pair."""
    db = load_db()
    key = f"{str(chat_id)}:{url}"
    db["stock_state"][key] = in_stock
    save_db(db)


def bulk_update_stock_states(updates):
    """
    Batch update stock states to minimize disk writes.
    updates: list of (chat_id, url, in_stock) tuples
    """
    if not updates:
        return
    db = load_db()
    for chat_id, url, in_stock in updates:
        key = f"{str(chat_id)}:{url}"
        db["stock_state"][key] = in_stock
    save_db(db)


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_stats():
    """Get database statistics."""
    db = load_db()
    total_users = len(db["users"])
    total_urls = sum(len(urls) for urls in db["users"].values())
    return {"total_users": total_users, "total_urls": total_urls}


def get_all_urls_flat():
    """Get all unique URLs across all users (for batch checking)."""
    db = load_db()
    all_urls = set()
    for urls in db["users"].values():
        all_urls.update(urls)
    return list(all_urls)


# ─── Admin Panel Functions ────────────────────────────────────────────────────

def get_full_user_data():
    """
    Get all users with their URLs and stock states for the admin panel.
    Returns: { chat_id_str: [ {url, in_stock}, ... ] }
    """
    db = load_db()
    result = {}
    for chat_id_str, urls in db["users"].items():
        user_urls = []
        for url in urls:
            key = f"{chat_id_str}:{url}"
            state = db["stock_state"].get(key, None)
            user_urls.append({
                "url": url,
                "in_stock": state,
            })
        result[chat_id_str] = user_urls
    return result


def remove_user(chat_id):
    """Remove a user and all their data entirely. Returns True if removed."""
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str not in db["users"]:
        return False
    # Clean up all stock states for this user
    for url in db["users"][chat_id_str]:
        key = f"{chat_id_str}:{url}"
        db["stock_state"].pop(key, None)
    del db["users"][chat_id_str]
    save_db(db)
    return True


# ─── User Dashboard Tokens ───────────────────────────────────────────────────

import hashlib
import hmac

_TOKEN_SECRET = os.getenv("TOKEN_SECRET", "stock-bot-secret-key-2024")


def generate_user_token(chat_id):
    """
    Generate a deterministic, secure token for a user's dashboard.
    Same chat_id always produces the same token (no DB storage needed).
    """
    chat_id_str = str(chat_id)
    token = hmac.new(
        _TOKEN_SECRET.encode(),
        chat_id_str.encode(),
        hashlib.sha256
    ).hexdigest()[:24]
    return token


def verify_user_token(token):
    """
    Verify a user token and return the chat_id if valid.
    Checks all known users to find a match.
    """
    db = load_db()
    for chat_id_str in db["users"]:
        expected_token = hmac.new(
            _TOKEN_SECRET.encode(),
            chat_id_str.encode(),
            hashlib.sha256
        ).hexdigest()[:24]
        if token == expected_token:
            return chat_id_str
    return None


def get_user_dashboard_data(chat_id):
    """
    Get a user's dashboard data: their URLs with stock states.
    Returns a list of dicts: [{url, in_stock}, ...]
    """
    db = load_db()
    chat_id_str = str(chat_id)
    urls = db["users"].get(chat_id_str, [])
    result = []
    for url in urls:
        key = f"{chat_id_str}:{url}"
        state = db["stock_state"].get(key, None)
        result.append({
            "url": url,
            "in_stock": state,
        })
    return result


