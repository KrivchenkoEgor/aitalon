#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qwen.py — CLI-клиент для chat.qwen.ai (Qwen Studio).

Как работает:
  1. Открывает Chromium (Playwright) с persistent-профилем (profile/) —
     логин нужен только один раз, дальше сессия переиспользуется.
  2. Если не залогинен — входит по email/паролю (креды из .env / окружения).
  3. Вводит вопрос в поле чата с человеческими таймингами, отправляет,
     ждёт завершения генерации (текст ответа стабилизировался).
  4. Сохраняет ответ:
       answers/YYYY-MM-DD_HH-MM-SS.txt   — файл на запрос
       answers/history.md                — общий лог (append)

Использование:
  python3 qwen.py "твой вопрос"
  python3 qwen.py "вопрос" --headless    # без окна браузера
  python3 qwen.py --file notes.txt "Прочитай файл и..."  # с вложением
  python3 qwen.py --chat баба --persona persona.txt ""   # создать чат «баба» и загрузить личность
  python3 qwen.py --chat баба "новый пост..."            # вопрос всегда в чат «баба»

Креды (в .env или переменных окружения):
  QWEN_EMAIL=...
  QWEN_PASSWORD=...
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


def _setup_console():
    """Windows: UTF-8 в консоли, чтобы эмодзи/кириллица печатались без сбоев."""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_setup_console()

BASE_URL = "https://chat.qwen.ai"
PROFILE_DIR = Path(__file__).resolve().parent / "profile"
ANSWERS_DIR = Path(__file__).resolve().parent / "answers"
ERRORS_DIR = Path(__file__).resolve().parent / "errors"
CHATS_FILE = Path(__file__).resolve().parent / "chats.json"

# Промпт для загрузки личности при создании именованного чата (--persona)
PERSONA_PROMPT = (
    "Прочитай внимательно вставленный текст — это история нашей переписки, где ты пишешь "
    "для меня посты для личного блога (я — женщина, веду блог о своей жизни). Изучи его "
    "внимательно и сформулируй памятку по пунктам: "
    "1) ЛИЧНОСТЬ: кто я как автор блога (голос, характер, позиционирование); "
    "2) СТИЛЬ: как писать посты (тон, лексика, эмодзи, хэштеги, структура поста, "
    "вопросы к аудитории в конце); "
    "3) ТЕМЫ И ФОРМАТ: о чём обычно посты и как они начинаются; "
    "4) ПРАВИЛА ДЛЯ БУДУЩИХ ПОСТОВ: что соблюдать при написании следующих постов. "
    "Отвечай по-русски, структурированно. Цель — чтобы ты поняла мою личность и дальше "
    "писала посты в том же стиле."
)

# Подстроки URL стрим-эндпоинтов (появление запроса = генерация началась)
STREAM_URL_PATTERNS = ("chat/completions", "stream", "chat/completions/stream")

STABLE_FALLBACK_SEC = 8  # fallback: текст не меняется столько времени -> генерация завершена
GEN_TIMEOUT = 300       # максимум на генерацию ответа, сек
LOGIN_TIMEOUT = 120     # максимум на логин (включая возможный Cloudflare challenge)
START_TIMEOUT = 45      # ждём старта генерации после отправки, сек

# JS, выполняемый на каждой странице до скриптов сайта: скрывает следы
# автоматизации (navigator.webdriver, plugins, chrome.runtime и т.п.)
STEALTH_JS = r"""
// ---- Stealth: браузер выглядит как обычный Chrome человека ----
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

if (!window.chrome) {
  window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
}
if (window.chrome && !window.chrome.runtime) {
  window.chrome.runtime = { id: undefined, connect: () => {}, sendMessage: () => {} };
}

// Плагины: обычный Chrome показывает 3-5 плагинов, автоматизация — 0
const pluginList = [
  { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
    description: 'Portable Document Format' },
  { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
    description: '' },
  { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
];
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const arr = pluginList;
    arr.item = (i) => arr[i] || null;
    arr.namedItem = (n) => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    return arr;
  },
});
Object.defineProperty(navigator, 'mimeTypes', { get: () => [] });

Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });

// permissions: для уведомлений возвращаем реальное состояние, не «prompt»
try {
  const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
  window.navigator.permissions.query = (parameters) => {
    if (parameters && parameters.name === 'notifications') {
      return Promise.resolve({ state: Notification.permission });
    }
    return origQuery(parameters);
  };
} catch (e) {}
"""


