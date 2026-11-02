import hashlib
import hmac
import os
import pathlib
import sqlite3
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Измерено на этом ключе: 1 % дневной квоты = 1452 токена, круглая цифра — 150 000.
DAILY_QUOTA = 150_000
SEED_PASS = "1234"
# Имя браузерного источника в таблице расхода. Совпадает с qwen_browser.QWEN —
# равенство проверяет checks/check.py, потому что разъезд имён молча обнулит учёт.
QWEN_MODEL = "qwen"


def db_path():
    return os.environ.get("AITALON_DB") or str(ROOT / "db" / "aitalon.db")


def conn():
    p = db_path()
    pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS passes (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        if not c.execute("SELECT 1 FROM passes WHERE code=?", (SEED_PASS,)).fetchone():
            c.execute("INSERT OR IGNORE INTO passes(code, created_at) VALUES(?,?)",
                      (SEED_PASS, time.time()))
        if not c.execute("SELECT 1 FROM meta WHERE key='secret'").fetchone():
            c.execute("INSERT INTO meta(key,value) VALUES('secret',?)", (os.urandom(32).hex(),))
        _migrate(c)


def _migrate(c):
    """Браузерный источник не отдаёт токены, поэтому у него свои колонки.
    Старые строки остаются: колонки добавляются с нулём, а не пересозданием таблицы."""
    have = {r[1] for r in c.execute("PRAGMA table_info(usage)")}
    for col in ("chars_in", "chars_out"):
        if col not in have:
            c.execute("ALTER TABLE usage ADD COLUMN {} INTEGER NOT NULL DEFAULT 0".format(col))


def utc_day_start():
    now = time.time()
    return now - (now % 86400)


def verify_pass(code):
    if not (isinstance(code, str) and code.isdigit() and 1 <= len(code) <= 32):
        return False
    with conn() as c:
        return c.execute("SELECT 1 FROM passes WHERE code=?", (code,)).fetchone() is not None


def add_pass(code, label=""):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO passes(code,label,created_at) VALUES(?,?,?)",
                  (code, label, time.time()))


def secret():
    with conn() as c:
        return bytes.fromhex(c.execute("SELECT value FROM meta WHERE key='secret'").fetchone()[0])


def record_usage(code, model, prompt, completion, reasoning, chars_in=0, chars_out=0):
    with conn() as c:
        c.execute("INSERT INTO usage(code,model,prompt_tokens,completion_tokens,reasoning_tokens,"
                  "chars_in,chars_out,ts) VALUES(?,?,?,?,?,?,?,?)",
                  (code, model, int(prompt or 0), int(completion or 0), int(reasoning or 0),
                   int(chars_in or 0), int(chars_out or 0), time.time()))


def usage_by_code():
    with conn() as c:
        rows = c.execute("SELECT code, COUNT(*) n, SUM(prompt_tokens) p, SUM(completion_tokens) o,"
                         " SUM(reasoning_tokens) r, SUM(chars_in) ci, SUM(chars_out) co,"
                         " SUM(CASE WHEN model=? THEN 1 ELSE 0 END) qn"
                         " FROM usage WHERE ts>=? GROUP BY code", (QWEN_MODEL, utc_day_start())).fetchall()
    return {r["code"]: {"requests": r["n"],
                        "prompt_tokens": r["p"] or 0,
                        "completion_tokens": r["o"] or 0,
                        "reasoning_tokens": r["r"] or 0,
                        "total_tokens": (r["p"] or 0) + (r["o"] or 0),
                        "qwen_requests": r["qn"] or 0,
                        "qwen_chars": (r["ci"] or 0) + (r["co"] or 0)} for r in rows}


def usage_totals():
    with conn() as c:
        row = c.execute("SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) p,"
                        " COALESCE(SUM(completion_tokens),0) o, COALESCE(SUM(reasoning_tokens),0) r,"
                        " SUM(CASE WHEN model=? THEN 1 ELSE 0 END) qn,"
                        " COALESCE(SUM(chars_in+chars_out),0) qc"
                        " FROM usage WHERE ts>=?", (QWEN_MODEL, utc_day_start())).fetchone()
    used = row["p"] + row["o"]
    return {"requests": row["n"], "prompt_tokens": row["p"], "completion_tokens": row["o"],
            "reasoning_tokens": row["r"], "tokens_used": used, "quota": DAILY_QUOTA,
            "quota_left": max(0, DAILY_QUOTA - used),
            "qwen_requests": row["qn"] or 0, "qwen_chars": row["qc"],
            "resets_at_utc": utc_day_start() + 86400}
