import json
import os

DB_FILE = 'database.json'

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def add_url(chat_id, url):
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str not in db:
        db[chat_id_str] = []
    
    if url not in db[chat_id_str]:
        db[chat_id_str].append(url)
        save_db(db)
        return True
    return False

def remove_url(chat_id, url):
    db = load_db()
    chat_id_str = str(chat_id)
    if chat_id_str in db and url in db[chat_id_str]:
        db[chat_id_str].remove(url)
        save_db(db)
        return True
    return False

def get_urls(chat_id):
    db = load_db()
    return db.get(str(chat_id), [])

def get_all_users_and_urls():
    return load_db()