def get_chrome_version():
    """Версия установленного Chrome (для честного User-Agent)."""
    if sys.platform == "darwin":
        try:
            import plistlib
            plist = "/Applications/Google Chrome.app/Contents/Info.plist"
            with open(plist, "rb") as f:
                return plistlib.load(f).get("CFBundleShortVersionString", "")
        except Exception:
            return ""
    if sys.platform == "win32":
        # Windows: chrome.exe в стандартных местах, версию спрашиваем
        # у самого браузера (chrome.exe --version) — без реестра
        import subprocess
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for exe in candidates:
            if not exe or not os.path.exists(exe):
                continue
            try:
                out = subprocess.run([exe, "--version"], capture_output=True,
                                     text=True, timeout=10)
                m = re.search(r"\d+\.\d+\.\d+\.\d+", out.stdout or "")
                if m:
                    return m.group(0)
            except Exception:
                continue
    return ""


def verify_stealth(page):
    """Проверяем по DOM, что автоматизация скрыта (без скриншотов)."""
    try:
        res = page.evaluate(
            "() => ({ webdriver: navigator.webdriver, plugins: navigator.plugins.length, "
            "ua: navigator.userAgent.slice(0, 60) })"
        )
        hidden = res.get("webdriver") in (None, False)
        log("Stealth-проверка: webdriver={} plugins={} UA={} {}".format(
            res.get("webdriver"), res.get("plugins"), res.get("ua"),
            "OK" if hidden else "!!! ВИДЕН"))
        return hidden
    except Exception as exc:
        log("Stealth-проверка не выполнена: {}".format(exc))
        return False


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def save_error_dump(page, label):
    """Evidence при ошибке: текстовый дамп HTML и URL (не скриншот — их не используем)."""
    try:
        ERRORS_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        html_path = ERRORS_DIR / "{}_{}.html".format(label, ts)
        html_path.write_text(page.content(), encoding="utf-8")
        with (ERRORS_DIR / "errors.log").open("a", encoding="utf-8") as f:
            f.write("[{}] URL: {}\n".format(ts, page.url))
        log("Дамп ошибки: {}".format(html_path))
    except Exception as exc:  # pragma: no cover
        log("Не удалось сохранить дамп: {}".format(exc))


def extract_last_answer(page):
    """
    Полный текст последнего ответа модели.
    Контейнер ответа в Qwen Studio — .response-message-content (содержит все
    markdown-блоки одного ответа). Fallback'и — на случай смены вёрстки.
    """
    selectors = (
        '.response-message-content',
        '[data-testid*="message"] [class*="markdown"]',
        '[class*="message-content"] [class*="markdown"]',
        '[class*="markdown"]',
    )
    for sel in selectors:
        loc = page.locator(sel)
        n = loc.count()
        if n:
            try:
                return loc.nth(n - 1).inner_text(timeout=2000).strip()
            except Exception:
                continue
    return None


