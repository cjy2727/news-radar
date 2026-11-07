"""Single-process news dashboard. Python standard library only."""
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
LOCK = threading.Lock()
STOP = threading.Event()
PROVIDERS = ('google', 'naver', 'daum')


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db():
    con = sqlite3.connect(DATA / 'news.sqlite3', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS keywords(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS articles(id TEXT PRIMARY KEY, title TEXT, url TEXT, summary TEXT, publisher TEXT, published TEXT, collected TEXT);
        CREATE TABLE IF NOT EXISTS matches(article TEXT REFERENCES articles(id) ON DELETE CASCADE, keyword INTEGER REFERENCES keywords(id) ON DELETE CASCADE, provider TEXT, method TEXT, PRIMARY KEY(article,keyword,provider));
        CREATE TABLE IF NOT EXISTS runs(provider TEXT, keyword TEXT, method TEXT, status TEXT, count INTEGER, detail TEXT, finished TEXT, PRIMARY KEY(provider,keyword));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        if not con.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
            con.executemany('INSERT OR IGNORE INTO keywords(name) VALUES (?)', [('삼성전자',), ('하이닉스',)])
            con.execute("INSERT INTO settings VALUES ('initialized','1')")


def clean(value):
    return html.unescape(re.sub('<[^>]*>', '', value or '')).strip()


def date(value):
    try:
        dt = parsedate_to_datetime(value) if ',' in value else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        return ''


def safe_url(value):
    parsed = urllib.parse.urlsplit(value or '')
    return value if parsed.scheme in ('http', 'https') and parsed.hostname else ''


def request(url, headers=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'NewsRadar/1.0', **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError('응답 크기 제한 초과')
    return raw


def method(provider):
    if provider == 'naver':
        return '네이버 뉴스 · Google RSS 확대 검색 → 직접 검색'
    return 'Google 뉴스 RSS' if provider == 'google' else 'Google RSS 경유 · ' + provider + ' 도메인'


def fetch(provider, keyword):
    if provider not in PROVIDERS:
        raise ValueError('알 수 없는 검색 채널')
    query = '"' + keyword.replace('"', '') + '"'
    if provider == 'naver':
        query = keyword.replace('"', '')
    if provider != 'google':
        query += ' site:' + ('news.naver.com' if provider == 'naver' else 'v.daum.net')
    periods = (7, 30, None) if provider == 'naver' else (7,)
    for days in periods:
        if STOP.is_set():
            return []
        suffix = ' when:' + str(days) + 'd' if days else ''
        url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({'q': query + suffix, 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
        root = ET.fromstring(request(url))
        # An HTML consent/block page is a failure, not a valid empty search.
        if root.tag != 'rss' or root.find('channel') is None:
            raise ValueError('뉴스 RSS 형식이 아닌 응답')
        label = method(provider)
        if provider == 'naver':
            label = 'Google RSS 경유 · 네이버 뉴스 · ' + (str(days) + '일' if days else '기간 제한 없음')
        rows = [dict(title=clean(x.findtext('title')), url=x.findtext('link'), summary=clean(x.findtext('description')), publisher=x.findtext('source') or 'Google News', published=date(x.findtext('pubDate')), method=label) for x in root.findall('./channel/item')[:100]]
        rows = [row for row in rows if safe_url(row['url']) and keyword.casefold() in (row['title'] + ' ' + row['summary']).casefold()]
        if rows:
            return rows
    return fetch_naver_page(keyword) if provider == 'naver' else []


class NaverNewsParser(HTMLParser):
    """Read article anchors observed in the public news search HTML."""
    def __init__(self):
        super().__init__()
        self.anchor = None
        self.rows = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs = dict(attrs)
            kind = attrs.get('data-heatmap-target', '')
            if 'news_tit' in attrs.get('class', '').split():
                kind = '.tit'
            self.anchor = [attrs.get('href', ''), kind, []]

    def handle_data(self, value):
        if self.anchor:
            self.anchor[2].append(value)

    def handle_endtag(self, tag):
        if tag != 'a' or self.anchor is None:
            return
        url, kind, parts = self.anchor
        self.anchor = None
        if kind not in ('.tit', '.body') or not safe_url(url):
            return
        text = re.sub(r'\s*새\s*창\s*열기\s*$', '', ''.join(parts)).strip()
        row = self.rows.setdefault(url, dict(title='', url=url, summary='', publisher=urllib.parse.urlsplit(url).hostname, published='', method='네이버 뉴스 검색 페이지 직접 수집'))
        row['title' if kind == '.tit' else 'summary'] = text


def fetch_naver_page(keyword):
    if STOP.is_set():
        return []
    url = 'https://search.naver.com/search.naver?' + urllib.parse.urlencode({'where': 'news', 'query': keyword, 'sort': 1})
    body = request(url).decode('utf-8')
    parser = NaverNewsParser()
    parser.feed(body)
    articles = [row for row in parser.rows.values() if row['title']]
    if not articles:
        if '검색결과가 없습니다' in clean(body) or '검색 결과가 없습니다' in clean(body):
            return []
        raise ValueError('네이버 검색 페이지 구조 변경 또는 접근 제한')
    return [row for row in articles if keyword.casefold() in (row['title'] + ' ' + row['summary']).casefold()][:100]


def collect_locked():
    try:
        with db() as con:
            keywords = con.execute('SELECT * FROM keywords').fetchall()
        for kw in keywords:
            for provider in PROVIDERS:
                if STOP.is_set():
                    return
                mode, count, status, detail = method(provider), 0, 'ok', ''
                try:
                    rows = fetch(provider, kw['name'])
                    if rows:
                        mode = rows[0].get('method', mode)
                    else:
                        detail = 'Google RSS에서 일치하는 결과가 없습니다. 해당 포털에 기사가 없다는 뜻은 아닙니다.'
                    if provider == 'naver' and not rows:
                        detail = 'Google 7일·30일·기간 제한 없음 및 네이버 직접 검색에서 일치 결과가 없습니다.'
                    with db() as con:
                        if not con.execute('SELECT 1 FROM keywords WHERE id=? AND name=?', (kw['id'], kw['name'])).fetchone():
                            continue
                        for row in rows:
                            if not safe_url(row['url']) or kw['name'].casefold() not in (row['title'] + ' ' + row['summary']).casefold():
                                continue
                            # Publisher + normalized title joins the same story across search providers.
                            title_key = re.sub(r'\s+', '', row['title']).casefold()
                            key = hashlib.sha256((title_key + '|' + (row['publisher'] or '')).encode()).hexdigest()
                            con.execute('INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?)', (key, row['title'], row['url'], row['summary'], row['publisher'], row['published'], now()))
                            con.execute('INSERT OR REPLACE INTO matches VALUES (?,?,?,?)', (key, kw['id'], provider, row.get('method', mode)))
                            count += 1
                except urllib.error.HTTPError as exc:
                    status, detail = 'error', '검색 서버 HTTP ' + str(exc.code) + ' · 접근 제한 또는 일시적인 장애일 수 있습니다.'
                except Exception as exc:
                    status, detail = 'error', '수집 실패 (' + type(exc).__name__ + ') · 다음 수집에서 재시도합니다.'
                with db() as con:
                    con.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)', (provider, kw['name'], mode, status, count, detail, now()))
        with db() as con:
            con.execute("DELETE FROM articles WHERE collected < ?", (datetime.fromtimestamp(time.time() - 90 * 86400, timezone.utc).isoformat(),))
    finally:
        LOCK.release()


def start_collection():
    if not LOCK.acquire(blocking=False):
        return False
    threading.Thread(target=collect_locked, daemon=True).start()
    return True


def schedule():
    while not STOP.is_set():
        start_collection()
        STOP.wait(max(5, int(os.getenv('COLLECT_INTERVAL_MINUTES', '30'))) * 60)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Windowed Windows executables do not have sys.stderr.
        logging.info('%s %s', self.address_string(), fmt % args)

    def respond(self, data, status=200, mime='application/json; charset=utf-8'):
        raw = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/health':
            return self.respond({'ok': True, 'application': 'news-radar'})
        if path == '/api/state':
            with db() as con:
                keywords = [dict(r) for r in con.execute('SELECT * FROM keywords ORDER BY id')]
                articles = [dict(r) for r in con.execute('SELECT * FROM articles WHERE EXISTS (SELECT 1 FROM matches WHERE article=articles.id) ORDER BY COALESCE(NULLIF(published,\'\'),collected) DESC LIMIT 1000')]
                matches = {}
                for r in con.execute('SELECT m.*, k.name FROM matches m JOIN keywords k ON k.id=m.keyword'):
                    matches.setdefault(r['article'], []).append(dict(r))
                for row in articles:
                    row['matches'] = matches.get(row['id'], [])
                runs = [dict(r) for r in con.execute('SELECT * FROM runs WHERE keyword IN (SELECT name FROM keywords) ORDER BY provider,keyword')]
            return self.respond({'keywords': keywords, 'articles': articles, 'runs': runs, 'collecting': LOCK.locked(), 'providers': {p: method(p) for p in PROVIDERS}, 'interval': max(5, int(os.getenv('COLLECT_INTERVAL_MINUTES', '30')))})
        files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
        if path in files:
            name, mime = files[path]
            return self.respond((ROOT / 'static' / name).read_bytes(), mime=mime + '; charset=utf-8')
        self.respond({'error': '찾을 수 없습니다.'}, 404)

    def do_POST(self):
        # Require JSON + a custom header; cross-origin forms cannot mutate settings.
        if self.headers.get('X-News-Radar') != '1' or self.headers.get('Content-Type') != 'application/json':
            return self.respond({'error': '허용되지 않는 요청입니다.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            if self.path == '/api/collect':
                return self.respond({'started': start_collection()}, 202)
            if self.path == '/api/keywords':
                name = body.get('name', '')
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                    raise ValueError()
                with db() as con:
                    if con.execute('SELECT count(*) FROM keywords').fetchone()[0] >= 20:
                        return self.respond({'error': '키워드는 최대 20개입니다.'}, 400)
                    con.execute('INSERT INTO keywords(name) VALUES (?)', (name.strip(),))
                return self.respond({'ok': True}, 201)
            if self.path == '/api/keywords/delete':
                with db() as con:
                    con.execute('DELETE FROM keywords WHERE id=?', (int(body['id']),))
                return self.respond({'ok': True})
            self.respond({'error': '찾을 수 없습니다.'}, 404)
        except sqlite3.IntegrityError:
            self.respond({'error': '이미 등록된 키워드입니다.'}, 409)
        except (ValueError, KeyError, TypeError):
            self.respond({'error': '입력값을 확인하세요.'}, 400)

