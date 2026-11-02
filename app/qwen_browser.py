"""Qwen как источник ответа: отдельный лист браузера на каждый талон.

Браузер живёт в одном потоке (Playwright здесь синхронный, а сервер асинхронный),
но цикл опроса общий и проходит за один шаг по всем активным талонам. Поэтому
очереди между РАЗНЫМИ талонами нет — ждать можно только повторного вопроса в
тот же талон, где два ответа в одном разговоре и правда невозможны.

Селекторы, тайминги и логин не переписаны: qwwen/qwen.py импортируется как есть.
"""
import asyncio
import functools
import importlib.util
import json
import os
import pathlib
import queue
import re
import subprocess
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
QWEN = "qwen"
CHAT_PREFIX = "aitalon"
POLL_SEC = 0.5
QUIET_TICKS = 2  # сколько подряд шагов не видно кнопки «стоп» — считаем ответ готовым
WATCHDOG_SEC = 420
# Сколько ждём, пока страница признает нас вошедшими, и сколько раз подряд она
# должна это подтвердить. См. logged_in_soon.
LOGIN_WAIT_SEC = 20
LOGIN_POLL_SEC = 0.5
LOGIN_CONFIRM = 2


def _load_cli():
    path = ROOT / "qwwen" / "qwen.py"
    if not path.exists():
        raise RuntimeError("Не найден " + str(path))
    spec = importlib.util.spec_from_file_location("qwwen_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()

PROFILE_DIR = pathlib.Path(os.environ.get("QWEN_PROFILE") or (ROOT / "qwwen" / "profile"))


def _generating(page):
    """Признак идущей генерации — тот же, что в qwen.wait_answer_stable."""
    btn = page.locator('.stop-button, button[aria-label="Стоп"], button[aria-label="Stop"]')
    return bool(btn.count()) and btn.first.is_visible()


def poll_until(check, wait_sec, poll_sec=LOGIN_POLL_SEC, confirm=1,
               sleep=time.sleep, now=time.time):
    """Опрос признака с ограниченным окном: True, если он продержался `confirm` раз подряд.

    Всё, что рисует Qwen после перехода, появляется асинхронно, и живой прогон
    дважды показал одну и ту же ловушку: один взгляд на страницу врет. Поэтому
    здесь никогда нет «просто подождать секунду».
    """
    stop = now() + wait_sec
    seen = 0
    while True:
        if check():
            seen += 1
            if seen >= confirm:
                return True
        else:
            seen = 0
        if now() >= stop:
            return False
        sleep(poll_sec)


def logged_in_soon(check, wait_sec=None, poll_sec=None, confirm=None,
                   sleep=time.sleep, now=time.time):
    """True — если в течение окна проверка дважды подряд сказала «вошедший».

    cli.is_logged_in узнаёт гостя по ОТСУТСТВИЮ кнопки «Войти» и по URL, а сразу
    после goto SPA ещё не достроен: живой прогон показал «Гостевой режим —
    выполняю вход» при полностью живом аккаунте (в сайдбаре видны прошлые чаты).
    Поверив одному такому ответу, движок уходил на /auth и там падал по таймауту.
    Поэтому гостем считаем только то, что продержалось всё окно, а вход — то, что
    подтвердилось подряд (до гидратации признаков гостя тоже ещё нет).

    Пороги читаются в момент вызова: проверки гоняют окно на сокращённом, иначе
    одна же проверка спала бы двадцать секунд.
    """
    return poll_until(check,
                      LOGIN_WAIT_SEC if wait_sec is None else wait_sec,
                      LOGIN_POLL_SEC if poll_sec is None else poll_sec,
                      LOGIN_CONFIRM if confirm is None else confirm,
                      sleep=sleep, now=now)


LOGIN_FAILED = ("Qwen не пускает в аккаунт: вход на сайт не получился. "
                "Повтори вопрос через минуту; если повторится — скажи оператору")


# Признаки того, что браузер или лист больше не живы: после такого надо не
# пересобирать ответ, а поднимать новый браузер.
DEAD_MARKS = ("has been closed", "target closed", "browser has been closed",
              "crashed", "connection closed", "target page, context or browser")


def browser_gone(text):
    low = " ".join(str(text).split()).lower()
    return any(mark in low for mark in DEAD_MARKS)


def humanize(exc):
    """Наружу — простое слово, техническая подробность остаётся в логе сервера.

    Живой прогон показал, что человек читал в чате «Locator.click: Timeout
    30000ms exceeded ... input[name="email"]», а после того как окно закрыли
    руками — «BrowserContext.new_page: Target page, context or browser has been
    closed». Разбор один: свои тексты мы пишем по-русски и пропускаем их как есть,
    всё остальное — текст библиотеки, наружу его отдавать нельзя.
    """
    text = " ".join(str(exc).split())
    if re.search(r"[а-яё]", text, re.I):
        return text[:300]
    return ("Qwen не отвечает: окно браузера закрылось или страница не открылась. "
            "Повтори вопрос; если повторится — скажи оператору")


def scrub(text):
    """Вырезает из текста креды.

    Playwright в «Call log» печатает атрибуты элемента, включая value: у поля
    входа туда попадает набранный адрес почты. Лог сервера — не место для них.
    Срезаем до усечения, иначе обрезка оставит хвост секрета.
    """
    for secret in (os.getenv("QWEN_EMAIL"), os.getenv("QWEN_PASSWORD")):
        if secret:
            text = text.replace(secret, "…")
    return text


# Разметку удаления сняли живой пробой (дампы в /tmp/qwen_delete_probe): пункт меню
# «Удалить», окно `.chat-item-delete-confirm` с кнопками «Отменить» и «Удалить».
# Открытый чат помечен в сайдбаре классом `chat-item-drag-web-active`, и он ровно
# один: поэтому меню берётся внутри этой метки, а не «первым по позиции» — при
# параллельных талонах по позиции первым лежит чужой только что созданный чат.
CHAT_OPEN_WAIT_SEC = 20
CHAT_STEP_WAIT_SEC = 12
CHAT_GONE_WAIT_SEC = 20
DELETE_POLL_SEC = 0.5
GONE_CONFIRM = 2  # «исчез» должно повториться: редирект на корень делает клиентский JS

CHAT_DOM_JS = """
() => {
  const web = document.querySelector('.chat-item-drag-web-active');
  const item = [...document.querySelectorAll('[role="menuitem"]')]
    .find(e => (e.textContent || '').trim() === 'Удалить');
  const dialog = document.querySelector('.chat-item-delete-confirm');
  const yes = dialog ? [...dialog.querySelectorAll('button')]
      .find(b => (b.textContent || '').trim() === 'Удалить') : null;
  return { menu: !!(web && web.querySelector('button[aria-label="Chat Menu"]')),
           item: !!item, dialog: !!dialog, yes: !!yes,
           active: !!document.querySelector('.chat-item-drag-active') };
}
"""

CHAT_CLICK_JS = """
(target) => {
  if (target === 'menu') {
    const web = document.querySelector('.chat-item-drag-web-active');
    const b = web && web.querySelector('button[aria-label="Chat Menu"]');
    if (!b) return false;
    b.click(); return true;
  }
  if (target === 'item') {
    const e = [...document.querySelectorAll('[role="menuitem"]')]
      .find(x => (x.textContent || '').trim() === 'Удалить');
    if (!e) return false;
    e.click(); return true;
  }
  if (target === 'yes') {
    const d = document.querySelector('.chat-item-delete-confirm');
    const b = d && [...d.querySelectorAll('button')]
      .find(x => (x.textContent || '').trim() === 'Удалить');
    if (!b) return false;
    b.click(); return true;
  }
  return false;
}
"""


def classify_url(url, chat_id):
    """«ours» — открыт этот чат, «gone» — сайт вернул главную, «other» — всё остальное.

    Проба на живом аккаунте: адрес удалённого чата `/c/<id>` после загрузки JS
    уводит на корень, а живого — остаётся на месте. На /auth или на чужом чате
    удалять нельзя, поэтому они отдельно как «other».
    """
    if not chat_id:
        return "other"
    if "/auth" in url:
        return "other"
    found = cli.get_chat_id_from_url(url)
    if found == chat_id:
        return "ours"
    if found:
        return "other"
    return "gone"


def open_verdict(page, chat_id, wait_sec=CHAT_OPEN_WAIT_SEC, poll_sec=DELETE_POLL_SEC,
                 confirm=GONE_CONFIRM, sleep=time.sleep, now=time.time):
    """Чем кончился переход к чату: «ours», «gone» или «other».

    Сразу после `goto` адрес всегда наш — его задал браузер, а решение «такого
    чата нет» принимает уже клиентский JS и уводит на главную. Живой прогон
    показал это на трёх чатах, которые были удалены, но записались «не удалось»:
    поверив первому взгляду, движок ждал кнопку меню на уже ушедшей странице.
    Поэтому и «открыт», и «исчез» засчитываются только когда адрес продержался
    дважды подряд и сайдбар это подтверждает своим активным пунктом.
    """
    stop = now() + wait_sec
    ours = gone = 0
    while True:
        kind = classify_url(page.url, chat_id)
        if kind == "ours":
            ours += 1
            gone = 0
            if ours >= confirm and page.evaluate(CHAT_DOM_JS)["active"]:
                return "ours"
        elif kind == "gone":
            gone += 1
            ours = 0
            if gone >= confirm and not page.evaluate(CHAT_DOM_JS)["active"]:
                return "gone"
        else:
            ours = gone = 0
        if now() >= stop:
            return "other"
        sleep(poll_sec)


def delete_chat(page, chat_id, sleep=time.sleep, now=time.time):
    """Удаляет один чат Qwen. Возвращает "deleted" | "gone" | "failed".

    Ни один клик не принимается на веру: «deleted» ставим только тогда, когда
    повторный заход по адресу чата его не открывает. Пока такого доказательства
    нет, запись в chats.json остаётся на месте, а оператор видит «не удалось».
    Причина каждого отказа пишется в лог: без неё «не удалось» — пустое слово,
    и починить его нельзя.
    """
    def fail(why):
        cli.log("удаление {}: {} (адрес {})".format(chat_id, why, scrub(page.url)[:120]))
        return "failed"

    if not chat_id:
        return fail("привязки талона к чату нет")

    def state():
        return page.evaluate(CHAT_DOM_JS)

    page.goto(cli.BASE_URL + "/c/" + chat_id, wait_until="domcontentloaded")
    verdict = open_verdict(page, chat_id, sleep=sleep, now=now)
    if verdict == "gone":
        return "gone"
    if verdict != "ours":
        return fail("чат не открылся")

    if not poll_until(lambda: state()["menu"], CHAT_STEP_WAIT_SEC, DELETE_POLL_SEC,
                      sleep=sleep, now=now):
        return fail("кнопки меню этого чата не видно")
    if not page.evaluate(CHAT_CLICK_JS, "menu"):
        return fail("меню не открылось с клика")
    if not poll_until(lambda: state()["item"], CHAT_STEP_WAIT_SEC, DELETE_POLL_SEC,
                      sleep=sleep, now=now):
        return fail("пункта «Удалить» в меню нет")
    if not page.evaluate(CHAT_CLICK_JS, "item"):
        return fail("пункт «Удалить» не нажался")
    if not poll_until(lambda: state()["dialog"], CHAT_STEP_WAIT_SEC, DELETE_POLL_SEC,
                      sleep=sleep, now=now):
        return fail("окна подтверждения нет")
    if not page.evaluate(CHAT_CLICK_JS, "yes"):
        return fail("кнопка подтверждения не нажалась")
    if not poll_until(lambda: not state()["dialog"], CHAT_GONE_WAIT_SEC, DELETE_POLL_SEC,
                      sleep=sleep, now=now):
        return fail("окно подтверждения не закрылось")

    # Доказательство — не признаки на уже ушедшей странице, а повторный заход:
    # живой прогон показал, что адрес удалённого чата может стоять на месте,
    # тогда как сайт его уже не открывает.
    page.goto(cli.BASE_URL + "/c/" + chat_id, wait_until="domcontentloaded")
    if open_verdict(page, chat_id, wait_sec=CHAT_STEP_WAIT_SEC, sleep=sleep, now=now) == "gone":
        return "deleted"
    return fail("чат снова открылся по своему адресу")



class Watch:
    """Состояние наблюдения за одним ответом. Чистая функция состояния: ни спит,
    ни лезет в браузер — движок сам приносит ей текст, счётчик и признак
    генерации. Поэтому её можно прогнать на смоделированном сайте.

    Наружу отдаётся ВЕСЬ текущий текст блока, а не прирост. Замер по живому
    чату показал: из 35 шагов опроса 6 раз текст не был продолжением
    предыдущего — сайт переписывает середину блока по мере доформатирования
    markdown. Склейка «по префиксу» на этом удваивала ответ (замер: раздувание
    в 4–5 раз на длинных ответах).
    """

    def __init__(self, before_count, deadline):
        self.start = time.time()
        self.before_count = before_count
        self.deadline = deadline
        self.emitted = ""
        self.generation = False
        self.last_text = None
        self.stable_since = None
        self.quiet = 0
        self.finished = False
        self.timed_out = False

    def feed(self, text, count, generating, now):
        """Возвращает (текст для показа целиком, завершился ли ответ)."""
        out = ""
        if text:
            # Началом генерации считается ТОЛЬКО появление нового блока.
            # Изменение текста — не признак: при открытии чата с оборванным
            # ответом Qwen дописывает старый блок, и по изменению текста движок
            # принимал чужой прошлый ответ за новый (замер: 5938 символов за 8,6 с).
            if not self.generation and count > self.before_count:
                self.generation = True
            if self.generation and text != self.emitted:
                out = text
                self.emitted = text
            if text == self.last_text:
                if self.stable_since is None:
                    self.stable_since = now
            else:
                self.stable_since = now
                self.last_text = text

        if not self.generation:
            if now >= self.deadline:
                self.finished = self.timed_out = True
            return out, False

        if generating:
            self.quiet = 0
        else:
            self.quiet += 1
            if self.emitted and self.quiet >= QUIET_TICKS:
                self.finished = True
                return out, True

        if self.emitted and self.stable_since and (now - self.stable_since) >= cli.STABLE_FALLBACK_SEC:
            self.finished = True
        elif now >= self.deadline:
            self.finished = self.timed_out = True
        return out, self.finished


CHOICE_PHRASE = "Предпочитаю этот ответ"

# Разметку панели подтвердить нечем: в девяти сохранённых дампах Qwen её нет,
# а живой экземпляр я не могу вызвать по желанию. Поэтому элементы ищем по
# тексту, а тег не предполагаем: поднимаемся от найденной надписи до ближайшего
# кликабельного предка и уже его смотрим на видность и готовность.
CHOICE_SCAN_JS = """
(phrase) => {
  const hits = [...document.querySelectorAll('*')].filter(
    e => (e.textContent || '').trim() === phrase);
  const leaves = hits.filter(e => !hits.some(c => c !== e && e.contains(c)));
  return leaves.map(e => {
    let n = e, t = null;
    for (let k = 0; k < 6 && n; k++) {
      if (n.tagName === 'BUTTON' || n.tagName === 'A' || n.getAttribute('role') === 'button') { t = n; break; }
      n = n.parentElement;
    }
    t = t || e;
    const r = t.getBoundingClientRect(), st = getComputedStyle(t);
    return { visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden',
             enabled: !t.disabled && st.pointerEvents !== 'none' };
  });
}
"""

CHOICE_CLICK_JS = """
(index) => {
  const phrase = %s;
  const hits = [...document.querySelectorAll('*')].filter(
    e => (e.textContent || '').trim() === phrase);
  const leaves = hits.filter(e => !hits.some(c => c !== e && e.contains(c)));
  const el = leaves[index];
  if (!el) return false;
  let n = el, t = null;
  for (let k = 0; k < 6 && n; k++) {
    if (n.tagName === 'BUTTON' || n.tagName === 'A' || n.getAttribute('role') === 'button') { t = n; break; }
    n = n.parentElement;
  }
  (t || el).click();
  return true;
}
""" % repr(CHOICE_PHRASE)


CHOICE_ASK_PHRASE = "Какой ответ вы предпочитаете"
CHOICE_PENDING_JS = """
(phrase) => [...document.querySelectorAll('*')].some(
  e => e.children.length === 0 && (e.textContent || '').trim().startsWith(phrase))
"""


def choice_pending(page):
    """Отдельно от кнопки: сайт прямо спрашивает, какой ответ предпочесть.

    Нужен, чтобы редкая и случайная панель не превращалась в молчаливое зависание:
    если спросила, а нажать не вышло, мы обязаны сказать это человеку.
    """
    try:
        return bool(page.evaluate(CHOICE_PENDING_JS, CHOICE_ASK_PHRASE))
    except Exception:
        return False


def pick_last_ready(items):
    """Индекс последнего видимого и готового варианта, либо -1.

    Последний — потому что именно его текст мы уже отдали человеку как ответ
    (замер: последний блок в DOM совпадает с «Ответ 2»).
    """
    for i in range(len(items) - 1, -1, -1):
        it = items[i]
        if it.get("visible") and it.get("enabled"):
            return i
    return -1


def resolve_choice(page):
    """Разбирает панель «Какой ответ вы предпочитаете?».

    Qwen иногда генерирует два варианта и встаёт в ожидание выбора: пока его не
    сделают, чат может не принять следующий вопрос. Пока оба варианта не готовы,
    не трогаем — нажатая неактивная кнопка ничего не даст.
    """
    try:
        items = page.evaluate(CHOICE_SCAN_JS, CHOICE_PHRASE) or []
    except Exception:
        return False
    i = pick_last_ready(items)
    if i < 0:
        return False
    try:
        clicked = page.evaluate(CHOICE_CLICK_JS, i)
    except Exception:
        return False
    if clicked:
        cli.log("панель выбора ответов: выбран вариант {} из {}".format(i + 1, len(items)))
    return bool(clicked)


def rename_ours(page, chat_name, question):
    """Переименование верхнего чата — только если наверху действительно наш.

    Идентификатора чата в разметке сайдбара нет (проверено по дампу страницы:
    ни href, ни data-id), и cli.rename_top_chat берёт первый элемент по позиции.
    При параллельных талонах первым может оказаться чужой только что созданный
    чат, тогда мы перепишем его название. Сравниваем видимый заголовок с началом
    нашего вопроса и молчим, если не совпало: привязка талона к чату идёт по id
    из URL, поэтому безымянный чат безопаснее чужого переименованного.
    """
    # Сайт сам заглавную букву поднимает: «тест пипирки» превращается в «Тест
    # Пипирки». Без приведения к нижнему регистру проверка никогда бы не
    # совпадала, и чаты оставались бы безымянными.
    key = " ".join(question.split())[:10].lower()
    try:
        title = " ".join((page.locator('a.chat-item-drag-link').first.inner_text(timeout=3000) or "").split()).lower()
    except Exception:
        return False
    if key and key in title:
        cli.rename_top_chat(page, chat_name)
        return True
    cli.log("Переименование пропущено: наверху «{}», наш вопрос «{}»".format(title[:40], key))
    return False


FRONTMOST_SCRIPT = 'tell application "System Events" to name of first process whose frontmost is true'
CHROME_APP = "Google Chrome"
# Окно поднимает себя не только при старте браузера, но и при создании первой
# вкладки, а это ~20 с набора текста. Поэтому сторож живёт полминуты, но только
# пока впереди сам Chrome: если человек за это время сам открыл окно, не трогаем.
FOCUS_NUDGE_SEC = 0.7
FOCUS_NUDGES = 45


def run_script(script, run=subprocess.run):
    """Одна команда osascript. Пустой ответ — не беда: фокус это украшение, а не работа."""
    try:
        res = run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return (res.stdout or "").strip() or None
    except Exception as exc:
        cli.log("System Events не ответил: {}".format(str(exc)[:80]))
        return None


def activate_script(name):
    return ('tell application "System Events" to set frontmost of process {} to true'
            .format(json.dumps(name)))


def launch_with_focus_back(launch, visible, run=subprocess.run, wait=FOCUS_NUDGE_SEC,
                           nudges=FOCUS_NUDGES):
    """Запускает браузер и не даёт его окну остаться поверх чужой работы.

    Замер живого запуска: Chrome поднимает окно через ~1 с и держит передний план
    ~6 с, пока не вернётся вызов запуска браузера. Поэтому фокус возвращаем не
    только в конце, а по ходу: каждые 0,7 с, но только пока впереди действительно
    Chrome — если человек за это время сам переключился на другое приложение,
    тянуть его обратно не надо. В скрытом режиме вопросов о фокусе нет.
    """
    if not visible:
        return launch()
    prev = run_script(FRONTMOST_SCRIPT, run)
    if not prev:
        return launch()
    stop = threading.Event()

    def nudge():
        for _ in range(nudges):
            if stop.wait(wait):
                return
            if run_script(FRONTMOST_SCRIPT, run) == CHROME_APP:
                run_script(activate_script(prev), run)

    thread = threading.Thread(target=nudge, daemon=True)
    thread.start()
    try:
        ctx = launch()
        if run_script(FRONTMOST_SCRIPT, run) == CHROME_APP:
            run_script(activate_script(prev), run)
        return ctx
    finally:
        stop.set()


class _Running:
    def __init__(self, code, text, box, page, chat_name, created, watch):
        self.code, self.text, self.box, self.page = code, text, box, page
        self.chat_name, self.created, self.watch = chat_name, created, watch
        self.choice_warned = False


# Что человек видит в операторской по каждому талону закрытого документа.
CLEAN_TEXT = {"queued": "ждёт", "deleted": "удалён", "gone": "уже нет",
              "missing": "чата нет", "failed": "не удалось"}


class _Clean:
    """Чистка чатов одного закрытого документа.

    Список кодов и их чаты считаются сразу, браузер для этого не нужен: талон,
    который ни о чём не спрашивал, не имеет чата, и говорить о нём «не удалось»
    было бы враньем.
    """

    def __init__(self, doc_id, chats):
        self.doc_id = doc_id
        self.state = {}
        self.jobs = []
        for code, chat_id in chats:
            if chat_id is None:
                self.state[code] = "missing"
            else:
                self.state[code] = "queued"
                self.jobs.append((code, chat_id))

    @property
    def total(self):
        return len(self.state)

    @property
    def done(self):
        return sum(1 for v in self.state.values() if v != "queued")

    def snapshot(self):
        return {"doc_id": self.doc_id, "total": self.total, "done": self.done,
                "left": len(self.jobs), "state": dict(self.state)}


class Engine(threading.Thread):
    """Один Chromium, один поток, но все талоны опрашиваются вперемешку за один шаг."""

    def __init__(self):
        super().__init__(daemon=True, name="qwen-engine")
        self._lock = threading.Lock()
        self._waiting = {}
        self._active = {}
        self._pages = {}
        self._cleans = []
        self._clean_pg = None
        self._pw = None
        self._ctx = None
        self._logged = False
        self._started_once = False

    def submit(self, text, code):
        box = queue.Queue()
        with self._lock:
            ahead = len(self._waiting.get(code, [])) + (1 if code in self._active else 0)
            self._waiting.setdefault(code, []).append((text, box))
            if not self._started_once:
                self.start()
                self._started_once = True
        return box, ahead

    def submit_cleanup(self, doc_id, codes):
        """Ставит чистку чатов закрытого документа; возвращает сводку сразу.

        Браузер поднимается только если есть что удалять: закрыть документ,
        талоны которого молчали, не должно приводить в движение Chrome.
        """
        chats = cli.load_chats()
        pairs = [(c, chats.get("{}-{}".format(CHAT_PREFIX, c))) for c in codes]
        clean = _Clean(doc_id, pairs)
        with self._lock:
            self._cleans = [c for c in self._cleans if c.doc_id != doc_id]
            self._cleans.append(clean)
            if clean.jobs and not self._started_once:
                self.start()
                self._started_once = True
        cli.log("чистка документа {}: талонов {}, чатов на удаление {}".format(
            doc_id, clean.total, len(clean.jobs)))
        return clean.snapshot()

    def cleanup_status(self, doc_id):
        with self._lock:
            for c in self._cleans:
                if c.doc_id == doc_id:
                    return c.snapshot()
        return None

    def run(self):
        while True:
            try:
                self._start_ready()
                self._tick_all()
                self._do_cleanup()
            except Exception as exc:
                self._report_everywhere(exc)
            time.sleep(POLL_SEC)

    def _start_ready(self):
        """Поднимает НЕ БОЛЕЕ ОДНОГО талона за шаг.

        Десятый прогон показал: когда все десять поднимались внутри одного
        вызова, опрос не запускался полторы минуты, и все ответы приходили
        одним куском вместо стрима.
        """
        with self._lock:
            ready = [c for c, q in self._waiting.items() if c not in self._active and q]
        if not ready:
            return
        code = ready[0]
        text, box = self._waiting[code].pop(0)
        try:
            self._begin(code, text, box)
        except Exception as exc:
            if browser_gone(str(exc)):
                cli.log("талон {}: браузер закрыт, поднимаю новый и повторяю".format(code))
                self._drop_browser()
                try:
                    self._begin(code, text, box)
                    return
                except Exception as retry_exc:
                    exc = retry_exc
            cli.log("талон {}: сбой на старте — {}".format(code, scrub(str(exc))[:500]))
            box.put(("error", humanize(exc)))
            box.put(("done", None))
            self._pages.pop(code, None)

    def _begin(self, code, text, box):
        t0 = time.time()
        page = self._page_for(code)
        t1 = time.time()
        chat_name = "{}-{}".format(CHAT_PREFIX, code)
        box.put(("note", "открываю чат «{}»".format(chat_name)))
        chat_id, created = cli.ensure_named_chat(page, chat_name)
        t2 = time.time()
        # Панель выбора могла остаться с прошлого вопроса и перекрыть поле ввода —
        # снимаем её до отправки, иначе вопрос некуда вписать.
        resolve_choice(page)
        before_count = cli.count_answers(page)
        cli.send_question(page, text)
        t3 = time.time()
        cli.log("талон {}: лист {:.1f} с, чат {:.1f} с, отправка {:.1f} с".format(
            code, t1 - t0, t2 - t1, t3 - t2))
        box.put(("note", "Qwen печатает…"))
        watch = Watch(before_count, time.time() + cli.GEN_TIMEOUT)
        with self._lock:
            self._active[code] = _Running(code, text, box, page, chat_name, created, watch)

    def _tick_all(self):
        now = time.time()
        with self._lock:
            running = list(self._active.values())
        for r in running:
            resolved = resolve_choice(r.page)
            text = cli.extract_last_answer(r.page) or ""
            count = cli.count_answers(r.page)
            generating = _generating(r.page)
            if not resolved and not generating and not r.choice_warned and choice_pending(r.page):
                r.box.put(("note", "Qwen спрашивает, какой ответ предпочесть, а нажать не вышло — посмотри окно"))
                r.choice_warned = True
            out, finished = r.watch.feed(text, count, generating, now)
            if out:
                r.box.put(("content", out))
            if finished:
                self._finish(r)

    def _do_cleanup(self):
        """Обслуживает НЕ БОЛЕЕ ОДНОГО удаления за шаг и только когда талоны молчат.

        Удаление — фоновая работа: лист для него блокирует поток движка на секунды,
        а уроком прошлых прогонов было то, что подготовка не должна вставать в одну
        строку с опросом отвечающих талонов.
        """
        with self._lock:
            if self._active:
                return
            clean = next((c for c in self._cleans if c.jobs), None)
        if clean is None:
            return
        code, chat_id = clean.jobs[0]
        try:
            page = self._clean_page()
            verdict = delete_chat(page, chat_id)
        except Exception as exc:
            cli.log("чистка: талон {} — {}".format(code, scrub(str(exc))[:400]))
            if browser_gone(str(exc)):
                self._drop_browser()
            else:
                with self._lock:
                    self._clean_pg = None
            verdict = "failed"
        clean.jobs.pop(0)
        clean.state[code] = verdict
        if verdict in ("deleted", "gone"):
            self._forget_chat(code)
        with self._lock:
            self._pages.pop(code, None)
        cli.log("чистка документа {}: талон {} — {}".format(
            clean.doc_id, code, CLEAN_TEXT.get(verdict, verdict)))

    def _forget_chat(self, code):
        """Убирает привязку талона к чату: удалять второй раз нечего.

        Список перечитывается каждый раз, потому что движок в это же время пишет
        туда новые чаты, и одна перезатёртая строка стоит человеку «чата нет».
        """
        name = "{}-{}".format(CHAT_PREFIX, code)
        chats = cli.load_chats()
        if name not in chats:
            return
        chats.pop(name, None)
        cli.save_chats(chats)

    def _clean_page(self):
        with self._lock:
            if self._clean_pg is not None:
                return self._clean_pg
        self._ensure_browser()
        page = self._ctx.new_page()
        self._ensure_login(page)
        with self._lock:
            self._clean_pg = page
        return page

    def _ensure_login(self, page):
        """Вход проверяется один раз на браузер: листы делят один контекст и cookies,
        а десять проверок подряд стоили десятой части времени прогона."""
        if self._logged:
            return
        if not logged_in_soon(functools.partial(cli.is_logged_in, page)):
            email, password = os.getenv("QWEN_EMAIL"), os.getenv("QWEN_PASSWORD")
            if not (email and password):
                raise RuntimeError("Сессия Qwen не живая, а QWEN_EMAIL/QWEN_PASSWORD не найдены — "
                                   "нужно одно из двух: рабочая сессия в профиле или креды в .env")
            try:
                cli.ensure_logged_in(page, email, password)
            except Exception as exc:
                cli.log("Вход в Qwen не удался: " + scrub(str(exc))[:500])
                raise RuntimeError(LOGIN_FAILED)
        self._logged = True

    def _finish(self, r):
        with self._lock:
            self._active.pop(r.code, None)
        answer = r.watch.emitted
        cli.log("талон {}: ответ {} символов за {:.1f} с".format(
            r.code, len(answer), time.time() - r.watch.start))
        # Панель выбора закрываем до переименования: клик по «Предпочитаю этот
        # ответ» перерисовывает сайдбар, и верхний элемент в момент записи может
        # быть уже не тот.
        resolve_choice(r.page)
        if r.created and answer:
            new_id = cli.get_chat_id_from_url(r.page.url)
            if new_id:
                chats = cli.load_chats()
                chats[r.chat_name] = new_id
                cli.save_chats(chats)
                rename_ours(r.page, r.chat_name, r.text)
        if answer:
            r.box.put(("done", {"chars_in": len(r.text), "chars_out": len(answer)}))
        else:
            msg = ("Qwen не ответил за {} секунд".format(cli.GEN_TIMEOUT) if r.watch.timed_out
                   else "Qwen не ответил — возможно, попросил проверку или сменил вёрстку")
            if choice_pending(r.page):
                msg += ". Сайт ждёт выбора между двумя ответами, а нажать не получилось"
            r.box.put(("error", msg))
            r.box.put(("done", None))
            self._pages.pop(r.code, None)

    def _report_everywhere(self, exc):
        cli.log("движок Qwen упал: {}".format(scrub(str(exc))[:500]))
        with self._lock:
            victims = list(self._active.values())
            self._active.clear()
        self._drop_browser()
        for r in victims:
            r.box.put(("error", "браузер Qwen перезапускается: " + humanize(exc)))
            r.box.put(("done", None))

    def _drop_browser(self):
        """Забывает мёртвый браузер, чтобы следующая попытка подняла новый.

        Без этого замер показал стойкий отказ: окно Chrome закрыли руками, а
        движок продолжал держать мёртвый контекст, и каждый новый вопрос талона
        получал один и тот же отказ до перезапуска сервера.
        """
        pw = self._pw
        with self._lock:
            self._ctx = None
            self._pw = None
            self._pages.clear()
            self._clean_pg = None
            self._logged = False
        if pw is not None:
            try:
                pw.stop()
            except Exception as exc:
                cli.log("старый драйвер браузера не закрылся: {}".format(str(exc)[:120]))

    def _page_for(self, code):
        page = self._pages.get(code)
        if page:
            return page
        self._ensure_browser()
        page = self._ctx.new_page()
        page.goto(cli.BASE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        cli.close_popups(page)
        self._ensure_login(page)
        self._pages[code] = page
        return page

    def _ensure_browser(self):
        if self._ctx:
            return
        from playwright.sync_api import sync_playwright

        visible = os.environ.get("QWEN_WINDOW") == "1"

        def start():
            self._pw = sync_playwright().start()
            kwargs = dict(
                user_data_dir=str(PROFILE_DIR),
                # Скрыто по умолчанию: в сайдбаре одного аккаунта видны первые вопросы
                # всех талонов. QWEN_WINDOW=1 показывает окно, но не выводит его на
                # передний план — оно нужно, когда Qwen задал проверку человеку.
                headless=not visible,
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                args=["--disable-blink-features=AutomationControlled"])
            # Встроенного Chromium под этот интерпретатор нет (1.55 ждёт сборку 1187),
            # а ставить его ради теста не будем: на машине есть настоящий Chrome.
            channel = os.environ.get("QWEN_CHANNEL", "chrome")
            if channel:
                kwargs["channel"] = channel
            return self._pw.chromium.launch_persistent_context(**kwargs)

        self._ctx = launch_with_focus_back(start, visible)


_engine = None
_engine_lock = threading.Lock()


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


async def stream(text, code):
    box, ahead = engine().submit(text, code)
    if ahead:
        yield {"type": "note", "text": "в этом талоне уже идёт ответ, ты ждёшь: {}".format(ahead)}
    while True:
        try:
            kind, payload = await asyncio.to_thread(functools.partial(box.get, timeout=WATCHDOG_SEC))
        except queue.Empty:
            yield {"type": "error", "message": "Qwen молчит дольше {} секунд — окно с чатом зависло, "
                                               "нужен человек".format(WATCHDOG_SEC)}
            return
        if kind == "content":
            yield {"type": "content", "text": payload}
        elif kind == "note":
            yield {"type": "note", "text": payload}
        elif kind == "error":
            yield {"type": "error", "message": payload}
        elif kind == "done":
            if payload is not None:
                yield {"type": "done", "usage": payload}
            return