def close_popups(page):
    """Закрываем возможные модалки/онбординг-баннеры после входа.

    Устойчива к навигации (SPA может редиректить сразу после goto) —
    каждая проверка в try/except, при ошибке просто ждём и пробуем снова.
    """
    for _ in range(4):
        closed = False
        for sel in (
            '[aria-label="Close"]',
            '[aria-label="Закрыть"]',
            # Cookie-баннер Qwen Studio: кнопки «Принять все файлы cookie»
            # и «Принять все строго необходимые файлы cookie»
            '[class*="cookie-confirm"] button:has-text("файлы cookie")',
            'button:has-text("Принять все файлы cookie")',
            'button:has-text("Accept all")',
            'button:has-text("Принять всё")',
        ):
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    try:
                        loc.first.click(timeout=2000)
                        closed = True
                        page.wait_for_timeout(random.randint(400, 800))
                        break
                    except Exception:
                        continue
            except Exception:
                # навигация уничтожила контекст — подождём и попробуем заново
                page.wait_for_timeout(1000)
                break
        if not closed:
            break


def is_logged_in(page):
    """
    Действительно ли мы в аккаунте. Qwen даёт гостевой чат без входа,
    поэтому наличие textarea НЕ признак логина. Признаки гостя:
    страница входа /auth или видимая кнопка «Войти» в шапке.
    """
    if "/auth" in page.url or "/c/guest" in page.url:
        return False
    for sel in ('.header-right-auth-button',
                'button:has-text("Войти")',
                'button:has-text("Log in")',
                'button:has-text("Sign in")'):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return False
        except Exception:
            continue
    return True


def ensure_logged_in(page, email, password):
    """Переходим на сайт, логинимся, если сессия ещё не активна."""
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if is_logged_in(page):
        log("Уже в аккаунте (URL: {})".format(page.url))
        close_popups(page)
        return

    log("Гостевой режим — выполняю вход в аккаунт...")
    # Прямой переход на страницу входа (надёжнее, чем кликать по кнопке)
    page.goto(BASE_URL + "/auth", wait_until="domcontentloaded")

    email_input = page.locator('input[name="email"]')
    email_input.wait_for(timeout=LOGIN_TIMEOUT * 1000)

    # Имитация человека: не мгновенный fill, а набор с естественной скоростью
    email_input.click()
    email_input.type(email, delay=random.randint(15, 40))
    page.wait_for_timeout(random.randint(300, 800))

    pwd_input = page.locator('input[name="password"]')
    pwd_input.click()
    pwd_input.type(password, delay=random.randint(25, 60))
    page.wait_for_timeout(random.randint(400, 900))

    # Кнопка входа: submit-кнопка формы, fallback — по тексту.
    # Ant Design держит её disabled до валидации полей — ждём enabled.
    login_btn = page.locator('button[type="submit"]')
    if not login_btn.count():
        login_btn = page.locator('button:has-text("Войти")')
    deadline = time.time() + 20
    while time.time() < deadline and not login_btn.first.is_enabled():
        time.sleep(0.5)
    login_btn.first.click()

    # Ждём успешного входа: кнопка «Войти» пропала, появилось поле чата
    deadline = time.time() + LOGIN_TIMEOUT
    last_diag = 0
    while time.time() < deadline:
        if is_logged_in(page) and input_box(page).count():
            log("Вход выполнен. URL: {}".format(page.url))
            close_popups(page)
            page.wait_for_timeout(1000)
            return
        # Диагностика раз в 10 секунд: URL + ошибки на форме
        if time.time() - last_diag > 10:
            last_diag = time.time()
            errs = []
            for sel in ('[class*="error"]', '[class*="tips"]', '.ant-message', '[class*="captcha"]',
                        '[class*="verify"]', '[class*="code"]'):
                try:
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 3)):
                        t = loc.nth(i).inner_text(timeout=500).strip()
                        if t:
                            errs.append(t[:80])
                except Exception:
                    continue
            log("Ждём вход: URL={} errs={}".format(page.url, errs[:3] or "-"))
        time.sleep(1)
    raise RuntimeError("Не удалось войти за {} сек (Cloudflare challenge?)".format(LOGIN_TIMEOUT))


