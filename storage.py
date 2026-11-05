import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE = "bot_storage"

def load_json(name: str):
    """Supabase থেকে JSON ডেটা লোড করে। ডেটা না থাকলে None রিটার্ন করে।"""
    try:
        res = supabase.table(TABLE).select("data").eq("key", name).execute()
        if res.data:
            return res.data[0]["data"]
    except Exception as e:
        print(f"Supabase load error ({name}): {e}")
    return None

def save_json(name: str, data):
    """Supabase-এ JSON ডেটা সেভ করে।"""
    try:
        supabase.table(TABLE).upsert({
            "key": name,
            "data": data
        }).execute()
    except Exception as e:
        print(f"Supabase save error ({name}): {e}")
