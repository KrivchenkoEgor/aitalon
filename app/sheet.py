"""Печатные листы талонов: QR, A4 по 10 штук, экспорт в PDF.

PDF делает тот же Chromium, что уже служит для Qwen, — отдельной библиотеки для
документов заводить не пришлось. QR рисует segno (чистый Python, ноль
зависимостей), вставляется прямо в страницу как SVG, поэтому лист остаётся
текстом и масштабируется без потерь.
"""
import os
import pathlib

import segno

from . import tickets

# Адрес зашивается в QR на момент печати листа. Берём mDNS-имя, а не IP: роутер
# может выдать машине другой адрес, а отпечатанные листки переиздавать дорого.
BASE_URL = os.environ.get("AITALON_BASE_URL", "http://MacBook.local:8000")
PER_SHEET = 10


def talon_url(code):
    return "{}/?t={}".format(BASE_URL, code)


def qr_inline(code, scale=4):
    return segno.make(talon_url(code), error="m").svg_inline(scale=scale, border=1)


def _card(doc, row):
    return """
    <div class="card">
      <div class="qr">{qr}</div>
      <div class="meta">
        <div class="big">талон {label} из {total}</div>
        <div class="doc">документ {num}</div>
        <div class="rule">действует {talon} ч от первого входа</div>
        <div class="rule">выдать до {deadline}</div>
        <div class="cap">код талона — если камера не считала, введите его руками:</div>
        <div class="code">{code}</div>
      </div>
    </div>""".format(qr=qr_inline(row["code"]), label=row["label"], total=row["total"],
                     num=doc["num"], talon=int(doc["talon_hours"]),
                     deadline=tickets.fmt_time(doc["created_at"] + doc["doc_hours"] * tickets.HOUR),
                     code=row["code"])


def sheet_html(doc_id):
    doc = tickets.document(doc_id)
    if not doc:
        return "<h1>Документ не найден</h1>"
    with tickets.db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT code, label, total FROM talons WHERE doc_id=? ORDER BY label", (doc_id,))]
    cards = "".join(_card(doc, r) for r in rows)
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Талоны {num}</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 12px/1.35 -apple-system, "Helvetica Neue", Arial, sans-serif; margin:0; color:#111; }}
  .sheet {{ width:194mm; height:281mm; display:grid; grid-template-columns: 1fr 1fr;
            grid-auto-rows: 53mm; align-content: start; gap:4mm; page-break-after: always; }}
  .sheet:last-child {{ page-break-after: auto; }}
  .card {{ border:1px solid #999; border-radius:3mm; padding:4mm; display:flex; gap:4mm;
           align-items:center; break-inside:avoid; }}
  .qr svg {{ width:34mm; height:34mm; }}
  .meta {{ flex:1; min-width:0; }}
  .big {{ font-size:16px; font-weight:700; }}
  .doc {{ color:#444; margin-top:1mm; }}
  .rule {{ color:#444; margin-top:1mm; }}
  .cap {{ margin-top:2mm; font-size:9px; color:#666; }}
  .code {{ font-family: ui-monospace, Menlo, monospace; letter-spacing:.06em; margin-top:1mm;
           word-break:break-all; font-size:12px; }}
</style></head><body>{sheets}</body></html>""".format(
        num=doc["num"],
        sheets="".join('<div class="sheet">{}</div>'.format(
            "".join(_card(doc, r) for r in rows[i:i + PER_SHEET]))
            for i in range(0, len(rows), PER_SHEET)))


def sheet_pdf(doc_id):
    """Печать листа в PDF headless-Chromium'ом. Вызывать только из рабочего
    потока (синхронный Playwright блокирует надолго)."""
    from playwright.sync_api import sync_playwright
    html = sheet_html(doc_id)
    with sync_playwright() as p:
        # Встроенного Chromium под эту версию Playwright нет (ждёт сборку 1187),
        # поэтому печатаем настоящим Chrome — как и в источнике Qwen.
        kwargs = {"headless": True}
        channel = os.environ.get("QWEN_CHANNEL", "chrome")
        if channel:
            kwargs["channel"] = channel
        browser = p.chromium.launch(**kwargs)
        try:
            page = browser.new_page(viewport={"width": 900, "height": 1200})
            page.set_content(html, wait_until="networkidle")
            return page.pdf(format="A4", print_background=True,
                            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        finally:
            browser.close()