def type_like_human(locator, text):
    """Маленькие тексты печатаем с человеческой скоростью, большие — вставляем
    мгновенно (паста — человек тоже так делает)."""
    if len(text) > 500:
        locator.fill(text)
    else:
        locator.type(text, delay=random.randint(10, 30))


def input_box(page):
    """Поле ввода сообщения (в DOM есть скрытый дубль — берём видимое)."""
    loc = page.locator('textarea.message-input-textarea')
    if loc.count():
        return loc.first
    return page.locator('textarea:not([aria-hidden="true"])').first


def send_question(page, question):
    """Печатаем вопрос с человеческими таймингами и отправляем.
    Если в поле уже есть текст (например, вставленный файл) — дописываем в конец."""
    close_popups(page)  # cookie-баннер перехватывает клики по полю ввода
    ta = input_box(page)
    ta.wait_for(timeout=30000)
    ta.click()
    if ta.input_value():
        page.keyboard.press("Control+End")
        page.wait_for_timeout(200)
        if len(question) > 400:
            ta.fill(ta.input_value() + "\n\n" + question)
        else:
            type_like_human(ta, "\n\n" + question)
    else:
        type_like_human(ta, question)
    page.wait_for_timeout(random.randint(400, 1000))
    page.keyboard.press("Enter")


def click_new_chat(page):
    """Начинаем новый чат — чтобы каждый запуск был с чистого листа."""
    btn = page.locator('button:has-text("Новый чат"), [aria-label="Новый чат"]').first
    btn.wait_for(timeout=15000)
    btn.click()
    page.wait_for_timeout(1500)


# --- Именованные чаты -------------------------------------------------------

def load_chats():
    """{имя: id чата} из chats.json."""
    if CHATS_FILE.exists():
        try:
            return json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_chats(chats):
    CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=2), encoding="utf-8")


def get_chat_id_from_url(url):
    m = re.search(r"/c/([0-9a-fA-F-]+)", url)
    return m.group(1) if m else None


def rename_top_chat(page, name):
    """Переименовывает самый свежий (верхний) чат в сайдбаре.

    Кнопка меню появляется при наведении; меню → «Переименовать» → диалог с полем.
    """
    top = page.locator('a.chat-item-drag-link').first
    top.wait_for(timeout=15000)
    top.hover()
    page.wait_for_timeout(500)
    menu_btn = page.locator('button[aria-label="Chat Menu"]').first
    menu_btn.click()
    page.wait_for_timeout(600)
    page.get_by_role("menuitem", name="Переименовать").click()
    page.wait_for_timeout(600)

    inp = page.get_by_placeholder("Введите ваше сообщение")
    inp.wait_for(timeout=10000)
    inp.fill(name)
    page.wait_for_timeout(300)
    page.get_by_role("button", name="Подтвердить").click()
    page.wait_for_timeout(1500)


