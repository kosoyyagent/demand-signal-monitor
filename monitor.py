#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""monitor.py — бот мониторинга сигналов спроса (демо под заказ kwork #3270853).

Источники:
  1) Яндекс-выдача: новые страницы (за 24ч) по точным фразам -> заголовок+ссылка
  2) Публичные Telegram-каналы: посты с ключевыми словами (через t.me/s/)
  3) ТПП-площадка: заглушка-пример (нужен точный URL рубрики заказчика)

Уведомления: Telegram-бот (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) или консоль/файл.
Дедупликация: SQLite seen.db (url -> first_seen).
Запуск: python monitor.py once|loop
"""
import json, os, re, sqlite3, sys, time, random, html as htmllib
import urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'seen.db')
CFG = os.path.join(BASE, 'config.json')

YANDEX_QUERIES = [
    'купить с логотипом игрушку оптом',
    'мягкая игрушка с логотипом на заказ',
    'корпоративный мерч мягкая игрушка',
    'сувенирная игрушка под бренд',
    'поставщик мягких игрушек для мерча',
    'прикольный новогодний подарок мягкая игрушка',
    'смешная мягкая игрушка купить',
]
TG_KEYWORDS = ['игрушка', 'мерч', 'сувенир', 'подарок']
TG_CHANNELS = ['freelance_zakazy']  # пример; список каналов задаёт заказчик

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')


def db_init():
    con = sqlite3.connect(DB)
    con.execute('CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY, src TEXT, title TEXT, ts REAL)')
    con.commit()
    return con


def seen(con, url):
    return con.execute('SELECT 1 FROM seen WHERE url=?', (url,)).fetchone() is not None


def mark(con, url, src, title):
    con.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?,?)', (url, src, title, time.time()))
    con.commit()


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'})
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'ignore')


def _parse_yandex_html(r):
    out = []
    for m in re.finditer(r'<a[^>]+href="(https?://(?!yandex)[^"]+)"[^>]*>(.*?)</a>', r):
        u, t = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        d = urllib.parse.urlparse(u).netloc
        if t and 'yandex' not in d and 'ya.ru' not in d:
            out.append((htmllib.unescape(t)[:120], u))
    return out[:10]


def _yandex_via_browser(url):
    """Яндекс через реальный Chrome (CDP:9222) — обход капчи без прокси."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp('http://localhost:9222')
        pg = b.contexts[0].new_page()
        try:
            pg.goto(url, wait_until='domcontentloaded', timeout=30000)
            pg.wait_for_timeout(3500)
            return pg.content()
        finally:
            pg.close()


def yandex_new_links(query, use_browser=True):
    """Органика Яндекса за последние 24 часа (within=77). [(title,url)]"""
    q = urllib.parse.quote('"%s"' % query)
    url = f'https://yandex.ru/search/?text={q}&within=77&lr=213'
    try:
        r = fetch(url)
    except Exception:
        r = ''
    if 'showcaptcha' in r.lower() or 'Вы не робот' in r or not r:
        if not use_browser:
            return [('__CAPTCHA__', 'яндекс показал капчу')]
        try:
            r = _yandex_via_browser(url)
        except Exception as e:
            return [('__ERR__', str(e))]
    return _parse_yandex_html(r)


def tg_channel_posts(channel):
    """Свежие посты канала через t.me/s/ -> [(id, text, link)]"""
    try:
        r = fetch(f'https://t.me/s/{channel}')
    except Exception:
        return []
    out = []
    for m in re.finditer(r'data-post="(%s/\d+)"(.{0,40000}?)(?=data-post=|\Z)' % channel, r, re.S):
        post_id, chunk = m.group(1), m.group(2)
        tm = re.search(r'datetime="([^"]+)"', chunk)
        tx = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', chunk, re.S)
        text = re.sub(r'<[^>]+>', ' ', tx.group(1)) if tx else ''
        text = htmllib.unescape(re.sub(r'\s+', ' ', text)).strip()
        out.append((post_id, text, tm.group(1) if tm else ''))
    return out[-30:]


def notify(msg, cfg):
    tok, chat = cfg.get('telegram_bot_token'), cfg.get('telegram_chat_id')
    if tok and chat:
        u = f'https://api.telegram.org/bot{tok}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': chat, 'text': msg, 'disable_web_page_preview': 'true'}).encode()
        try:
            urllib.request.urlopen(u, data, timeout=15)
            return 'tg'
        except Exception:
            pass
    print('NOTIFY>', msg[:200].replace('\n', ' | '))
    with open(os.path.join(BASE, 'notifications.log'), 'a', encoding='utf-8') as f:
        f.write(msg + '\n---\n')
    return 'file'


def run_once(con, cfg):
    stats = {'yandex': 0, 'tg': 0}
    for q in cfg.get('yandex_queries', YANDEX_QUERIES):
        for title, url in yandex_new_links(q):
            if title.startswith('__'):
                print('yandex:', title, url)
                continue
            if not seen(con, url):
                mark(con, url, 'yandex', title)
                notify(f'🔍 Яндекс [{q}]\n{title}\n{url}', cfg)
                stats['yandex'] += 1
        time.sleep(random.uniform(3, 6))  # мягкий темп против капчи
    for ch in cfg.get('tg_channels', TG_CHANNELS):
        for post_id, text, ts in tg_channel_posts(ch):
            url = f'https://t.me/{post_id}'
            low = text.lower()
            if text and any(k in low for k in cfg.get('tg_keywords', TG_KEYWORDS)) and not seen(con, url):
                mark(con, url, 'tg', text[:80])
                notify(f'📢 TG @{ch}\n{text[:250]}\n{url}', cfg)
                stats['tg'] += 1
    return stats


def main():
    cfg = {}
    if os.path.exists(CFG):
        cfg = json.load(open(CFG, encoding='utf-8'))
    con = db_init()
    mode = sys.argv[1] if len(sys.argv) > 1 else 'once'
    if mode == 'once':
        print('stats:', run_once(con, cfg))
    else:
        while True:
            try:
                print('cycle:', run_once(con, cfg))
            except Exception as e:
                print('cycle error:', e)
            time.sleep(1800)  # каждые 30 минут


if __name__ == '__main__':
    main()
