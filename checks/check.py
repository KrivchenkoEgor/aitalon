"""Проверки боевым HTTP. Запуск: run.sh check"""
import json as _json
import os
import pathlib
import queue
import re
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["AITALON_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "check.db")
os.environ.setdefault("ZRO_API_KEY", "ключ-не-используется-в-проверках")

from fastapi.testclient import TestClient  # noqa: E402

from app import db, gate, qwen_browser, sheet, sources, zro  # noqa: E402
from app.main import app  # noqa: E402

PASS = "1234"
fails = []


def check(name, cond, detail=""):
    print(("  ок   " if cond else "  ПАДЕНИЕ ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def last_content(items):
    """Последнее событие content = ВЕСЬ текст ответа.

    Протокол намеренно не «прирост»: сайт переписывает середину блока, и склейка
    приростов удваивала ответ (замер: раздувание в 4–5 раз).
    """
    vals = []
    for it in items:
        kind, text = (it.get("type"), it.get("text", "")) if isinstance(it, dict) else (it[0], it[1])
        if kind == "content":
            vals.append(text)
    return vals[-1] if vals else ""


db.init_db()
client = TestClient(app)

print("\n1. Экран числа")
r = client.get("/")
check("главная отдаёт HTML", r.status_code == 200 and "text/html" in r.headers["content-type"])
check("на главной есть поле для кода талона", "талон" in r.text.lower())

print("\n2. Пропуск")
r = client.post("/api/enter", json={"code": "0000"})
check("неверное число отклонено 403", r.status_code == 403, str(r.status_code))
check("неверное число не выдаёт cookie", "aitalon_session" not in r.cookies)
r = client.post("/api/enter", json={"code": "abc"})
check("не-число отклонено", r.status_code in (400, 403), str(r.status_code))
r = client.post("/api/enter", json={"code": PASS})
check("верное число принято", r.status_code == 200, str(r.status_code))
check("верное число выдаёт cookie", "aitalon_session" in r.cookies)
token = r.cookies.get("aitalon_session")

print("\n3. Кто я")
client.cookies.clear()
r = client.get("/api/me")
check("без cookie — не авторизован", r.json()["authorized"] is False)
client.cookies.set("aitalon_session", token)
me = client.get("/api/me").json()
check("с cookie — авторизован под 1234", me["authorized"] is True and me["code"] == PASS, str(me))
client.cookies.clear()

print("\n4. Подделка cookie")
r = client.get("/api/me", headers={"Cookie": "aitalon_session=" + gate.sign(PASS)[:-6]})
check("обрезанная подпись не проходит", r.json()["authorized"] is False)
r = client.get("/api/me", headers={"Cookie": "aitalon_session=" + PASS})
check("само число вместо cookie не проходит", r.json()["authorized"] is False)
forged = gate.sign("9999")
r = client.get("/api/me", headers={"Cookie": "aitalon_session=" + forged})
check("правильно подписанный код вне списка пропусков отклонён", r.json()["authorized"] is False, r.text)
r = client.post("/api/chat", headers={"Cookie": "aitalon_session=" + forged},
                json={"model": zro.MODELS[0], "messages": [{"role": "user", "content": "х"}]})
check("такой же поддельной сессией нельзя в чат", r.status_code in (401, 403), str(r.status_code))
check("просроченная сессия отклоняется", gate.unsign(gate.sign(PASS, ttl=-10)) is None)

print("\n5. Чат требует пропуска")
r = client.post("/api/chat", json={"model": zro.MODELS[0], "messages": [{"role": "user", "content": "привет"}]})
check("без cookie чат закрыт", r.status_code in (401, 403), str(r.status_code))
client.cookies.set("aitalon_session", token)
r = client.post("/api/chat", json={"model": "gpt-4", "messages": []})
check("неизвестная модель отклонена", r.status_code == 400, str(r.status_code))
r = client.post("/api/chat", json={"model": zro.MODELS[0], "messages": []})
check("пустая история отклонена", r.status_code == 400, str(r.status_code))

print("\n6. Стрим на подставном zro (без сети и без расхода квоты)")
seen = {}


async def fake_stream(messages, model):
    yield {"type": "reasoning", "text": "думаю"}
    yield {"type": "content", "text": "При"}
    yield {"type": "content", "text": "вет"}
    yield {"type": "done", "usage": {"prompt_tokens": 11, "completion_tokens": 7, "reasoning_tokens": 3}}


real = zro.stream
zro.stream = fake_stream
try:
    with client.stream("POST", "/api/chat", json={"model": zro.MODELS[0],
                                                  "messages": [{"role": "user", "content": "тест"}]}) as r:
        check("стрим отдаёт 200", r.status_code == 200)
        check("стрим это event-stream", "text/event-stream" in r.headers.get("content-type", ""))
        events = [_json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    check(".reasoning идёт отдельным событием", "reasoning" in types, str(types))
    text = last_content(events)
    check("zro: прирост свёрнут в накопленный текст", text == "Привет", repr(text))
    check("последнее событие — done с usage", types[-1] == "done")
    seen = db.usage_by_code()
    row = seen.get(PASS, {})
    check("расход записан под пропуском", row.get("requests") == 1, str(seen))
    # reasoning_tokens входят в completion_tokens (замер: 98 размышления + 12 текста = 110 вывода),
    # поэтому складывать их с выводом нельзя — будет двойной счёт.
    check("расход = вход + вывод, размышление не считается дважды",
          row.get("total_tokens") == row.get("prompt_tokens") + row.get("completion_tokens") == 18, str(row))
finally:
    zro.stream = real

print("\n7. Защита от пустого ответа (найдено замером: HTTP 200 и пусто)")
try:
    zro.guard_finish("length", 0)
    check("finish=length без текста всплывает ошибкой", False)
except zro.EmptyAnswer:
    check("finish=length без текста всплывает ошибкой", True)
zro.guard_finish("stop", 12)
check("нормальный ответ не падает", True)
try:
    zro.guard_finish("stop", 0)
    check("пустой ответ при stop тоже ловится", False)
except zro.EmptyAnswer:
    check("пустой ответ при stop тоже ловится", True)
check("бюджет вывода не меньше 2000 (иначе размышление съест ответ)", zro.MAX_TOKENS >= 2000, str(zro.MAX_TOKENS))

print("\n8. Расход")
tot = db.usage_totals()
check("есть поле остатка дневной квоты", "quota_left" in tot, str(tot))
check("после одного запроса израсходовано 18", tot["tokens_used"] == 18, str(tot))

print("\n9. Qwen как третий источник")
check("имя источника одинаковое во всех трёх местах",
      db.QWEN_MODEL == qwen_browser.QWEN == sources.QWEN,
      "{} {} {}".format(db.QWEN_MODEL, qwen_browser.QWEN, sources.QWEN))
check("источников ровно три", len(sources.SOURCES) == 3, str(sources.SOURCES))
check("cli импортирован без запуска браузера", qwen_browser.cli.BASE_URL == "https://chat.qwen.ai")
check("пороги не задублированы, а взяты из cli",
      qwen_browser.cli.GEN_TIMEOUT == 300 and qwen_browser.cli.STABLE_FALLBACK_SEC == 8)
me = client.get("/api/me").json()
check("страница видит три источника", me["models"] == sources.SOURCES, str(me["models"]))
check("из истории в qwen уходит только новая реплика",
      sources.last_user_text([{"role": "user", "content": "первая"},
                              {"role": "assistant", "content": "ответ"},
                              {"role": "user", "content": "вторая"}]) == "вторая")

before = db.usage_totals()
captured = {}


async def fake_qwen(text, code):
    captured["text"], captured["code"] = text, code
    yield {"type": "note", "text": "Qwen печатает…"}
    yield {"type": "content", "text": "При"}
    yield {"type": "content", "text": "Привет"}
    yield {"type": "done", "usage": {"chars_in": len(text), "chars_out": 6}}


real_qwen = qwen_browser.stream
qwen_browser.stream = fake_qwen
try:
    with client.stream("POST", "/api/chat", json={"model": "qwen",
                                                  "messages": [{"role": "user", "content": "тест"}]}) as r:
        check("qwen отдаёт 200", r.status_code == 200, str(r.status_code))
        evs = [_json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data: ")]
finally:
    qwen_browser.stream = real_qwen

types = [e["type"] for e in evs]
check("тот же набор событий, что у api-источника", types[0] == "note" and types[-1] == "done", str(types))
check("qwen: последнее событие несёт весь ответ", last_content(evs) == "Привет", repr(last_content(evs)))
check("ушёл только текст, без всей истории", captured["text"] == "тест", str(captured))
check("чат привязан к пропуску", captured["code"] == PASS, str(captured))

after = db.usage_totals()
check("qwen не списывает токены zro", after["tokens_used"] == before["tokens_used"],
      "{} -> {}".format(before["tokens_used"], after["tokens_used"]))
check("остаток квоты не изменился", after["quota_left"] == before["quota_left"])
check("обращение к qwen посчитано", after["qwen_requests"] == before["qwen_requests"] + 1)
check("символы записаны", after["qwen_chars"] == before["qwen_chars"] + 10, str(after["qwen_chars"]))
# Ловушка, которую живой прогон показал, а HTTP-проверка нет: Thread.__init__
# кладёт в экземпляр self._handle, и метод с таким же именем молча переставал
# быть методом. Сверяем имена класса с __dict__ готового экземпляра.
_probe = qwen_browser.Engine()
_mine = {m for m in vars(qwen_browser.Engine) if not m.startswith("__")}
_shadow = sorted(_mine & set(vars(_probe)))
check("ни один метод воркера не перекрыт полем threading.Thread", not _shadow, str(_shadow))
check("браузер за проверки не поднимался", qwen_browser._engine is None)

print("\n10. Миграция старой базы")
old_path = pathlib.Path(tempfile.mkdtemp()) / "old.db"
import sqlite3  # noqa: E402
oc = sqlite3.connect(old_path)
oc.executescript("""
CREATE TABLE passes (code TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE usage (id INTEGER PRIMARY KEY, code TEXT NOT NULL, model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO passes(code,created_at) VALUES('1234',1);
INSERT INTO usage(code,model,prompt_tokens,completion_tokens,reasoning_tokens,ts)
  VALUES('1234','deepseek-v4.1-flash',100,50,40,9e17);
""")
oc.commit(); oc.close()

saved_env = os.environ["AITALON_DB"]
os.environ["AITALON_DB"] = str(old_path)
try:
    db.init_db()
    with db.conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(usage)")}
        left = c.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    check("колонки символов добавились в старую таблицу", {"chars_in", "chars_out"} <= cols, str(sorted(cols)))
    check("ни одна старая строка не пропала", left == 1, str(left))
    t = db.usage_totals()
    check("старый расход пересчитался", t["tokens_used"] == 150, str(t["tokens_used"]))
finally:
    os.environ["AITALON_DB"] = saved_env

print("\n11. Наблюдатель ответа")


def _run_watch(steps, before_count):
    """Прогон Watch по смоделированным состояниям сайта: (текст, блоков, генерация).

    Возвращает (последний отданный текст, список отданных текстов, наблюдатель).
    """
    w = qwen_browser.Watch(before_count, deadline=10_000)
    got = []
    for n, (text, count, generating) in enumerate(steps):
        out, fin = w.feed(text, count, generating, n * 0.5)
        if out:
            got.append(out)
        if fin:
            break
    return (got[-1] if got else ""), got, w


# Сценарий живого прогона: до начала генерации в DOM лежит прошлый ответ.
sent, seq, w = _run_watch([("Ноябрь", 1, False), ("Ноябрь", 1, False),
                           ("Qwen", 2, False), ("Qwen", 2, False)], 1)
check("прошлый ответ не уходит в ленту", sent == "Qwen", repr(sent))
check("итог — только новый ответ", w.emitted == "Qwen", repr(w.emitted))
# Ответ растёт по буквам, кнопка «стоп» видна, потом исчезает.
sent2, seq2, w2 = _run_watch([("", 0, False), ("Пр", 1, True), ("При", 1, True),
                              ("Привет", 1, True), ("Привет", 1, False),
                              ("Привет", 1, False)], 0)
check("каждое событие — весь текст на сейчас", seq2 == ["Пр", "При", "Привет"], str(seq2))
check("итог равен отданному тексту", w2.emitted == sent2, repr(w2.emitted))
# Живой замер: сайт переписывает СЕРЕДИНУ блока, и текст перестаёт быть
# продолжением. Старая схема отдавала прирост, а клиент дописывал — ответ
# раздувало враз. Дискриминатор здесь — что каждое событие несёт ЦЕЛЫЙ текст,
# а не огрызок: при возврате к приросту эти проверки падают.
sent3, seq3, w3 = _run_watch([("Раз", 1, True), ("Раз, два", 1, True), ("Раз, два, три", 1, True),
                              ("Раз, два, три", 1, False), ("Раз, два, три", 1, False)], 0)
check("последнее событие = весь ответ целиком", sent3 == "Раз, два, три", repr(sent3))
check("события идут целым текстом, а не огрызками",
      seq3 == ["Раз", "Раз, два", "Раз, два, три"], str(seq3))
sent4, seq4, w4 = _run_watch([("Раз", 1, True), ("ПЕРЕПИСАНО", 1, True), ("ПЕРЕПИСАНО", 1, False),
                              ("ПЕРЕПИСАНО", 1, False)], 0)
check("перерисовка середины не оставляет старых кусков",
      sent4 == "ПЕРЕПИСАНО" and seq4 == ["Раз", "ПЕРЕПИСАНО"], str(seq4))
# Живой случай талона 1010: при открытии чата с оборванным ответом Qwen дописывает
# СТАРЫЙ блок — блоков столько же, текст растёт. Прошлый признак «текст изменился»
# принимал эту довыдачу за новый ответ и отдавал человеку чужой текст.
sent5, seq5, w5 = _run_watch([("старый ответ", 2, True), ("старый ответ стал длиннее", 2, True),
                              ("старый ответ самый длинный", 2, False),
                              ("новый ответ", 3, True), ("новый ответ", 3, False),
                              ("новый ответ", 3, False)], 2)
check("довыдача старого блока не считается ответом", sent5 == "новый ответ", repr(sent5))
check("чужой прошлый ответ не попадает в ленту",
      all("старый" not in s for s in seq5), str(seq5))
# Сторона браузера недоступна этим проверкам (TestClient JS не исполняет),
# поэтому хотя бы страховка от отката: событие несёт весь текст, значит клиент
# обязан заменять, а не дописывать. Проверка по исходнику страницы.
_page = (pathlib.Path(__file__).resolve().parent.parent / "app" / "web" / "index.html").read_text(encoding="utf-8")
check("страница заменяет текст ответа, а не дописывает",
      "answer += ev.text" not in _page and "answer = ev.text" in _page)
# Генерация не началась — ожидание должно завершиться само, а не висеть.
dead = qwen_browser.Watch(0, deadline=5)
dead.feed("", 0, False, 10)
check("просрочка завершает наблюдение", dead.finished and dead.timed_out)

print("\n12. Два талона отвечаются одновременно")


class _BtnList:
    def __init__(self, btns):
        self._b = btns

    def count(self):
        return len(self._b)

    def nth(self, i):
        return self._b[i]


class _Tab:
    def __init__(self, name, panel=None, log=None, pending=False):
        self.name = name
        self.url = "https://chat.qwen.ai/c/" + name
        self.panel = panel or []
        self.log = log
        self.pending = pending

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        return _BtnList([])

    def evaluate(self, script, arg=None):
        if script == qwen_browser.CHOICE_SCAN_JS:
            return self.panel
        if script == qwen_browser.CHOICE_PENDING_JS:
            return self.pending
        if script == qwen_browser.CHOICE_CLICK_JS:
            if self.log is not None:
                self.log.append(arg)
            return True
        return None


site = {"1111": ["", 0, False], "2222": ["", 0, False]}
sent_to = []
real = {k: getattr(qwen_browser.cli, k, None) for k in
        ("ensure_named_chat", "send_question", "extract_last_answer", "count_answers",
         "get_chat_id_from_url", "load_chats", "save_chats", "log")}
real_gen, real_rename = qwen_browser._generating, qwen_browser.rename_ours

eng = qwen_browser.Engine()
eng._started_once = True          # нить не поднимаем, все шаги делаем руками
eng._pages = {"1111": _Tab("1111"), "2222": _Tab("2222")}

try:
    qwen_browser.cli.log = lambda m: None
    qwen_browser.cli.ensure_named_chat = lambda page, name: (None, True)
    qwen_browser.cli.extract_last_answer = lambda page: site[page.name][0]
    qwen_browser.cli.count_answers = lambda page: site[page.name][1]
    qwen_browser.cli.get_chat_id_from_url = lambda url: url.rsplit("/", 1)[-1]
    qwen_browser.cli.load_chats = lambda: {}
    qwen_browser.cli.save_chats = lambda d: None
    qwen_browser._generating = lambda page: site[page.name][2]
    qwen_browser.rename_ours = lambda page, name, q: False

    def send(page, text):
        sent_to.append(page.name)
        order.append("отправка:" + page.name)
        site[page.name] = ["", 0, True]

    qwen_browser.cli.send_question = send

    b1, a1 = eng.submit("первый вопрос", "1111")
    b2, a2 = eng.submit("второй вопрос", "2222")
    check("разные талоны не ждут друг друга", a1 == 0 and a2 == 0, "{} {}".format(a1, a2))
    b3, a3 = eng.submit("повтор в тот же талон", "1111")
    check("повтор тому же талону встаёт в очередь", a3 == 1, str(a3))

    # Оставшаяся с прошлого вопроса панель перекрывает поле ввода — значит её
    # надо снять ДО отправки, а не только во время опроса.
    order = []
    eng._pages["1111"].panel = [{"visible": True, "enabled": True}]
    eng._pages["1111"].log = order

    eng._start_ready()
    check("за один шаг поднимается не больше одного талона", len(eng._active) == 1,
          str(set(eng._active)))
    check("первый уже отвечает, пока второй ещё поднимается",
          sorted(eng._active) == ["1111"], str(set(eng._active)))
    eng._start_ready()
    check("оба талона генерируют одновременно", set(eng._active) == {"1111", "2222"},
          str(set(eng._active)))
    check("вопрос ушёл в каждый лист", sorted(sent_to) == ["1111", "2222"], str(sent_to))
    check("панель снимается до отправки вопроса",
          order == [0, "отправка:1111", "отправка:2222"], str(order))
    eng._pages["1111"].panel = []
    eng._pages["1111"].log = None

    acc1, acc2 = [], []

    # Проводка: движок обязан сам разбирать панель, иначе чат встанет до выбора.
    panel_clicks = []
    eng._pages["1111"].panel = [{"visible": True, "enabled": True}, {"visible": True, "enabled": True}]
    eng._pages["1111"].log = panel_clicks
    eng._tick_all()
    check("движок сам закрывает панель выбора", panel_clicks == [1], str(panel_clicks))
    eng._pages["1111"].panel = []
    eng._pages["1111"].log = None

    def drain(box, acc):
        while True:
            try:
                acc.append(box.get_nowait())
            except queue.Empty:
                return

    def texts(acc):
        return last_content(acc)

    # Панель спросила, а нажать не удалось: редкий и случайный случай, поэтому
    # он обязан заявить о себе сам, а не молча держать чат.
    site["1111"] = ["", 0, False]
    eng._pages["1111"].panel = [{"visible": True, "enabled": False}]
    eng._pages["1111"].pending = True
    drain(b1, acc1)
    eng._tick_all()
    eng._tick_all()
    drain(b1, acc1)
    notes = [p for k, p in acc1 if k == "note"]
    check("застрявшую панель видно человеку", any("не вышло" in n for n in notes), str(notes))
    check("предупреждение о панели не повторяется каждый шаг",
          sum(1 for n in notes if "не вышло" in n) == 1, str(notes))
    eng._pages["1111"].panel = []
    eng._pages["1111"].pending = False

    site["1111"] = ["Раз", 1, True]
    site["2222"] = ["Два", 1, True]
    eng._tick_all()
    drain(b1, acc1)
    drain(b2, acc2)
    check("один шаг движка двинул ОБА ответа", texts(acc1) == "Раз" and texts(acc2) == "Два",
          "{} | {}".format(texts(acc1), texts(acc2)))

    site["1111"] = ["Раз, два", 1, True]
    site["2222"] = ["Два, три", 1, True]
    eng._tick_all()
    drain(b1, acc1)
    drain(b2, acc2)
    site["1111"] = ["Раз, два", 1, False]
    site["2222"] = ["Два, три", 1, False]
    eng._tick_all()
    eng._tick_all()
    drain(b1, acc1)
    drain(b2, acc2)

    check("талон 1111 получил свой текст", texts(acc1) == "Раз, два", repr(texts(acc1)))
    check("талон 2222 получил свой текст", texts(acc2) == "Два, три", repr(texts(acc2)))
    check("тексты не переехали между талонами",
          "Два" not in texts(acc1) and "Раз" not in texts(acc2))
    check("оба завершились и активные слоты свободны", eng._active == {}, str(eng._active))
    check("третий вопрос остался ждать своего талона", len(eng._waiting["1111"]) == 1,
          str(len(eng._waiting["1111"])))
    done1 = [p for k, p in acc1 if k == "done"]
    check("расход посчитан в символах", done1 and done1[0]["chars_out"] == 8, str(done1))
finally:
    for k, v in real.items():
        setattr(qwen_browser.cli, k, v)
    qwen_browser._generating, qwen_browser.rename_ours = real_gen, real_rename

print("\n13. Переименование чата защищено от чужого")


class _Row:
    def __init__(self, t):
        self.t = t

    def inner_text(self, timeout=None):
        return self.t


class _RowList:
    def __init__(self, t):
        self.first = _Row(t)


class _SideTab:
    """Лист, у которого в сайдбаре первым лежит чат с заданным заголовком."""

    def __init__(self, title):
        self.title = title

    def locator(self, sel):
        return _RowList(self.title)


renamed = []
real_rename_fn = qwen_browser.cli.rename_top_chat
qwen_browser.cli.rename_top_chat = lambda page, name: renamed.append(name)
try:
    ok1 = qwen_browser.rename_ours(_SideTab("Раз, два, три и ещё"), "aitalon-1111", "Раз, два, три и ещё")
    check("свой чат переименован", ok1 is True and renamed == ["aitalon-1111"], str(renamed))
    ok2 = qwen_browser.rename_ours(_SideTab("Совсем чужой вопрос"), "aitalon-2222", "Наш вопрос")
    check("чужой чат НЕ переименован", ok2 is False and renamed == ["aitalon-1111"], str(renamed))
    ok3 = qwen_browser.rename_ours(_SideTab(None), "aitalon-3333", "Что-то")
    check("сбой чтения заголовка не роняет ответ", ok3 is False and renamed == ["aitalon-1111"], str(renamed))
    # Живой случай: сайт поднимает заглавную букву, «тест пипирки» → «Тест Пипирки».
    ok4 = qwen_browser.rename_ours(_SideTab("Тест Пипирки"), "aitalon-4444", "тест пипирки и что дальше")
    check("регистр не мешает узнать свой чат", ok4 is True and renamed[-1] == "aitalon-4444", str(renamed))
finally:
    qwen_browser.cli.rename_top_chat = real_rename_fn

print("\n14. Выход")
# Отдельный клиент, который входит по-настоящему: cookie, подложенный вручную
# через cookies.set(), httpx хранит без домена, и серверное удаление его не
# снимает. Это ловушка проверки, а не поведение приложения.
guest = TestClient(app)
guest.post("/api/enter", json={"code": PASS})
check("до выхода пущен", guest.get("/api/me").json()["authorized"] is True)
r = guest.post("/api/exit")
check("выход ответил 200", r.status_code == 200, str(r.status_code))
check("после выхода больше не авторизован", guest.get("/api/me").json()["authorized"] is False)
check("после выхода чат закрыт", guest.post("/api/chat", json={"model": zro.MODELS[0],
      "messages": [{"role": "user", "content": "x"}]}).status_code in (401, 403))
check("на странице есть кнопка выхода", 'id="exit"' in _page and "/api/exit" in _page)
check("подсказку про отдельное окно убрали", "Отвечает в отдельном окне" not in _page)
check("счётчик Qwen со страницы убран", "qspend" not in _page and "вопр. на" not in _page)

print("\n15. Панель «Какой ответ вы предпочитаете»")


def _ready(v=True, e=True):
    return {"visible": v, "enabled": e}


check("пустой список не даёт выбора", qwen_browser.pick_last_ready([]) == -1)
check("из двух готовых берется последний",
      qwen_browser.pick_last_ready([_ready(), _ready()]) == 1)
check("если последний ещё пишется, берется готовый",
      qwen_browser.pick_last_ready([_ready(), _ready(e=False)]) == 0)
check("невидимый вариант не выбирается",
      qwen_browser.pick_last_ready([_ready(v=False), _ready()]) == 1)
check("пока ничего не готово — не трогаем",
      qwen_browser.pick_last_ready([_ready(e=False), _ready(v=False, e=False)]) == -1)


class _PanelPage:
    def __init__(self, items, click_ok=True):
        self.items, self.click_ok = items, click_ok
        self.clicked = []

    def evaluate(self, script, arg=None):
        if script == qwen_browser.CHOICE_SCAN_JS:
            return self.items
        if script == qwen_browser.CHOICE_CLICK_JS:
            self.clicked.append(arg)
            return self.click_ok
        return None


check("текст вопроса панели ищется по русски",
      qwen_browser.CHOICE_ASK_PHRASE == "Какой ответ вы предпочитаете")


class _PendingPage:
    def __init__(self, v):
        self.v = v

    def evaluate(self, script, arg=None):
        assert script == qwen_browser.CHOICE_PENDING_JS, "choice_pending не должен звать другие скрипты"
        return self.v


class _DeadPage:
    def evaluate(self, script, arg=None):
        raise RuntimeError("страница закрыта")


check("вопрос о предпочтении узнаётся", qwen_browser.choice_pending(_PendingPage(True)) is True)
check("без вопроса — пусто", qwen_browser.choice_pending(_PendingPage(False)) is False)
check("сбой при поиске панели не роняет ответ", qwen_browser.choice_pending(_DeadPage()) is False)

real_log_fn = qwen_browser.cli.log
qwen_browser.cli.log = lambda m: None
try:
    p = _PanelPage([])
    check("без панели кликов нет", qwen_browser.resolve_choice(p) is False and p.clicked == [])
    p = _PanelPage([_ready(), _ready()])
    check("панель закрыта выбором последнего варианта",
          qwen_browser.resolve_choice(p) is True and p.clicked == [1], str(p.clicked))
    p = _PanelPage([_ready(), _ready(e=False)])
    check("если последний ещё пишется, берется готовый",
          qwen_browser.resolve_choice(p) is True and p.clicked == [0], str(p.clicked))
    p = _PanelPage([_ready(e=False), _ready(e=False)])
    check("пока оба неактивны — не трогаем", qwen_browser.resolve_choice(p) is False and p.clicked == [])
    p = _PanelPage([_ready(), _ready()], click_ok=False)
    check("неудачный клик не считается разбором", qwen_browser.resolve_choice(p) is False)

    class _Boom:
        def evaluate(self, script, arg=None):
            raise RuntimeError("страница закрыта")

    check("сбой страницы не роняет ответ", qwen_browser.resolve_choice(_Boom()) is False)
    check("текст кнопки на панели ищется по русски",
          qwen_browser.CHOICE_PHRASE == "Предпочитаю этот ответ")
finally:
    qwen_browser.cli.log = real_log_fn

print("\n16. Документы талонов")
from app import tickets  # noqa: E402

H = 3600.0
T0 = 1_700_000_000.0

doc = tickets.create_document(count=5, talon_hours=6, doc_hours=24, now=T0)
codes = tickets.document_codes(doc["id"])
check("документу присвоены номер и дата", bool(re.match(r"\d{4}-\d\d-\d\d/\d+$", doc["num"])), doc["num"])
check("талонов ровно столько, сколько заказали", len(codes) == 5, str(len(codes)))
check("код не короче 12 знаков", all(len(c) >= 12 for c in codes), str(codes[0]))
check("в коде нет похожих друг на друга знаков",
      all(set(c) <= set(tickets.ALPHABET) for c in codes), str(codes[0]))
check("внутри документа коды не повторяются", len(set(codes)) == len(codes))
doc_b = tickets.create_document(count=5, talon_hours=6, doc_hours=24, now=T0)
codes_b = tickets.document_codes(doc_b["id"])
check("между документами коды не повторяются", not (set(codes) & set(codes_b)))
check("номера документов разные", doc["num"] != doc_b["num"], "{} = {}".format(doc["num"], doc_b["num"]))

print("\n  привязка к устройству")
ok, msg = tickets.enter("NOPE1234567890", "devA", now=T0)
check("несуществующий код отклонён", ok is False, msg)
ok, msg = tickets.enter(codes[0], "devA", now=T0)
check("первый вход разрешён", ok is True, msg)
ok, msg = tickets.enter(codes[0], "devA", now=T0)
check("повторный вход тем же устройством разрешён", ok is True, msg)
ok, msg = tickets.enter(codes[0], "devB", now=T0)
check("активированный талон чужому устройству не отдаётся", ok is False, msg)
ok, msg = tickets.enter(codes[1], "devA", now=T0)
check("второй живой талон тому же устройству запрещён", ok is False, msg)
ok, msg = tickets.enter(codes[1], "devB", now=T0 + 7 * H)
check("освободившееся место можно занять", ok is True, msg)
ok, msg = tickets.enter(codes[2], "devB", now=T0 + 7 * H)
check("и тут же занять нельзя", ok is False, msg)

print("\n  два часа")
ok, msg = tickets.enter(codes[3], "devC", now=T0 + 25 * H)
check("спустя сутки после выпуска войти нельзя", ok is False, msg)
d3 = tickets.create_document(count=1, talon_hours=6, doc_hours=24, now=T0)
c3 = tickets.document_codes(d3["id"])[0]
ok, msg = tickets.enter(c3, "devD", now=T0 + 23.9 * H)
check("успел за минуту до конца окна документа", ok is True, msg)
ok, msg = tickets.validate(c3, "devD", now=T0 + 26 * H)
check("после входа талон переживает окно документа", ok is True, msg)
ok, msg = tickets.validate(c3, "devD", now=T0 + 30 * H)
check("но свой век отсчитывает от первого входа", ok is False, msg)

print("\n  досрочное закрытие")
d4 = tickets.create_document(count=2, talon_hours=6, doc_hours=24, now=T0)
c4 = tickets.document_codes(d4["id"])
tickets.enter(c4[0], "devE", now=T0)
tickets.close_document(d4["id"], now=T0 + H)
ok, msg = tickets.validate(c4[0], "devE", now=T0 + 2 * H)
check("закрытие гасит даже активированный талон", ok is False, msg)
ok, msg = tickets.enter(c4[1], "devF", now=T0 + 2 * H)
check("после закрытия оставшиеся коды мертвы", ok is False, msg)
docs = tickets.documents()
check("список документов показывает и закрытые",
      any(d["id"] == d4["id"] and d["closed"] for d in docs), str(docs[-1] if docs else None))

print("\n17. Сквозной путь: QR → чат, и операторская")
# Ключ читается из окружения в момент запроса, поэтому перезагружать модуль не нужно.
os.environ["AITALON_ADMIN_KEY"] = "ключ-для-проверок"

web = TestClient(app)
r = web.get("/")
check("главная выдаёт метку устройства", "aitalon_dev" in r.cookies, str(dict(r.cookies)))
dev = r.cookies.get("aitalon_dev")
doc_http = tickets.create_document(count=3, talon_hours=6, doc_hours=24)
codes_http = tickets.document_codes(doc_http["id"])

r = web.post("/api/enter", json={"code": codes_http[0]})
check("по QR-коду пускает без ручного ввода", r.status_code == 200 and "aitalon_session" in r.cookies,
      str(r.status_code) + " " + r.text[:80])
r = web.post("/api/enter", json={"code": "НЕВЕРНЫЙ12345"})
check("несуществующий код отклонён и здесь", r.status_code == 403)

other = TestClient(app)
other.get("/")
r = other.post("/api/enter", json={"code": codes_http[0]})
check("второе устройство в тот же талон не попадёт", r.status_code == 403, r.text[:90])

r = web.get("/api/me")
check("сессия по талону жива", r.json()["authorized"] is True, str(r.json()))

print("\n  операторская")
anon = TestClient(app)
r = anon.get("/admin/documents")
check("без ключа список закрыт", r.status_code in (401, 503), str(r.status_code))
r = anon.post("/admin/login", json={"key": "не тот"})
check("неверный ключ не пускает", r.status_code == 401)
r = anon.post("/admin/login", json={"key": "ключ-для-проверок"})
check("верный ключ пускает", r.status_code == 200, str(r.status_code))
r = anon.get("/admin/documents")
check("после входа список доступен", r.status_code == 200 and len(r.json()["documents"]) >= 1)
r = anon.post("/admin/documents", json={"count": 12, "talon_hours": 6, "doc_hours": 24})
check("документ создаётся из формы", r.status_code == 200 and r.json()["document"]["total"] == 12,
      str(r.status_code) + " " + r.text[:80])
new_id = r.json()["document"]["id"]
r = anon.post("/admin/documents", json={"count": 0, "talon_hours": 6, "doc_hours": 24})
check("нулевое количество отклонено", r.status_code == 400, str(r.status_code))
r = anon.get("/documents/{}/sheet".format(new_id))
check("лист A4 собран по 10 на страницу", r.status_code == 200
      and r.text.count('class="card"') == 12 and r.text.count('class="sheet"') == 2,
      str(r.text.count('class="card"')))
# Адрес зашит внутрь штрихов QR, текстом его в листе искать бессмысленно.
check("ссылка талона строится из базового адреса и кода",
      sheet.talon_url("ABC234") == sheet.BASE_URL + "/?t=ABC234", sheet.talon_url("ABC234"))
check("базовый адрес не перебивает явную настройку оператора",
      sheet.base_url({"AITALON_BASE_URL": "http://192.168.1.55:8000"}) == "http://192.168.1.55:8000",
      sheet.base_url({"AITALON_BASE_URL": "http://192.168.1.55:8000"}))

print("\n  адрес QR строится на машине оператора, а не зашит в код")
here = socket.gethostname().strip()
lan = sheet.lan_ip()
check("на этой машине в адресе её имя или её сетевой адрес",
      here in sheet.default_base_url() or (lan and lan in sheet.default_base_url()),
      "{} | имя {!r} | адрес {!r}".format(sheet.default_base_url(), here, lan))
check("машина с mDNS-имем отдаёт его как есть",
      sheet.default_base_url(lambda: "Dell-2.local", lambda: "10.0.0.5")
      == "http://Dell-2.local:8000", sheet.default_base_url(lambda: "Dell-2.local", lambda: "10.0.0.5"))
check("машина без mDNS-имени (Linux, Windows) отдаёт свой сетевой адрес",
      sheet.default_base_url(lambda: "ubuntu", lambda: "10.0.0.5")
      == "http://10.0.0.5:8000", sheet.default_base_url(lambda: "ubuntu", lambda: "10.0.0.5"))
check("если и адреса нет, остаётся имя машины, а не чужая заглушка",
      sheet.default_base_url(lambda: "ubuntu", lambda: "") == "http://ubuntu:8000",
      sheet.default_base_url(lambda: "ubuntu", lambda: ""))
check("в коде не осталось имени конкретной машины",
      "MacBook" not in (ROOT / "app/sheet.py").read_text(encoding="utf-8")
      or "MacBook" in here, "app/sheet.py всё ещё держит чужое имя")
sheet_codes = tickets.document_codes(new_id)
check("на листе напечатаны сами коды и по одному QR на талон",
      all(c in r.text for c in sheet_codes) and r.text.count("<svg") == 12,
      "кодов: {} svg: {}".format(sum(1 for c in sheet_codes if c in r.text), r.text.count("<svg")))
r = anon.get("/documents/{}.pdf".format(new_id))
check("PDF отдаётся и это настоящий PDF",
      r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 5000,
      str(r.status_code) + " " + str(len(r.content)))

print("\n  закрытие гасит активную сессию")
tickets.close_document(doc_http["id"])
r = web.get("/api/me")
check("после закрытия сессия больше не жива", r.json()["authorized"] is False, str(r.json()))
check("и причина названа словами", "закрыт" in (r.json().get("reason") or "").lower(), str(r.json()))
r = web.post("/api/chat", json={"model": "glm-5.3-flash",
                                "messages": [{"role": "user", "content": "x"}]})
check("в чат после закрытия не пустить", r.status_code in (401, 403), str(r.status_code))

print("\n  старый ручной вход не сломался")
legacy = TestClient(app)
legacy.get("/")
r = legacy.post("/api/enter", json={"code": "1234"})
check("число из таблицы пропусков по-прежнему работает", r.status_code == 200, str(r.status_code))

print("\n18. Вход в Qwen: гонка первой отрисовки")


class _Clock:
    """Логические часы: sleep передвигает время, ничего на самом деле не ждёт."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


class _Answers:
    """Проверка логина по заранее записанной серии; после её конца повторяет последнюю."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


clk = _Clock()
guest = _Answers([False])
check("державшийся всё окно гость остаётся гостем",
      qwen_browser.logged_in_soon(guest, sleep=clk.sleep, now=clk.now) is False)
check("окно ожидания соблюдено, а не бесконечный опрос",
      clk.now() - 1000.0 >= qwen_browser.LOGIN_WAIT_SEC, str(clk.now() - 1000.0))
check("за окно проверка спрашивается много раз", guest.calls > 3, str(guest.calls))

clk = _Clock()
slow = _Answers([False, False, True])
check("медленная первая отрисовка всё равно признаётся входом",
      qwen_browser.logged_in_soon(slow, sleep=clk.sleep, now=clk.now) is True)
check("вход подтверждается двумя подряд", slow.calls == 4, str(slow.calls))

clk = _Clock()
blink = _Answers([True, False, True, False])
check("моргнувший до гидратации вход не считается",
      qwen_browser.logged_in_soon(blink, sleep=clk.sleep, now=clk.now) is False)

print("\n  наружу — простое слово")

# Ровно то, что живой прогон показал человеку в талоне ZP8PJAADG6DY.
PW = ('Locator.click: Timeout 30000ms exceeded. Call log: - waiting for '
      'locator("input[name=\\"email\\"]") - attempting click action')
shown = qwen_browser.humanize(RuntimeError(PW))
check("лог Playwright в чат не уходит",
      "Locator" not in shown and "email" not in shown and "Call log" not in shown, shown)
check("сбой назван по-русски и говорит, что делать",
      bool(shown) and "повтори" in shown.lower(), shown)
own = RuntimeError("Сессия Qwen не живая, а QWEN_EMAIL/QWEN_PASSWORD не найдены")
check("свою готовую подсказку humanize не перебивает", qwen_browser.humanize(own) == str(own))

# Так выглядит «Call log» Playwright, когда поле уже заполнено: значение атрибута
# попадает в текст ошибки, а значит и в служебную строку лога.
PW_FILLED = ('Locator.click: Timeout 30000ms exceeded. Call log: - waiting for '
             'locator("input[name=\\"email\\"]") - locator resolved to '
             '<input value="проверка@example.invalid" type="text" name="email">')


class _LoginPage(_Tab):
    def goto(self, url, wait_until=None):
        self.url = url


eng_login = qwen_browser.Engine()
eng_login._started_once = True  # нить не поднимаем: весь путь проходим руками
tab = _LoginPage("9999")
eng_login._ensure_browser = lambda: None


class _Ctx:
    def new_page(self):
        return tab


eng_login._ctx = _Ctx()
real_login = {k: getattr(qwen_browser.cli, k) for k in
              ("close_popups", "is_logged_in", "ensure_logged_in", "log")}
saved_creds = {k: os.environ.get(k) for k in ("QWEN_EMAIL", "QWEN_PASSWORD")}
saved_window = {k: getattr(qwen_browser, k) for k in ("LOGIN_WAIT_SEC", "LOGIN_POLL_SEC")}
os.environ["QWEN_EMAIL"] = "проверка@example.invalid"
os.environ["QWEN_PASSWORD"] = "проверка-пароль"
qwen_browser.LOGIN_WAIT_SEC = 1
qwen_browser.LOGIN_POLL_SEC = 0.2
try:
    qwen_browser.cli.close_popups = lambda page: None
    polls = []
    logins = []
    logged = []
    qwen_browser.cli.log = lambda m: logged.append(m)

    def _guest(page):
        polls.append(1)
        return False

    def _boom(page, email, password):
        logins.append((email, password))
        raise RuntimeError(PW_FILLED)

    qwen_browser.cli.is_logged_in = _guest
    qwen_browser.cli.ensure_logged_in = _boom
    box_login, _ahead = eng_login.submit("тест входа", "9999")
    eng_login._start_ready()
    events = []
    while not box_login.empty():
        events.append(box_login.get_nowait())
    errs = [p for k, p in events if k == "error"]
    check("неудачный вход объясняется человеку, а не локом",
          errs == [qwen_browser.LOGIN_FAILED], str(errs)[:200])
    check("креды в текст наружу не попадают",
          all("example.invalid" not in e and "проверка-пароль" not in e for e in errs), str(errs)[:200])
    check("кредов нет и в служебной строке лога",
          all("example.invalid" not in l and "проверка-пароль" not in l for l in logged),
          str(logged)[:200])
    check("но лог всё равно сохраняет техническую подробность",
          any("Locator" in l for l in logged), str(logged)[:200])
    check("scrub срезает секрет целиком, а не по длине строки",
          "example.invalid" not in qwen_browser.scrub(PW_FILLED), qwen_browser.scrub(PW_FILLED)[:120])
    check("прежде чем объявить гостя, страница спрошена несколько раз",
          len(polls) >= 3, str(len(polls)))
    check("вход на страницу пробы один, а не на каждый опрос",
          len(logins) == 1, str(len(logins)))
    check("креды взяты из окружения, а не из текста ошибки",
          logins == [("проверка@example.invalid", "проверка-пароль")], str(len(logins)))
finally:
    for k, v in real_login.items():
        setattr(qwen_browser.cli, k, v)
    for k, v in saved_window.items():
        setattr(qwen_browser, k, v)
    for k, v in saved_creds.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\n19. Удаление чатов закрытого документа")

ROOT_URL = "https://chat.qwen.ai/"
CHAT_URL = "https://chat.qwen.ai/c/"
ID_A = "11111111-1111-1111-1111-111111111111"
ID_B = "22222222-2222-2222-2222-222222222222"
GONE_DOM = {"menu": False, "item": False, "dialog": False, "yes": False, "active": False}
LIVE_DOM = {"menu": True, "item": True, "dialog": True, "yes": True, "active": True}

check("открытый чат узнаётся по своему адресу",
      qwen_browser.classify_url(CHAT_URL + ID_A, ID_A) == "ours")
check("увод на главную читается как «чата нет»",
      qwen_browser.classify_url(ROOT_URL, ID_A) == "gone")
check("чужой чат — это не наш чат", qwen_browser.classify_url(CHAT_URL + ID_B, ID_A) == "other")
check("страница входа не выдаётся за удалённый чат",
      qwen_browser.classify_url(ROOT_URL + "auth", ID_A) == "other")
# Без этой строки пустой id совпал бы с «в адресе id нет», и не существующий чат
# начал бы открываться как живой.
check("пустой id не принимается за открытый чат",
      qwen_browser.classify_url(ROOT_URL, None) == "other",
      qwen_browser.classify_url(ROOT_URL, None))


class _Reads:
    """Страница, где каждый новый взгляд показывает следующий кадр записанных адресов."""

    def __init__(self, urls, dom=None):
        self._urls = list(urls)
        self._i = 0
        self.dom = dom if dom is not None else dict(GONE_DOM)

    @property
    def url(self):
        u = self._urls[min(self._i, len(self._urls) - 1)]
        self._i += 1
        return u

    def evaluate(self, script, arg=None):
        assert script == qwen_browser.CHAT_DOM_JS, "open_verdict не должен трогать разметку иначе"
        return self.dom


clk = _Clock()
check("один взгляд на главную не объявляет чат удалённым",
      qwen_browser.open_verdict(_Reads([ROOT_URL, CHAT_URL + ID_A], dict(LIVE_DOM)), ID_A,
                                sleep=clk.sleep, now=clk.now) == "ours")
check("два взгляда на главную без активного чата — «исчез»",
      qwen_browser.open_verdict(_Reads([ROOT_URL]), ID_A, sleep=clk.sleep, now=clk.now) == "gone")
check("но если активный чат в сайдбаре есть — «исчез» не ставится",
      qwen_browser.open_verdict(_Reads([ROOT_URL], dict(LIVE_DOM)), ID_A,
                                sleep=clk.sleep, now=clk.now) == "other")
# Живой прогон: адрес удалённого чата сразу после перехода всё равно наш, а
# редирект приходит позже. Принять первый взгляд за «открыт» — значит ждать кнопку
# меню на ушедшей странице и записать удалённый чат как «не удалось».
check("адрес сразу после перехода не считается открытым чатом",
      qwen_browser.open_verdict(_Reads([CHAT_URL + ID_A]), ID_A,
                                sleep=clk.sleep, now=clk.now) == "other")
check("чат открыт, только когда адрес держится и активный пункт есть",
      qwen_browser.open_verdict(_Reads([CHAT_URL + ID_A], dict(LIVE_DOM)), ID_A,
                                sleep=clk.sleep, now=clk.now) == "ours")
# В другую сторону та же ловушка: один взгляд на главную не имеет права решать,
# что чата нет, — иначе привязка живого талона будет выброшена зря.
clk = _Clock()
check("главная, увиденная один раз, не объявляет чат удалённым",
      qwen_browser.open_verdict(_Reads([ROOT_URL, CHAT_URL + ID_A]), ID_A,
                                sleep=clk.sleep, now=clk.now) != "gone")


class _Site:
    """Страница пробы удаления.

    Модель живого сайта в двух правилах: переход по адресу чата приводит туда,
    где чат ещё есть, а удалённый чат сайт уводит на главную; разметка и адрес
    меняются от подтверждения удаления. Поэтому каждый новый чат ведётся
    самостоятельно, а не «залипает» на уже удалённом адресе.

    lands_on — редирект на чужой чат, deletes=False — сайт подтвердил клик, но
    чат как ни в чём не бывало остался.
    """

    def __init__(self, live=True, dom=None, click_ok=None, lands_on=None, deletes=True,
                 stuck_dialog=False):
        self.dead = set() if live else {ID_A}
        self._url = (CHAT_URL + ID_A) if live else ROOT_URL
        self.base_dom = dom if dom is not None else (dict(LIVE_DOM) if live else dict(GONE_DOM))
        self.dom = dict(self.base_dom)
        self.click_ok = click_ok or {}
        self.lands_on = lands_on
        self.deletes = deletes
        self.stuck_dialog = stuck_dialog
        self.pending = False
        self.clicks = []
        self.gotos = []

    @property
    def url(self):
        return self._url

    def goto(self, url, wait_until=None):
        self.gotos.append(url)
        who = self.lands_on if self.lands_on else _chat_of(url)
        # Переход всегда сначала приводит по запрошенному адресу — редирект делает
        # уже клиентский JS. В пробе он случается на первом взгляде на разметку,
        # иначе фикстура не ловила бы ту самую ошибку, что дал живой прогон.
        self._url = CHAT_URL + who
        self.pending = who in self.dead
        self.dom = dict(GONE_DOM) if self.pending else dict(self.base_dom)

    def evaluate(self, script, arg=None):
        if script == qwen_browser.CHAT_DOM_JS:
            if self.pending:
                self._url = ROOT_URL
                self.pending = False
            return self.dom
        if script == qwen_browser.CHAT_CLICK_JS:
            self.clicks.append(arg)
            if arg == "yes" and not self.stuck_dialog:
                # Живой прогон: после подтверждения окно гаснет, а адрес страницы
                # может оставаться прежним — удаляющий код на это и не смотрит.
                self.dom = dict(self.base_dom, dialog=False, item=False)
                if self.deletes:
                    self.dead.add(_chat_of(self._url))
            return self.click_ok.get(arg, True)
        raise AssertionError("удаление не должно звать другие скрипты: " + str(script)[:40])


def _chat_of(url):
    m = re.search(r"/c/([0-9a-fA-F-]+)", url)
    return m.group(1) if m else None


clk = _Clock()
ok_site = _Site()
verdict = qwen_browser.delete_chat(ok_site, ID_A, sleep=clk.sleep, now=clk.now)
check("удаление доведено до «удалён»", verdict == "deleted", verdict)
check("порядок кликов: меню, пункт «Удалить», подтверждение",
      ok_site.clicks == ["menu", "item", "yes"], str(ok_site.clicks))
check("адрес чата открывается и в начале, и для финальной сверки",
      ok_site.gotos == [CHAT_URL + ID_A, CHAT_URL + ID_A], str(ok_site.gotos))

clk = _Clock()
wrong = _Site(lands_on=ID_B)
check("чужой чат не трогается вообще",
      qwen_browser.delete_chat(wrong, ID_A, sleep=clk.sleep, now=clk.now) == "failed"
      and wrong.clicks == [] and wrong.gotos == [CHAT_URL + ID_A], str(wrong.clicks))

clk = _Clock()
absent = _Site(live=False)
check("уже удалённый чат даёт «уже нет» без единого клика",
      qwen_browser.delete_chat(absent, ID_A, sleep=clk.sleep, now=clk.now) == "gone"
      and absent.clicks == [], str(absent.clicks))

clk = _Clock()
nodlg = _Site(dom={"menu": True, "item": True, "dialog": False,
                       "yes": False, "active": True})
check("без окна подтверждения удаление не подтверждается",
      qwen_browser.delete_chat(nodlg, ID_A, sleep=clk.sleep, now=clk.now) == "failed"
      and nodlg.clicks == ["menu", "item"], str(nodlg.clicks))

clk = _Clock()
alive = _Site(deletes=False)
check("если чат после подтверждения жив — «удалён» не сказать",
      qwen_browser.delete_chat(alive, ID_A, sleep=clk.sleep, now=clk.now) == "failed"
      and alive.clicks == ["menu", "item", "yes"], str(alive.clicks))

clk = _Clock()
stuck = _Site(stuck_dialog=True)
check("если окно подтверждения не погасло — удаления не было",
      qwen_browser.delete_chat(stuck, ID_A, sleep=clk.sleep, now=clk.now) == "failed"
      and stuck.clicks == ["menu", "item", "yes"], str(stuck.clicks))

clk = _Clock()
nomenu = _Site(dom={"menu": False, "item": False, "dialog": False,
                        "yes": False, "active": True})
check("без кнопки меню удаляющий лист не тыкает вслепую",
      qwen_browser.delete_chat(nomenu, ID_A, sleep=clk.sleep, now=clk.now) == "failed"
      and nomenu.clicks == [], str(nomenu.clicks))

clk = _Clock()
seen_logs = []
real_log_del = qwen_browser.cli.log
qwen_browser.cli.log = lambda m: seen_logs.append(m)
try:
    qwen_browser.delete_chat(_Site(deletes=False), ID_A, sleep=clk.sleep, now=clk.now)
    qwen_browser.delete_chat(_Site(stuck_dialog=True), ID_A, sleep=clk.sleep, now=clk.now)
finally:
    qwen_browser.cli.log = real_log_del
check("каждый отказ назван словами и с адресом чата, а не просто «не удалось»",
      any("чат снова открылся по своему адресу" in l and ID_A in l for l in seen_logs)
      and any("окно подтверждения не закрылось" in l for l in seen_logs), str(seen_logs)[:250])

print("\n  движок чистит по одному и не путается с отвечающими талонами")

eng_c = qwen_browser.Engine()
eng_c._started_once = True
eng_c._logged = True
eng_c._ensure_browser = lambda: None
store = {"aitalon-7001": ID_A, "aitalon-7002": ID_B}
real_clean = {k: getattr(qwen_browser.cli, k) for k in ("load_chats", "save_chats", "log")}
try:
    qwen_browser.cli.load_chats = lambda: dict(store)
    qwen_browser.cli.save_chats = lambda d: (store.clear(), store.update(d))
    qwen_browser.cli.log = lambda m: None

    snap = eng_c.submit_cleanup(31, ["7001", "7002", "7003"])
    check("сводка известна сразу, до всякого браузера",
          snap["total"] == 3 and snap["left"] == 2, str(snap))
    check("талон, который ни о чём не спросил, — «чата нет», а не «не удалось»",
          snap["state"]["7003"] == "missing", str(snap["state"]))
    check("молчаливый документ не поднимает нить движка",
          eng_c.submit_cleanup(32, ["8888"])["left"] == 0)

    clean_site = _Site()
    eng_c._clean_pg = clean_site
    eng_c._do_cleanup()
    st = eng_c.cleanup_status(31)
    check("за один шаг движка удаляется ровно один чат",
          len(clean_site.clicks) == 3, str(clean_site.clicks))
    check("первый талон записан удалённым, второй ещё ждёт",
          st["state"]["7001"] == "deleted" and st["state"]["7002"] == "queued", str(st["state"]))
    check("привязка удалённого чата убрана из хранилища",
          "aitalon-7001" not in store, str(sorted(store)))
    check("привязка ещё не удалённого чата осталась на месте",
          "aitalon-7002" in store, str(sorted(store)))

    eng_c._active["9999"] = object()
    before = list(clean_site.clicks)
    eng_c._do_cleanup()
    check("пока талон отвечает, фоновая чистка его не перебивает",
          clean_site.clicks == before, str(clean_site.clicks))
    del eng_c._active["9999"]

    eng_c._do_cleanup()
    check("второй шаг дочистил второй чат", eng_c.cleanup_status(31)["left"] == 0,
          str(eng_c.cleanup_status(31)["state"]))
    check("оба чата убраны из привязок", store == {}, str(sorted(store)))

    fail_site = _Site(lands_on=ID_A)
    eng_c._clean_pg = fail_site
    store["aitalon-7005"] = ID_B
    snap5 = eng_c.submit_cleanup(33, ["7005"])
    eng_c._do_cleanup()
    st5 = eng_c.cleanup_status(33)
    check("неудалённый чат остаётся в привязках",
          st5["state"]["7005"] == "failed" and "aitalon-7005" in store, str(st5["state"]))
    check("страница талона забывается вместе с его чатом",
          "7005" not in eng_c._pages, str(sorted(eng_c._pages)))

    # Живой прогон оставил ровно это: чаты удалены, а привязки висят. Такой талон
    # должен получить «уже нет», а не вечное «не удалось», и привязка обязана уйти.
    store["aitalon-7006"] = ID_A
    eng_c._clean_pg = _Site(live=False)
    eng_c.submit_cleanup(34, ["7006"])
    eng_c._do_cleanup()
    check("уже удалённый чат закрывается словом «уже нет»",
          eng_c.cleanup_status(34)["state"]["7006"] == "gone"
          and "aitalon-7006" not in store, str(store))
finally:
    for k, v in real_clean.items():
        setattr(qwen_browser.cli, k, v)

print("\n  операторская: закрытие ставит чистку, прогресс читается отдельно")


class _FakeEng:
    def __init__(self):
        self.calls = []
        self.status = {"doc_id": 1, "total": 3, "done": 1, "left": 2,
                       "state": {"a": "deleted", "b": "queued"}}

    def submit_cleanup(self, doc_id, codes):
        self.calls.append((doc_id, list(codes)))
        return {"doc_id": doc_id, "total": len(codes), "done": 0, "left": len(codes),
                "state": {c: "queued" for c in codes}}

    def cleanup_status(self, doc_id):
        # Как в настоящем движке: прогресс есть только у того документа, чистку
        # которого ставили.
        return self.status if any(d == doc_id for d, _ in self.calls) else None


fake_eng = _FakeEng()
real_engine_fn = qwen_browser.engine
qwen_browser.engine = lambda: fake_eng
try:
    doc_http2 = tickets.create_document(count=3, talon_hours=6, doc_hours=24)
    codes_http2 = tickets.document_codes(doc_http2["id"])
    r = anon.post("/admin/documents/{}/close".format(doc_http2["id"]))
    snap_close = (r.json().get("cleanup") or {})
    check("закрытие возвращает сводку чистки", r.status_code == 200
          and snap_close.get("total") == 3, str(r.json())[:160])
    check("чистке передаются ровно коды этого документа",
          fake_eng.calls == [(doc_http2["id"], codes_http2)], str(fake_eng.calls))
    r = anon.get("/admin/documents/{}/cleanup".format(doc_http2["id"]))
    check("прогресс читается отдельно от закрытия",
          r.status_code == 200 and (r.json().get("cleanup") or {}).get("left") == 2,
          str(r.json())[:120])
    r = anon.get("/admin/documents/{}/cleanup".format(424242))
    check("несуществующему документу прогресса нет", r.json().get("cleanup") is None, str(r.json()))
    stranger = TestClient(app)
    r = stranger.get("/admin/documents/{}/cleanup".format(doc_http2["id"]))
    check("без ключа прогресс закрыт", r.status_code in (401, 503), str(r.status_code))
    r = anon.get("/admin")
    check("на странице есть строка прогресса чистки",
          "удаляем чаты" in r.text and "id=\"clean\"" in r.text, str(r.status_code))
    r = anon.post("/admin/documents/{}/close".format(424242))
    check("закрытие несуществующего документа не ставит чистку",
          r.status_code == 404 and len(fake_eng.calls) == 1, str(r.status_code))
finally:
    qwen_browser.engine = real_engine_fn

print("\n20. Окно не лезет на передний план; мёртвый браузер пересобирается")

DEAD = "BrowserContext.new_page: Target page, context or browser has been closed"


class _Osa:
    """Псевдо-System Events: каждый следующий взгляд отвечает тем, кто активен.

    Ответы читаются по порядку, последний повторяется. Команды пишет и фоновая
    нить, поэтому проверяется состав команд, а не их точное число.
    """

    def __init__(self, answers, boom=False):
        self.answers = list(answers)
        self.calls = []
        self.boom = boom
        self.lock = threading.Lock()

    def __call__(self, cmd, **kw):
        with self.lock:
            self.calls.append(cmd[2])
            i = len(self.calls) - 1
            answer = self.answers[min(i, len(self.answers) - 1)]
            if self.boom:
                raise RuntimeError("Функции Упрощённого доступа не разрешены")
        return type("Res", (), {"stdout": answer})()

    def activated(self):
        return [c for c in self.calls if "set frontmost of process" in c]


def slow_launch(box):
    """Запуск браузера идёт секунды — за это время окно успевает вылезти вперёд."""

    def go():
        time.sleep(0.2)
        box.append("запуск")
        return "КОНТЕКСТ"

    return go


cls = []
took = _Osa(["Yandex", "Google Chrome"])
res = qwen_browser.launch_with_focus_back(slow_launch(cls), True, run=took, wait=0.05, nudges=6)
check("браузер запущен ровно один раз", res == "КОНТЕКСТ" and cls == ["запуск"], str(cls))
check("до старта спрошено, кто был активен",
      "frontmost is true" in took.calls[0], str(took.calls)[:160])
check("пока впереди Chrome, фокус возвращают прежнему приложению",
      len(took.activated()) >= 2 and "Yandex" in took.activated()[0],
      "{} команд возврата".format(len(took.activated())))

cls = []
switched = _Osa(["Yandex", "TextEdit"])
qwen_browser.launch_with_focus_back(slow_launch(cls), True, run=switched, wait=0.05, nudges=6)
time.sleep(0.25)
check("если человек сам ушёл в другое приложение, фокус у него не отбирают",
      switched.activated() == [] and cls == ["запуск"], str(switched.activated()))

cls = []
hidden = _Osa(["Qoder"])
qwen_browser.launch_with_focus_back(lambda: cls.append("запуск") or "КОНТЕКСТ", False,
                                    run=hidden, wait=0.05, nudges=3)
check("в скрытом режиме про фокус не спрашиваем и команд не шлём",
      hidden.calls == [] and cls == ["запуск"], str(hidden.calls))

silent = _Osa([""])
cls = []
check("если активное приложение неизвестно, браузер всё равно стартует",
      qwen_browser.launch_with_focus_back(lambda: cls.append("запуск") or "КОНТЕКСТ",
                                          True, run=silent, wait=0.05, nudges=3) == "КОНТЕКСТ"
      and len(silent.calls) == 1, str(silent.calls))

broken = _Osa(["Qoder"], boom=True)
real_log_fn2 = qwen_browser.cli.log
logged_broken = []
qwen_browser.cli.log = lambda m: logged_broken.append(m)
try:
    try:
        got = qwen_browser.launch_with_focus_back(lambda: "КОНТЕКСТ", True, run=broken,
                                                  wait=0.05, nudges=2)
    except Exception as exc:
        got = "исключение: " + str(exc)
finally:
    qwen_browser.cli.log = real_log_fn2
check("отказ System Events не роняет запуск браузера", got == "КОНТЕКСТ", str(got))
check("но отказ видно в логе", any("System Events" in l for l in logged_broken), str(logged_broken))

check("смерть браузера узнаётся по тексту", qwen_browser.browser_gone(DEAD) is True)
check("обычный отказ не считается смертью браузера",
      qwen_browser.browser_gone(qwen_browser.LOGIN_FAILED) is False
      and qwen_browser.browser_gone("чат снова открылся по своему адресу") is False)
check("английский текст библиотеки наружу не выходит",
      "BrowserContext" not in qwen_browser.humanize(RuntimeError(DEAD))
      and "closed" not in qwen_browser.humanize(RuntimeError(DEAD)).lower(),
      qwen_browser.humanize(RuntimeError(DEAD)))
check("русская подсказка по-прежнему проходит как своя",
      qwen_browser.humanize(RuntimeError(qwen_browser.LOGIN_FAILED)) == qwen_browser.LOGIN_FAILED)

eng_d = qwen_browser.Engine()
eng_d._started_once = True
eng_d._ctx = object()
eng_d._pw = object()
eng_d._pages = {"7007": object()}
eng_d._logged = True
real_log_fn3 = qwen_browser.cli.log
qwen_browser.cli.log = lambda m: None
try:
    box_d, _ = eng_d.submit("вопрос", "7007")
    attempts = []

    def _always_dead(*a):
        attempts.append(1)
        raise RuntimeError(DEAD)

    eng_d._begin = _always_dead
    eng_d._start_ready()
    errs_d = []
    while not box_d.empty():
        kind, payload = box_d.get_nowait()
        if kind == "error":
            errs_d.append(payload)
    check("после смерти браузера контекст забыт и вход будет проверен заново",
          eng_d._ctx is None and eng_d._pw is None and eng_d._pages == {}
          and eng_d._logged is False, str(eng_d._ctx))
    check("повторов бесконечно нет: попытка ровно две, потом честный отказ",
          len(attempts) == 2 and len(errs_d) == 1, "{} попыток, {}".format(len(attempts), errs_d))
    check("талону достался человеческий отказ, а не текст Playwright",
          "BrowserContext" not in " ".join(str(e) for e in errs_d), str(errs_d)[:160])

    eng_r = qwen_browser.Engine()
    eng_r._started_once = True
    eng_r._ctx, eng_r._logged = object(), True
    box_r, _ = eng_r.submit("вопрос", "7009")
    tries = []

    def _dies_once(code, text, box):
        tries.append(code)
        if len(tries) == 1:
            raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
        eng_r._active[code] = code

    eng_r._begin = _dies_once
    eng_r._start_ready()
    errs_r = []
    while not box_r.empty():
        kind, payload = box_r.get_nowait()
        if kind == "error":
            errs_r.append(payload)
    check("закрытое руками окно не стоит человеку отказа: талон поднят со второй попытки",
          len(tries) == 2 and "7009" in eng_r._active and errs_r == [], str(errs_r))

    eng_d._ctx, eng_d._pages, eng_d._logged = object(), {"7008": 1}, True
    box_d2, _ = eng_d.submit("вопрос 2", "7008")

    def _ours(*a):
        raise RuntimeError(qwen_browser.LOGIN_FAILED)

    eng_d._begin = _ours
    eng_d._start_ready()
    check("живой браузер не пересобирается из-за обычного отказа",
          eng_d._ctx is not None and eng_d._logged is True)
finally:
    qwen_browser.cli.log = real_log_fn3

print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not fails else "ПАДЕНИЯ: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