def ensure_named_chat(page, name):
    """
    Открывает именованный чат. Если его нет в chats.json — нажимает «Новый чат».
    Возвращает (chat_id, created_new): id может быть None — он появляется в URL
    только после первого сообщения, поэтому сохраняем его позже.
    """
    chats = load_chats()
    chat_id = chats.get(name)

    if chat_id:
        page.goto(BASE_URL + "/c/" + chat_id, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if get_chat_id_from_url(page.url) == chat_id:
            log("Открыт чат «{}» (/c/{})".format(name, chat_id))
            return chat_id, False
        log("Чат «{}» не открылся — создаю заново".format(name))
        chats.pop(name, None)

    log("Создаю новый чат...")
    new_chat_btn = page.get_by_role("button", name="Новый чат")
    new_chat_btn.wait_for(timeout=15000)
    new_chat_btn.click()
    page.wait_for_timeout(2500)
    return None, True


# --- Отправка вопроса -------------------------------------------------------


def insert_file_text(page, path):
    """Fallback: вставляем содержимое файла прямо в поле ввода."""
    content = path.read_text(encoding="utf-8")
    log("Вставляю содержимое файла в поле ввода ({} символов)".format(len(content)))
    close_popups(page)
    ta = input_box(page)
    ta.click()
    ta.fill(content)
    page.wait_for_timeout(1500)
    actual = ta.input_value()
    if actual != content:
        log("Внимание: поле обрезало текст ({} из {}) — продолжаю, но проверь результат".format(
            len(actual), len(content)))


def attach_or_insert(page, path):
    """
    Прикрепляем файл как вложение в чат (через скрытый input[type=file]).
    Если вложение не подтвердилось — вставляем текст файла в поле ввода.
    """
    path = Path(path)
    if not path.exists():
        sys.exit("Файл не найден: {}".format(path))

    file_input = page.locator('input[type="file"]')
    if file_input.count():
        try:
            file_input.first.set_input_files(str(path), timeout=15000)
            # ждём появления элемента вложения (чип с именем файла)
            deadline = time.time() + 20
            while time.time() < deadline:
                if page.locator('[class*="attachment"], [class*="file-item"], [class*="uploaded"], [class*="file-card"]').count():
                    log("Файл прикреплён как вложение: {}".format(path.name))
                    page.wait_for_timeout(2500)  # даём время на загрузку
                    return
                time.sleep(0.5)
            log("Вложение не подтвердилось — вставляю текст файла")
        except Exception as exc:
            log("Upload не сработал ({}): вставляю текст файла".format(exc))
    insert_file_text(page, path)


def wait_stream_started(page, stream_requests):
    """Ждём появления сетевого запроса к стрим-эндпоинту (генерация пошла)."""
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if stream_requests:
            return True
        # Fallback: даже без сетевого сигнала, мог появиться текст ответа
        if extract_last_answer(page):
            return True
        time.sleep(0.5)
    return False


def count_answers(page):
    """Сколько ответов модели уже отрендерено в чате (каждый ответ — один блок)."""
    return page.locator('.response-message-content').count()


def wait_answer_stable(page, before_text, before_count=None):
    """
    Ждём завершения генерации.

    Главный сигнал: кнопка «Стоп» (stop-button) видна только пока модель
    генерирует — как только она исчезла, ответ готов.
    Fallback: текст не менялся STABLE_FALLBACK_SEC подряд.
    Начало генерации определяем по СЧЁТЧИКУ ответов (а не по изменению текста:
    Qwen может ответить тем же словом, что и раньше — тогда текст не меняется,
    но новый блок ответа всё равно появляется).
    Возвращает итоговый текст или None.
    """
    if before_count is None:
        before_count = count_answers(page)
    stop_btn = page.locator('.stop-button, button[aria-label="Стоп"], button[aria-label="Stop"]')
    deadline = time.time() + GEN_TIMEOUT
    generation_seen = False
    last_text = None
    stable_since = None

    while time.time() < deadline:
        text = extract_last_answer(page)
        if text:
            if text != before_text or count_answers(page) > before_count:
                generation_seen = True
            if text == last_text:
                if stable_since is None:
                    stable_since = time.time()
            else:
                stable_since = time.time()
                last_text = text
        else:
            stable_since = None

        generating = bool(stop_btn.count()) and stop_btn.first.is_visible()

        # Генерация завершилась: кнопка «Стоп» исчезла, текст есть
        if generation_seen and not generating and last_text:
            page.wait_for_timeout(2000)  # пауза на финализацию DOM
            return extract_last_answer(page) or last_text

        # Fallback: долгая стабильность текста
        if generation_seen and stable_since and last_text \
                and (time.time() - stable_since) >= STABLE_FALLBACK_SEC:
            return last_text

        time.sleep(0.5)

    return last_text if generation_seen else None


def save_answer(question, answer):
    """Файл на запрос + append в общий лог. Возвращает путь к файлу."""
    ANSWERS_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")

    single = ANSWERS_DIR / "{}.txt".format(ts)
    single.write_text(
        "Вопрос: {q}\nДата: {d}\n{sep}\n{a}\n".format(
            q=question, d=now.strftime("%Y-%m-%d %H:%M:%S"), sep="=" * 60, a=answer
        ),
        encoding="utf-8",
    )

    history = ANSWERS_DIR / "history.md"
    with history.open("a", encoding="utf-8") as h:
        h.write(
            "## {d}\n\n**Вопрос:** {q}\n\n{a}\n\n---\n\n".format(
                d=now.strftime("%Y-%m-%d %H:%M:%S"), q=question, a=answer
            )
        )
    return single


def main():
    ap = argparse.ArgumentParser(description="Qwen CLI: вопрос -> ответ в файл")
    ap.add_argument("question", nargs="?", default=None,
                    help="Вопрос модели (можно опустить, если загружается личность)")
    ap.add_argument("--chat", default=None, metavar="NAME",
                    help="Именованный чат: открыть существующий или создать новый")
    ap.add_argument("--persona", default=None, metavar="FILE",
                    help="Файл личности — загружается при создании нового чата (--chat)")
    ap.add_argument("--file", action="append", default=None, metavar="PATH",
                    help="Файл для прикрепления к чату (можно несколько раз)")
    ap.add_argument("--headless", action="store_true", help="Без окна браузера")
    ap.add_argument("--stealth", action="store_true",
                    help="Stealth-режим: реальный Chrome + маскировка автоматизации")
    ap.add_argument("--tg-post", action="store_true",
                    help="Опубликовать ответ в Telegram-канал (TG_CHANNEL, см. tg_post.py)")
    ap.add_argument("--new-chat", action="store_true", help="Начать новый чат (устарело, используйте --chat)")
    ap.add_argument("--profile", default=str(PROFILE_DIR), help="Директория профиля (сессия)")
    ap.add_argument("--no-save", action="store_true", help="Не сохранять ответ в файлы")
    args = ap.parse_args()

    if not args.question and not (args.chat and args.persona):
        sys.exit("Ошибка: задайте вопрос или --chat с --persona")

    load_dotenv()
    email = os.getenv("QWEN_EMAIL")
    password = os.getenv("QWEN_PASSWORD")
    if not email or not password:
        sys.exit("Ошибка: задайте QWEN_EMAIL и QWEN_PASSWORD в .env или окружении")

    with sync_playwright() as p:
        launch_kwargs = dict(
            user_data_dir=args.profile,
            headless=args.headless,
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )

        if args.stealth:
            # Реальный Chrome вместо Chromium + отключение метки автоматизации.
            # Канал "chrome" работает только если Chrome установлен; иначе
            # падаем с понятной ошибкой (fallback на chromium бессмысленен —
            # пользователь явно просил stealth).
            launch_kwargs["channel"] = "chrome"
            launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
            ver = get_chrome_version()
            if ver:
                # UA под платформу: на Windows нельзя отдавать Mac-строку
                if sys.platform == "win32":
                    platform_ua = "Windows NT 10.0; Win64; x64"
                else:
                    platform_ua = "Macintosh; Intel Mac OS X 10_15_7"
                launch_kwargs["user_agent"] = (
                    "Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/{} Safari/537.36".format(platform_ua, ver)
                )
            log("Stealth: канал Chrome{}, UA={}".format(
                " " + ver if ver else " (версия не определена)",
                launch_kwargs.get("user_agent", "default")))
        else:
            launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        if args.stealth:
            context.add_init_script(STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()

        # Перехват стрим-запросов
        stream_requests = []
        page.on("request", lambda req: stream_requests.append(req.url)
                if any(pat in req.url for pat in STREAM_URL_PATTERNS) else None)

        try:
            ensure_logged_in(page, email, password)
            if args.stealth:
                verify_stealth(page)

            # Именованный чат: открываем существующий или создаём новый
            if args.chat:
                chat_id, created_new = ensure_named_chat(page, args.chat)
                # Новый чат + файл личности: загружаем личность один раз
                if created_new and args.persona:
                    log("Загружаю личность из: {}".format(args.persona))
                    attach_or_insert(page, args.persona)
                    send_question(page, PERSONA_PROMPT)
                    if not wait_stream_started(page, stream_requests):
                        raise RuntimeError("Генерация памятки не началась")
                    log("Жду завершения генерации памятки...")
                    answer = wait_answer_stable(page, None)
                    if not answer:
                        raise RuntimeError("Памятка не получена")
                    # Первое сообщение отправлено — чат создан, у него появился id
                    chat_id = get_chat_id_from_url(page.url)
                    if chat_id:
                        rename_top_chat(page, args.chat)
                        chats = load_chats()
                        chats[args.chat] = chat_id
                        save_chats(chats)
                        log("Чат «{}» создан: /c/{}".format(args.chat, chat_id))
                    else:
                        log("Внимание: не удалось получить id нового чата")
                    if not args.no_save:
                        path = save_answer("PERSONA: " + args.persona, answer)
                        log("Памятка сохранена: {}".format(path))
                    else:
                        log("Памятка получена (без сохранения)")
                    if not args.question:
                        return
            elif args.new_chat:
                click_new_chat(page)

            # Основной вопрос (с файлами-вложениями, если заданы)
            for fpath in (args.file or []):
                attach_or_insert(page, fpath)

            before_text = extract_last_answer(page)
            if args.question:
                log("Отправляю вопрос...")
                send_question(page, args.question)

                if not wait_stream_started(page, stream_requests):
                    # Возможно, Enter не отправил — пробуем кнопку отправки
                    log("Стрим не замечен, пробую кнопку отправки...")
                    for sel in ('button[aria-label*="Отправить" i]',
                                '[data-testid*="send" i]',
                                'button[aria-label*="Send" i]'):
                        btn = page.locator(sel)
                        if btn.count() and btn.first.is_visible():
                            btn.first.click()
                            break
                    if not wait_stream_started(page, stream_requests):
                        raise RuntimeError("Генерация не началась (Enter не сработал)")

                log("Генерация началась, жду завершения...")
                answer = wait_answer_stable(page, before_text)
                if not answer:
                    raise RuntimeError("Ответ не получен за {} сек".format(GEN_TIMEOUT))

                print("\n" + "=" * 60)
                print(answer)
                print("=" * 60)

                if not args.no_save:
                    path = save_answer(args.question, answer)
                    log("Сохранено: {}".format(path))
                    log("Лог: {}".format(ANSWERS_DIR / "history.md"))

                if args.tg_post:
                    log("Telegram: ставлю пост в очередь и пробую опубликовать...")
                    import subprocess
                    post_file = ANSWERS_DIR / "tg_post_pending.txt"
                    post_file.write_text(answer, encoding="utf-8")
                    res = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve().parent / "tg_post.py"),
                         "--queue-file", str(post_file)],
                        capture_output=True, text=True, timeout=120)
                    if res.stdout:
                        log(res.stdout.strip())
                    if res.stderr:
                        log("tg_post stderr: {}".format(res.stderr.strip()[:300]))
                    if res.returncode == 3:
                        # Telegram недоступен (блокировка/сбой сети) — не ошибка.
                        # Пост уже в очереди, уйдёт при восстановлении сети.
                        log("Telegram недоступен — пост в очереди (tg_queue/), "
                            "опубликуется автоматически")
                    elif res.returncode != 0:
                        raise RuntimeError("Ошибка публикации в Telegram (код {})".format(
                            res.returncode))
                    else:
                        log("Опубликовано в Telegram (очередь пуста)")

        except Exception as exc:
            log("Ошибка: {}".format(exc))
            save_error_dump(page, "error")
            raise
        finally:
            context.close()


if __name__ == "__main__":
    main()
