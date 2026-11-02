"""Документы талонов.

Семантика чека из кофейни: код печатается на листке, первым отсканировал — тот и
владелец. Отсюда три правила, которые держат весь модуль:

  * код уникален навсегда, а не в пределах документа — иначе второй документ
    повторяет первый и листки из разных партий взаимозаменяемы;
  * талон привязывается к устройству при первом входе;
  * у устройства может быть только один живой талон — иначе один человек с
    горсткой листков выкачивает весь документ.

Часов два, и они считаются по-разному: войти в талон можно, пока не прошло
`doc_hours` от выпуска документа, а дальше талон живёт свои `talon_hours` от
МОМЕНТА ПЕРВОГО ВХОДА. Успел зайти в 23:59 при окне в 24 часа — работает до 05:59.
"""
import datetime
import secrets
import sqlite3
import time

from . import db

# Без 0/O и 1/I/L: код иногда диктуют вслух или набирают с клавиатуры телефона.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LEN = 12
MAX_PER_DOCUMENT = 9999
HOUR = 3600.0


_inited = False


def init():
    """Идемпотентен: схема создаётся один раз за процесс, повторные вызовы дешёвые.
    Каждая точка входа обязана его позвать, иначе модуль может быть импортирован
    без запуска приложения (проверки, скрипты) и упадёт на отсутствующей таблице."""
    global _inited
    if _inited:
        return
    with db.conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            num TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            talon_hours REAL NOT NULL,
            doc_hours REAL NOT NULL,
            closed_at REAL
        );
        CREATE TABLE IF NOT EXISTS talons (
            code TEXT PRIMARY KEY,
            doc_id INTEGER NOT NULL,
            label INTEGER NOT NULL,
            total INTEGER NOT NULL,
            device TEXT,
            activated_at REAL
        );
        CREATE INDEX IF NOT EXISTS ix_talons_doc ON talons(doc_id);
        CREATE INDEX IF NOT EXISTS ix_talons_device ON talons(device);
        """)
    _inited = True


def _new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def create_document(count, talon_hours, doc_hours, now=None):
    now = now if now is not None else time.time()
    count = int(count)
    if not 1 <= count <= MAX_PER_DOCUMENT:
        raise ValueError("Талонов в документе: от 1 до {}".format(MAX_PER_DOCUMENT))
    for name, val in (("век талона", talon_hours), ("век документа", doc_hours)):
        if not 0 < float(val) <= 24 * 60:
            raise ValueError("{} задаётся в часах от 1 до 1440".format(name))
    init()
    day = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    with db.conn() as c:
        for attempt in range(20):
            seq = c.execute("SELECT COUNT(*) FROM documents WHERE num LIKE ?",
                            (day + "/%",)).fetchone()[0] + 1 + attempt
            num = "{}/{:02d}".format(day, seq)
            try:
                cur = c.execute(
                    "INSERT INTO documents(num,created_at,talon_hours,doc_hours) VALUES(?,?,?,?)",
                    (num, now, float(talon_hours), float(doc_hours)))
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("Не удалось подобрать номер документа")
        doc_id = cur.lastrowid
        for i in range(count):
            while True:
                try:
                    c.execute("INSERT INTO talons(code,doc_id,label,total) VALUES(?,?,?,?)",
                              (_new_code(), doc_id, i + 1, count))
                    break
                except sqlite3.IntegrityError:
                    continue  # код уникален навсегда, а не внутри документа
    return document(doc_id)


def document(doc_id):
    init()
    with db.conn() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        used = c.execute("SELECT COUNT(*) FROM talons WHERE doc_id=? AND activated_at IS NOT NULL",
                         (doc_id,)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM talons WHERE doc_id=?", (doc_id,)).fetchone()[0]
    return _doc_dict(row, total, used)


def documents():
    init()
    with db.conn() as c:
        rows = c.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        counts = {r["doc_id"]: (r["total"], r["used"]) for r in c.execute(
            "SELECT doc_id, COUNT(*) total,"
            " SUM(CASE WHEN activated_at IS NOT NULL THEN 1 ELSE 0 END) used"
            " FROM talons GROUP BY doc_id")}
    return [_doc_dict(r, *(counts.get(r["id"], (0, 0)))) for r in rows]


def _doc_dict(row, total, used):
    return {"id": row["id"], "num": row["num"], "created_at": row["created_at"],
            "talon_hours": row["talon_hours"], "doc_hours": row["doc_hours"],
            "closed": row["closed_at"] is not None, "closed_at": row["closed_at"],
            "total": total, "used": used}


def document_codes(doc_id):
    init()
    with db.conn() as c:
        return [r["code"] for r in c.execute(
            "SELECT code FROM talons WHERE doc_id=? ORDER BY label", (doc_id,))]


def close_document(doc_id, now=None):
    now = now if now is not None else time.time()
    with db.conn() as c:
        c.execute("UPDATE documents SET closed_at=? WHERE id=? AND closed_at IS NULL", (now, doc_id))


def talon(code):
    init()
    with db.conn() as c:
        row = c.execute("SELECT * FROM talons WHERE code=?", (code,)).fetchone()
    if not row:
        return None
    doc = document(row["doc_id"])
    return {"code": row["code"], "doc_id": row["doc_id"], "label": row["label"],
            "total": row["total"], "device": row["device"], "activated_at": row["activated_at"],
            "doc": doc}


def _reason(t, doc, device, now):
    """Почему талон нельзя использовать. None — можно."""
    if doc["closed"]:
        return "Документ {} закрыт.".format(doc["num"])
    if t["activated_at"] is None:
        if now >= doc["created_at"] + doc["doc_hours"] * HOUR:
            return ("Срок выдачи талонов по документу {} истёк ({}).".format(
                doc["num"], fmt_time(doc["created_at"] + doc["doc_hours"] * HOUR)))
        return None
    dies = t["activated_at"] + doc["talon_hours"] * HOUR
    if now >= dies:
        return "Талон истёк в {}.".format(fmt_time(dies))
    if t["device"] and t["device"] != device:
        return "Этот талон уже активирован на другом устройстве."
    return None


def _live_talon_of(device, except_code, now):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM talons WHERE device=? AND code<>? AND activated_at IS NOT NULL",
                         (device, except_code)).fetchall()
        for r in rows:
            doc = document(r["doc_id"])
            if doc["closed"]:
                continue
            if now < r["activated_at"] + doc["talon_hours"] * HOUR:
                return {"code": r["code"], "label": r["label"], "doc": doc,
                        "until": r["activated_at"] + doc["talon_hours"] * HOUR}
    return None


def validate(code, device, now=None):
    """Годен ли уже используемый талон. Дёргается на каждый запрос."""
    now = now if now is not None else time.time()
    init()
    with db.conn() as c:
        t = c.execute("SELECT * FROM talons WHERE code=?", (code,)).fetchone()
    if not t:
        return False, "Такого талона нет."
    doc = document(t["doc_id"])
    reason = _reason(t, doc, device, now)
    if reason:
        return False, reason
    return True, ""


def enter(code, device, now=None):
    """Первый вход по коду: проверяет и привязывает талон к устройству."""
    now = now if now is not None else time.time()
    init()
    code = (code or "").strip().upper()
    with db.conn() as c:
        t = c.execute("SELECT * FROM talons WHERE code=?", (code,)).fetchone()
    if not t:
        return False, "Такого талона нет. Проверь номер на листке."
    doc = document(t["doc_id"])
    reason = _reason(t, doc, device, now)
    if reason:
        return False, reason
    busy = _live_talon_of(device, code, now)
    if busy:
        return False, "У этого устройства уже есть талон {} из {} — он действует до {}.".format(
            busy["label"], busy["doc"]["num"], fmt_time(busy["until"]))
    if t["activated_at"] is None:
        with db.conn() as c:
            c.execute("UPDATE talons SET device=?, activated_at=? WHERE code=? AND activated_at IS NULL",
                      (device, now, code))
    return True, code
