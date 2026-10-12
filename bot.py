#!/usr/bin/env python3
"""
Bot de Telegram para buscar en Gelbooru - v5
Config por usuario + filtros + score + autopost beta (pause/horario/álbum/stats) + JSON.
"""

import os
import sys
import time
import json
import uuid
import random
import logging
import threading
from datetime import datetime
from urllib.parse import urljoin

from flask import Flask, request
import requests

# ==================== CONFIG ====================
SITE_BASE_URL = "https://gelbooru.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "").strip()
 
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== PERSISTENCIA ====================
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "/tmp"
DATA_FILE = os.path.join(DATA_DIR, "bot_data.json")
_save_lock = threading.Lock()

# ==================== ESTADO ====================
user_configs = {}
sessions = {}
processed_updates = set()
autoposts = {}
autopost_lock = threading.Lock()
_worker_started = False

ALL_FIELDS = ['id', 'uploader', 'date', 'size', 'source', 'score', 'rating', 'tags']
DEFAULT_FIELDS = {'id', 'date', 'size', 'score', 'rating', 'tags'}
ALL_MEDIA = ['image', 'gif', 'video']
DEFAULT_MEDIA = set(ALL_MEDIA)
VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.avi', '.mkv')

CONTENT_FILTERS = {
    'ai':       {'label': '🤖 Filtro AI',                 'tags': ['ai_generated', 'ai-assisted', 'ai_assisted']},
    'gay':      {'label': '🏳️‍🌈 Filtro etiquetas Gay',     'tags': ['yaoi', 'male/male']},
    'lesbian':  {'label': '👩‍❤️‍👩 Filtro etiquetas Lesbianas', 'tags': ['yuri', 'female/female']},
    'furry':    {'label': '🐾 Filtro Furry',              'tags': ['furry', 'anthro']},
    'scat':     {'label': '💩 Filtro Scat',               'tags': ['scat']},
    'futanari': {'label': '⚧️ Filtro Futanari',           'tags': ['futanari', 'futa']},
    'gore':     {'label': '🩸 Filtro Gore',               'tags': ['gore', 'blood', 'guro']},
}

SCORE_PRESETS = [0, 25, 50, 100, 200, 500]
PHOTO_PRESETS = [1, 2, 3, 5, 10]
HOUR_PRESETS = [0, 6, 8, 10, 12, 18, 22, 24]

# ==================== JSON LOAD/SAVE ====================
def _serialize_config(cfg):
    return {
        'fields': list(cfg.get('fields', [])),
        'rating': cfg.get('rating', ''),
        'media': list(cfg.get('media', [])),
        'min_score': cfg.get('min_score', 0),
        'content_filters': list(cfg.get('content_filters', [])),
    }

def _deserialize_config(d):
    return {
        'fields': set(d.get('fields', DEFAULT_FIELDS)),
        'rating': d.get('rating', ''),
        'media': set(d.get('media', DEFAULT_MEDIA)),
        'min_score': int(d.get('min_score', 0)),
        'content_filters': set(d.get('content_filters', [])),
    }

def save_data():
    try:
        with _save_lock:
            with autopost_lock:
                ap = {k: dict(v) for k, v in autoposts.items()}
            payload = {
                'user_configs': {str(k): _serialize_config(v) for k, v in user_configs.items()},
                'autoposts': ap,
                'saved_at': datetime.utcnow().isoformat(),
            }
            tmp = DATA_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"❌ save_data: {e}")

def load_data():
    global user_configs, autoposts
    try:
        if not os.path.exists(DATA_FILE):
            logger.info("📂 No hay JSON, arrancando limpio")
            return
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        user_configs = {int(k): _deserialize_config(v)
                        for k, v in payload.get('user_configs', {}).items()}
        autoposts = payload.get('autoposts', {})
        for t in autoposts.values():
            ensure_task_defaults(t)
        logger.info(f"✅ JSON: {len(user_configs)} configs, {len(autoposts)} autoposts")
    except Exception as e:
        logger.error(f"❌ load_data: {e}")

def save_async():
    threading.Thread(target=save_data, daemon=True).start()

# ==================== GELBOORU API ====================
class GelbooruAPI:
    def __init__(self):
        self.api_key = os.environ.get("GELBOORU_API_KEY")
        self.user_id = os.environ.get("GELBOORU_USER_ID")
        self.base_url = "https://gelbooru.com/index.php"

    def _proxies(self):
        return {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None

    def _build_params(self, tags, page=1, limit=100):
        params = {"page": "dapi", "s": "post", "q": "index", "json": 1,
                  "tags": tags, "pid": (page - 1) * limit, "limit": limit}
        if self.api_key and self.user_id:
            params["api_key"] = self.api_key
            params["user_id"] = self.user_id
        return params

    def fetch_images(self, tags, page=1, retry=0):
        try:
            r = requests.get(self.base_url, headers={'User-Agent': USER_AGENT},
                             params=self._build_params(tags, page),
                             timeout=REQUEST_TIMEOUT, proxies=self._proxies())
            if r.status_code == 429 and retry < MAX_RETRIES:
                return self.fetch_images(tags, page, retry + 1)
            r.raise_for_status()
            posts = self._extract_posts(r.json())
            return self._extract_images(posts) if posts else []
        except Exception as e:
            logger.error(f"❌ fetch_images: {e}")
            return []

    def _extract_posts(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if 'post' in data: return data['post']
            if 'posts' in data: return data['posts']
            if 'error' in data: logger.error(f"API: {data['error']}")
        return []

    def _extract_images(self, posts):
        out = []
        for p in posts:
            img = {'file_url': p.get('file_url'), 'sample_url': p.get('sample_url'),
                   'tags': p.get('tags', ''), 'id': p.get('id'),
                   'score': p.get('score', 0), 'rating': p.get('rating', ''),
                   'width': p.get('width', 0), 'height': p.get('height', 0),
                   'uploader': p.get('uploader') or '', 'source': p.get('source') or '',
                   'created_at': p.get('created_at') or ''}
            for k in ['file_url', 'sample_url']:
                if img[k] and not img[k].startswith('http'):
                    img[k] = urljoin(SITE_BASE_URL, img[k])
            if not img['file_url'] and img['sample_url']:
                img['file_url'] = img['sample_url']
            if img['file_url']:
                out.append(img)
        return out

gelbooru = GelbooruAPI()

# ==================== HELPERS ====================
def get_media_type(url):
    if not url: return 'image'
    u = url.lower().split('?')[0]
    if u.endswith('.gif'): return 'gif'
    if u.endswith(VIDEO_EXTS): return 'video'
    return 'image'

def is_admin(user_id):
    return str(user_id) == ADMIN_USER_ID

def get_user_config(uid):
    if uid not in user_configs:
        user_configs[uid] = {'fields': set(DEFAULT_FIELDS), 'rating': '',
                             'media': set(DEFAULT_MEDIA), 'min_score': 0,
                             'content_filters': set()}
    c = user_configs[uid]
    c.setdefault('min_score', 0)
    c.setdefault('content_filters', set())
    return c

def format_date(s):
    if not s: return ''
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

def format_ago(ts):
    if not ts: return 'nunca'
    d = int(time.time() - ts)
    if d < 60: return f'hace {d}s'
    if d < 3600: return f'hace {d//60} min'
    if d < 86400: return f'hace {d//3600} h'
    return f'hace {d//86400} d'

def today_str():
    return datetime.utcnow().strftime('%Y-%m-%d')

def format_caption(img, uid):
    f = get_user_config(uid)['fields']
    if not f: return ""
    lines = []
    if 'id' in f and img.get('id'): lines.append(f"🖼️ *ID:* `{img['id']}`")
    if 'uploader' in f: lines.append(f"📤 *Uploader:* {img.get('uploader') or 'Anónimo'}")
    if 'date' in f and img.get('created_at'): lines.append(f"📅 *Posted:* {format_date(img['created_at'])}")
    if 'size' in f and img.get('width'): lines.append(f"📐 *Tamaño:* {img['width']}x{img['height']}")
    if 'source' in f and img.get('source'): lines.append(f"🔗 *Source:* {img['source']}")
    sr = []
    if 'score' in f: sr.append(f"⭐ {img.get('score', 0)}")
    if 'rating' in f: sr.append(f"📊 `{(img.get('rating') or '').upper()}`")
    if sr: lines.append("  |  ".join(sr))
    if 'tags' in f and img.get('tags'):
        t = img['tags']
        if len(t) > 900: t = t[:897] + '...'
        lines.append(f"🏷️ `{t}`")
    return "\n".join(lines)

# ==================== MENÚS CONFIG USUARIO ====================
def main_menu_text(uid):
    c = get_user_config(uid)
    rd = {'': '🌐 Todos', 'safe': '✅ Safe', 'general': '🟢 General',
          'questionable': '🟡 Questionable', 'explicit': '🔴 Explicit'}.get(c['rating'], c['rating'])
    mi = ' '.join(v for k, v in {'image':'📷','gif':'🎞️','video':'🎬'}.items() if k in c['media']) or '❌'
    st = f"⭐ ≥{c['min_score']}" if c['min_score'] > 0 else "🌐 Cualquiera"
    ft = f"🚫 {len(c['content_filters'])} activos" if c['content_filters'] else "🌐 Ninguno"
    return ("⚙️ *Configuración*\n\n"
            "Personaliza tus búsquedas. Los cambios son solo para ti.\n\n"
            f"📋 *Info:* {len(c['fields'])}/8 campos\n"
            f"🎚️ *Rating:* {rd}\n"
            f"🎬 *Tipo:* {mi}\n"
            f"⭐ *Score mín:* {st}\n"
            f"🚫 *Filtros:* {ft}\n")

def main_menu_keyboard(uid):
    c = get_user_config(uid)
    rows = [
        [{'text': '📋 Información de imagen', 'callback_data': 'cfg_info'}],
        [{'text': '🎬 Tipo de contenido', 'callback_data': 'cfg_media'}],
        [{'text': '🎚️ Filtrar por rating', 'callback_data': 'cfg_rating'}],
        [{'text': '⭐ Score mínimo', 'callback_data': 'cfg_score'}],
    ]
    if c['rating'] == 'explicit':
        rows.append([{'text': '🚫 Filtros de contenido', 'callback_data': 'cfg_filters'}])
    return {'inline_keyboard': rows}

def info_menu_text(uid):
    c = get_user_config(uid)
    return ("📋 *Información de imagen*\n\n"
            "Activa o desactiva qué datos aparecen en el mensaje.\n\n"
            f"📊 *Activos:* {len(c['fields'])}/8")

def info_menu_keyboard(uid):
    f = get_user_config(uid)['fields']
    def b(l, k): return {'text': ('✅ ' if k in f else '❌ ') + l, 'callback_data': f'cfg_f_{k}'}
    return {'inline_keyboard': [
        [b('🖼️ ID', 'id'), b('📤 Uploader', 'uploader')],
        [b('📅 Fecha', 'date'), b('📐 Tamaño', 'size')],
        [b('🔗 Source', 'source'), b('⭐ Score', 'score')],
        [b('📊 Rating', 'rating'), b('🏷️ Tags', 'tags')],
        [{'text': '🔕 Sin info (solo media)', 'callback_data': 'cfg_f_none'}],
        [{'text': '✅ Activar todo', 'callback_data': 'cfg_f_all'}],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def media_menu_text(uid):
    return ("🎬 *Tipo de contenido*\n\n"
            "Elige qué tipo de archivos quieres recibir.\n"
            "Puedes activar varios a la vez.\n\n"
            f"📊 *Activos:* {len(get_user_config(uid)['media'])}/3")

def media_menu_keyboard(uid):
    m = get_user_config(uid)['media']
    def b(l, k): return {'text': ('✅ ' if k in m else '❌ ') + l, 'callback_data': f'cfg_m_{k}'}
    return {'inline_keyboard': [
        [b('📷 Imágenes', 'image')],
        [b('🎞️ GIFs', 'gif')],
        [b('🎬 Vídeos', 'video')],
        [{'text': '✅ Todos', 'callback_data': 'cfg_m_all'},
         {'text': '🔕 Ninguno', 'callback_data': 'cfg_m_none'}],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def rating_menu_text(uid):
    c = get_user_config(uid)
    rd = {'': '🌐 Todos', 'safe': '✅ Safe', 'general': '🟢 General',
          'questionable': '🟡 Questionable', 'explicit': '🔴 Explicit'}.get(c['rating'], c['rating'])
    note = ""
    if c['rating'] == 'explicit':
        note = "\n\n💡 Con explicit aparece `🚫 Filtros de contenido` para excluir cosas."
    return ("🎚️ *Filtrar por rating*\n\n"
            "Filtra las imágenes según su clasificación.\n\n"
            f"*Actual:* {rd}\n\n"
            "ℹ️ Safe → seguro\nℹ️ General → público\nℹ️ Questionable → sugerente\nℹ️ Explicit → adulto" + note)

def rating_menu_keyboard(uid):
    r = get_user_config(uid)['rating']
    def b(l, v): return {'text': ('✅ ' if r == v else '') + l, 'callback_data': f'cfg_r_{v or "all"}'}
    return {'inline_keyboard': [
        [b('🌐 Todos', '')], [b('✅ Safe', 'safe')], [b('🟢 General', 'general')],
        [b('🟡 Questionable', 'questionable')], [b('🔴 Explicit', 'explicit')],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def score_menu_text(uid):
    s = get_user_config(uid)['min_score']
    return ("⭐ *Score mínimo*\n\n"
            "Solo se mostrarán imágenes con este score o superior.\n\n"
            f"*Actual:* {'🌐 Cualquiera' if s == 0 else f'⭐ ≥{s}'}")

def score_menu_keyboard(uid):
    s = get_user_config(uid)['min_score']
    rows, row = [], []
    for v in SCORE_PRESETS:
        label = '🌐 Cualquiera' if v == 0 else f'⭐ ≥{v}'
        if s == v: label = '✅ ' + label
        row.append({'text': label, 'callback_data': f'cfg_s_{v}'})
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}])
    return {'inline_keyboard': rows}

def filters_menu_text(uid):
    n = len(get_user_config(uid)['content_filters'])
    return ("🚫 *Filtros de contenido*\n\n"
            "Activa los filtros para *excluir* esas categorías de tus resultados.\n\n"
            f"📊 *Activos:* {n}/{len(CONTENT_FILTERS)}")

def filters_menu_keyboard(uid):
    a = get_user_config(uid)['content_filters']
    rows = []
    for k, info in CONTENT_FILTERS.items():
        rows.append([{'text': ('✅ ' if k in a else '❌ ') + info['label'],
                      'callback_data': f'cfg_cf_{k}'}])
    rows.append([{'text': '✅ Activar todos', 'callback_data': 'cfg_cf_all'},
                 {'text': '🔕 Quitar todos', 'callback_data': 'cfg_cf_none'}])
    rows.append([{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}])
    return {'inline_keyboard': rows}

# ==================== TELEGRAM ====================
def send_message(chat_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'text': text,
                'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if keyboard: data['reply_markup'] = json.dumps(keyboard)
        r = requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
        return r.json().get('result', {}).get('message_id') if r.ok else None
    except Exception as e:
        logger.error(f"send_message: {e}")
        return None

def edit_message(chat_id, msg_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'message_id': msg_id,
                'text': text, 'parse_mode': 'Markdown'}
        if keyboard: data['reply_markup'] = json.dumps(keyboard)
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=10)
    except Exception as e:
        logger.error(f"edit_message: {e}")

def answer_callback(cb_id, text=None):
    try:
        data = {'callback_query_id': cb_id}
        if text: data['text'] = text
        requests.post(f"{API_URL}/answerCallbackQuery", json=data, timeout=5)
    except Exception as e:
        logger.error(f"answer_callback: {e}")

def send_media(chat_id, url, caption="", keyboard=None):
    mt = get_media_type(url)
    try:
        headers = {'User-Agent': USER_AGENT,
                   'Accept': 'image/webp,image/apng,image/*,video/*,*/*;q=0.8',
                   'Referer': 'https://gelbooru.com/'}
        proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None
        r = requests.get(url, headers=headers, timeout=90, proxies=proxies)
        if r.status_code != 200: return False
        ct = r.headers.get('content-type', '').lower()
        if mt == 'video':
            ep, fld, fn = 'sendVideo', 'video', 'video.mp4'
            if 'video' not in ct: ct = 'video/mp4'
        elif mt == 'gif':
            ep, fld, fn = 'sendAnimation', 'animation', 'anim.gif'
            if 'gif' not in ct: ct = 'image/gif'
        else:
            ep, fld, fn = 'sendPhoto', 'photo', 'image.jpg'
            if 'image' not in ct: ct = 'image/jpeg'
        files = {fld: (fn, r.content, ct)}
        data = {'chat_id': chat_id}
        if caption:
            data['caption'] = caption
            data['parse_mode'] = 'Markdown'
        if keyboard: data['reply_markup'] = json.dumps(keyboard)
        rr = requests.post(f"{API_URL}/{ep}", files=files, data=data, timeout=90)
        if rr.ok: return True
        files = {'document': (fn, r.content, ct or 'application/octet-stream')}
        r2 = requests.post(f"{API_URL}/sendDocument", files=files, data=data, timeout=90)
        return r2.ok
    except Exception as e:
        logger.error(f"send_media: {e}")
        return False

# ==================== BÚSQUEDA USUARIO ====================
def build_query(uid, query):
    c = get_user_config(uid)
    parts = [query]
    if c['rating']: parts.append(f"rating:{c['rating']}")
    if c.get('min_score', 0) > 0: parts.append(f"score:>={c['min_score']}")
    for k in c.get('content_filters', set()):
        for t in CONTENT_FILTERS.get(k, {}).get('tags', []):
            parts.append(f"-{t}")
    return ' '.join(p for p in parts if p).strip()

def fetch_next_image(s):
    images, idx, page = s['images'], s['current_index'], s['page']
    sent, allowed, ms = s['sent_ids'], s['media'], s.get('min_score', 0)
    def ok(i):
        if get_media_type(i['file_url']) not in allowed: return False
        if ms and (i.get('score') or 0) < ms: return False
        return True
    if not images or idx >= len(images):
        attempts, new_images = 0, []
        while attempts < 8:
            raw = gelbooru.fetch_images(s['full_query'], page=page)
            new_images = [i for i in raw if i.get('id') not in sent and ok(i)]
            if new_images: break
            attempts += 1; page += 1
        if not new_images: return None
        s['images'] = new_images; s['current_index'] = 0; s['page'] = page
        images, idx = new_images, 0
    img = images[idx]
    sent.add(img.get('id'))
    s['current_index'] = idx + 1
    return img

def send_next(chat_id, uid, sid, s, reply_to=None):
    img = fetch_next_image(s)
    if not img:
        send_message(chat_id, f"❌ No hay más resultados para `{s['query']}`")
        return False
    cap = format_caption(img, uid)
    kb = {'inline_keyboard': [[{'text': '🔄 Más', 'callback_data': f'more:{sid}'}]]}
    ok = send_media(chat_id, img['file_url'], caption=cap, keyboard=kb)
    if ok and reply_to:
        try: requests.post(f"{API_URL}/deleteMessage",
                           json={'chat_id': chat_id, 'message_id': reply_to}, timeout=5)
        except Exception: pass
    return ok

def handle_search(chat_id, uid, query):
    c = get_user_config(uid)
    sid = uuid.uuid4().hex[:12]
    sessions[sid] = {'query': query, 'full_query': build_query(uid, query),
                     'page': 1, 'images': [], 'current_index': 0,
                     'sent_ids': set(), 'media': set(c['media']),
                     'min_score': c.get('min_score', 0)}
    st = send_message(chat_id, f"🔎 Buscando `{query}`...")
    send_next(chat_id, uid, sid, sessions[sid], reply_to=st)

# ==================== AUTO-POST (BETA) ====================
def ensure_task_defaults(t):
    t.setdefault('paused', False)
    t.setdefault('active_hours', {'start': 0, 'end': 24})
    t.setdefault('photos_per_post', 1)
    t.setdefault('stats', {
        'total_sent': 0, 'total_failed': 0,
        'today_sent': 0, 'today_failed': 0,
        'today_date': '', 'last_post_ts': 0, 'last_success_ts': 0,
    })
    return t

def bump_today(stats, key):
    tk = today_str()
    if stats.get('today_date') != tk:
        stats['today_date'] = tk
        stats['today_sent'] = 0
        stats['today_failed'] = 0
    stats[key] += 1

def in_active_hours(t):
    h = t.get('active_hours', {'start': 0, 'end': 24})
    s, e = h.get('start', 0), h.get('end', 24)
    if s == 0 and e == 24: return True
    if s == e: return True
    now = datetime.utcnow().hour
    if s < e: return s <= now < e
    return now >= s or now < e  # cruza medianoche

def fetch_post_images(t, n):
    parts = [t['tags']]
    if t.get('min_score'): parts.append(f"score:>={t['min_score']}")
    for x in t.get('exclude', []): parts.append(f"-{x}")
    q = ' '.join(parts).strip()
    page = random.randint(1, 20)
    imgs = gelbooru.fetch_images(q, page=page)
    if not imgs: return []
    random.shuffle(imgs)
    return imgs[:n]

def send_media_album(chat_id, images, caption=""):
    """Envía varias imágenes. Intenta álbum si son todas fotos ≤10."""
    try:
        headers = {'User-Agent': USER_AGENT, 'Accept': 'image/*,*/*;q=0.8',
                   'Referer': 'https://gelbooru.com/'}
        proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None
        dl = []
        for img in images:
            try:
                r = requests.get(img['file_url'], headers=headers, timeout=90, proxies=proxies)
                if r.status_code != 200: continue
                ct = r.headers.get('content-type', 'image/jpeg').lower()
                ext = 'jpg'
                if 'png' in ct: ext = 'png'
                elif 'gif' in ct: ext = 'gif'
                elif 'webp' in ct: ext = 'webp'
                dl.append({'content': r.content, 'ct': ct, 'ext': ext,
                           'media_type': get_media_type(img['file_url'])})
            except Exception as e:
                logger.error(f"download album: {e}")
        if not dl: return 0

        all_photos = all(d['media_type'] == 'image' for d in dl)
        if all_photos and 2 <= len(dl) <= 10:
            media, files = [], {}
            for i, d in enumerate(dl):
                key = f"file{i}"
                item = {'type': 'photo', 'media': f'attach://{key}'}
                if i == 0 and caption:
                    item['caption'] = caption
                    item['parse_mode'] = 'Markdown'
                media.append(item)
                files[key] = (f'image{i}.{d["ext"]}', d['content'], d['ct'])
            r = requests.post(f"{API_URL}/sendMediaGroup",
                              data={'chat_id': chat_id, 'media': json.dumps(media)},
                              files=files, timeout=120)
            if r.ok: return len(dl)
            logger.warning(f"album falló: {r.text[:200]}")

        # Fallback individual
        sent = 0
        for i, d in enumerate(dl):
            try:
                cap = caption if i == 0 else ""
                if d['media_type'] == 'video':
                    ep, fld, fn = 'sendVideo', 'video', f'v{i}.mp4'
                elif d['media_type'] == 'gif':
                    ep, fld, fn = 'sendAnimation', 'animation', f'g{i}.gif'
                else:
                    ep, fld, fn = 'sendPhoto', 'photo', f'p{i}.{d["ext"]}'
                files = {fld: (fn, d['content'], d['ct'])}
                data = {'chat_id': chat_id}
                if cap:
                    data['caption'] = cap
                    data['parse_mode'] = 'Markdown'
                r = requests.post(f"{API_URL}/{ep}", files=files, data=data, timeout=90)
                if r.ok: sent += 1
            except Exception as e:
                logger.error(f"send single: {e}")
        return sent
    except Exception as e:
        logger.error(f"send_media_album: {e}")
        return 0

def post_to_channel(t):
    try:
        n = t.get('photos_per_post', 1)
        imgs = fetch_post_images(t, n)
        if not imgs: return False
        cap = ""
        if imgs[0].get('tags'):
            cap = f"🏷️ `{imgs[0]['tags'][:900]}`"
        sent = send_media_album(t['chat_id'], imgs, caption=cap)
        ok = sent > 0
        logger.info(f"{'✅' if ok else '⚠️'} Autopost {t['chat_id']}: {sent}/{len(imgs)}")
        return ok
    except Exception as e:
        logger.error(f"❌ post_to_channel: {e}")
        return False

def autopost_worker():
    logger.info("🚀 Auto-post worker iniciado")
    while True:
        try:
            now = time.time()
            with autopost_lock:
                tasks = list(autoposts.items())
            for pid, t in tasks:
                ensure_task_defaults(t)
                if t.get('paused'): continue
                if not in_active_hours(t): continue
                if now - t.get('last_post', 0) < t.get('next_delay', 0): continue
                ok = post_to_channel(t)
                with autopost_lock:
                    st = t['stats']
                    bump_today(st, 'today_sent' if ok else 'today_failed')
                    if ok:
                        st['total_sent'] += 1
                        st['last_success_ts'] = now
                    else:
                        st['total_failed'] += 1
                    st['last_post_ts'] = now
                    t['last_post'] = now
                    t['next_delay'] = random.randint(t['interval_min'] * 60,
                                                     t['interval_max'] * 60)
                save_async()
                logger.info(f"⏱️ Próximo {t['chat_id']} en {t['next_delay']//60} min")
            time.sleep(30)
        except Exception as e:
            logger.error(f"❌ worker loop: {e}")
            time.sleep(60)

def start_worker_once():
    global _worker_started
    if not _worker_started:
        _worker_started = True
        threading.Thread(target=autopost_worker, daemon=True).start()
        logger.info("✅ Hilo auto-post lanzado")

# ==================== MENÚS AUTO-POST ====================
def autopost_menu_text():
    if not autoposts:
        return ("🤖 *Auto-post (BETA)*\n\nNo hay tareas configuradas.\n\n"
                "Usa `/autopost add` para crear una.")
    text = "🤖 *Auto-post (BETA)*\n\n"
    for pid, t in autoposts.items():
        ensure_task_defaults(t)
        icon = '⏸️' if t['paused'] else '▶️'
        ex = ' '.join(f"`-{x}`" for x in t.get('exclude', [])) or '—'
        nxt = max(0, int((t['last_post'] + t['next_delay'] - time.time()) / 60))
        h = t['active_hours']
        hours = '24h' if h['start'] == 0 and h['end'] == 24 else f"{h['start']:02d}-{h['end']:02d}h UTC"
        s = t['stats']
        text += (f"{icon} 🆔 `{pid}`\n"
                 f"  📢 `{t['chat_id']}`  |  🏷️ `{t['tags']}`\n"
                 f"  ⭐ ≥{t.get('min_score', 0)}  |  🖼️ x{t['photos_per_post']}\n"
                 f"  🚫 {ex}\n"
                 f"  ⏱️ {t['interval_min']}-{t['interval_max']} min  |  🕐 {hours}\n"
                 f"  📊 {s['total_sent']}✅ {s['total_failed']}❌ | hoy {s['today_sent']}✅ {s['today_failed']}❌\n"
                 f"  🕒 Último: {format_ago(s['last_post_ts'])}  |  ⏳ Próximo: ~{nxt} min\n\n")
    return text

def autopost_menu_keyboard():
    rows = [[{'text': '➕ Nueva tarea', 'callback_data': 'ap_new'}]]
    for pid, t in autoposts.items():
        ensure_task_defaults(t)
        icon = '⏸️' if t['paused'] else '▶️'
        rows.append([{'text': f'{icon} {pid}', 'callback_data': f'ap_menu_{pid}'}])
    rows.append([{'text': '🔄 Refrescar', 'callback_data': 'ap_refresh'}])
    return {'inline_keyboard': rows}

def task_menu_text(pid):
    t = autoposts.get(pid)
    if not t: return "❌ Tarea no encontrada"
    ensure_task_defaults(t)
    status = '⏸️ Pausado' if t['paused'] else '▶️ Activo'
    ex = ' '.join(f"`-{x}`" for x in t.get('exclude', [])) or '—'
    h = t['active_hours']
    hours = '24h (siempre)' if h['start'] == 0 and h['end'] == 24 else f"{h['start']:02d}:00 - {h['end']:02d}:00 UTC"
    nxt = max(0, int((t['last_post'] + t['next_delay'] - time.time()) / 60))
    s = t['stats']
    return (f"⚙️ *Tarea* `{pid}`\n\n"
            f"📊 *Estado:* {status}\n"
            f"📢 *Chat:* `{t['chat_id']}`\n"
            f"🏷️ *Tags:* `{t['tags']}`\n"
            f"⭐ *Score mín:* {t.get('min_score', 0)}\n"
            f"🚫 *Excluir:* {ex}\n"
            f"⏱️ *Intervalo:* {t['interval_min']}-{t['interval_max']} min\n"
            f"🖼️ *Fotos/post:* {t['photos_per_post']}\n"
            f"🕐 *Horario:* {hours}\n"
            f"⏳ *Próximo:* ~{nxt} min\n\n"
            f"📊 *Estadísticas*\n"
            f"  • Total: {s['total_sent']} ✅ / {s['total_failed']} ❌\n"
            f"  • Hoy: {s['today_sent']} ✅ / {s['today_failed']} ❌\n"
            f"  • Último post: {format_ago(s['last_post_ts'])}\n"
            f"  • Último OK: {format_ago(s['last_success_ts'])}\n")

def task_menu_keyboard(pid):
    t = autoposts.get(pid)
    if not t:
        return {'inline_keyboard': [[{'text': '⬅️ Volver', 'callback_data': 'ap_refresh'}]]}
    ensure_task_defaults(t)
    pause_btn = '▶️ Reanudar' if t['paused'] else '⏸️ Pausar'
    return {'inline_keyboard': [
        [{'text': pause_btn, 'callback_data': f'ap_toggle_{pid}'},
         {'text': '🧪 Test', 'callback_data': f'ap_test_{pid}'}],
        [{'text': '🖼️ Fotos/post', 'callback_data': f'ap_photos_{pid}'}],
        [{'text': '🕐 Horario activo', 'callback_data': f'ap_sched_{pid}'}],
        [{'text': '🗑️ Eliminar', 'callback_data': f'ap_del2_{pid}'}],
        [{'text': '⬅️ Volver', 'callback_data': 'ap_refresh'}],
    ]}

def photos_menu_text(pid):
    t = autoposts.get(pid)
    if not t: return "❌"
    ensure_task_defaults(t)
    return (f"🖼️ *Fotos por post* — `{pid}`\n\n"
            f"Actual: *{t['photos_per_post']}*\n\n"
            "Se envían como álbum si son todas fotos (≤10).\n"
            "Si hay mezcla o error, se envían una por una.")

def photos_menu_keyboard(pid):
    t = autoposts.get(pid)
    if not t: return {'inline_keyboard': []}
    ensure_task_defaults(t)
    cur = t['photos_per_post']
    rows, row = [], []
    for n in PHOTO_PRESETS:
        label = ('✅ ' if cur == n else '') + str(n)
        row.append({'text': label, 'callback_data': f'ap_setph_{pid}_{n}'})
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{'text': '⬅️ Volver', 'callback_data': f'ap_menu_{pid}'}])
    return {'inline_keyboard': rows}

def sched_menu_text(pid):
    t = autoposts.get(pid)
    if not t: return "❌"
    ensure_task_defaults(t)
    h = t['active_hours']
    txt = '24h (siempre)' if h['start'] == 0 and h['end'] == 24 else f"{h['start']:02d}:00 → {h['end']:02d}:00 UTC"
    now_h = datetime.utcnow().hour
    return (f"🕐 *Horario activo* — `{pid}`\n\n"
            f"Actual: *{txt}*\n"
            f"Hora UTC actual: *{now_h:02d}:00*\n\n"
            "⚠️ Los horarios son en UTC.\n"
            "Si inicio = fin = 0 → publica 24h.")

def sched_menu_keyboard(pid):
    return {'inline_keyboard': [
        [{'text': '🟢 Inicio', 'callback_data': f'ap_sstart_{pid}'},
         {'text': '🔴 Fin', 'callback_data': f'ap_send_{pid}'}],
        [{'text': '🌐 24h siempre', 'callback_data': f'ap_s24_{pid}'}],
        [{'text': '⬅️ Volver', 'callback_data': f'ap_menu_{pid}'}],
    ]}

def hour_picker_text(pid, kind):
    label = "inicio" if kind == 'start' else "fin"
    return f"🕐 *Hora de {label}* — `{pid}`\n\nElige la hora en UTC (0-24)."

def hour_picker_keyboard(pid, kind):
    pref = 'ap_setstart' if kind == 'start' else 'ap_setend'
    rows, row = [], []
    for h in HOUR_PRESETS:
        row.append({'text': f'{h:02d}', 'callback_data': f'{pref}_{pid}_{h}'})
        if len(row) == 4: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{'text': '⬅️ Volver', 'callback_data': f'ap_sched_{pid}'}])
    return {'inline_keyboard': rows}

def parse_add_command(args):
    try:
        sp = args.split(maxsplit=1)
        if len(sp) < 2: return None, "❌ Falta el tags"
        chat_id = sp[0].strip()
        parts = [p.strip() for p in sp[1].split('|')]
        tags = parts[0]
        imin, imax = 5, 30
        if len(parts) >= 2 and parts[1]:
            iv = parts[1].split()
            imin = max(1, int(iv[0]))
            imax = max(imin, int(iv[1])) if len(iv) > 1 else imin * 2
        ms = 0
        if len(parts) >= 3 and parts[2]:
            ms = int(parts[2])
        ex = []
        if len(parts) >= 4 and parts[3]:
            ex = [x.strip() for x in parts[3].split(',') if x.strip()]
        return {'chat_id': chat_id, 'tags': tags, 'interval_min': imin,
                'interval_max': imax, 'min_score': ms, 'exclude': ex,
                'last_post': 0, 'next_delay': 0,
                'paused': False, 'active_hours': {'start': 0, 'end': 24},
                'photos_per_post': 1,
                'stats': {'total_sent': 0, 'total_failed': 0,
                          'today_sent': 0, 'today_failed': 0,
                          'today_date': '', 'last_post_ts': 0, 'last_success_ts': 0}}, None
    except Exception as e:
        return None, f"❌ Error: {e}"

def handle_autopost(chat_id, uid, text):
    if not is_admin(uid):
        send_message(chat_id, "❌ Solo admin.")
        return
    parts = text.split(maxsplit=2)
    if len(parts) == 1:
        send_message(chat_id, autopost_menu_text(), keyboard=autopost_menu_keyboard())
        return
    sub = parts[1].lower()
    if sub == 'add':
        if len(parts) < 3:
            send_message(chat_id,
                "📝 *Uso:*\n"
                "`/autopost add <chat_id> <tags> | <min> <max> | [score] | [excluir]`\n\n"
                "*Ejemplo:*\n"
                "`/autopost add -1001234567890 rem_(re:zero) | 10 30 | 100 | loli,guro`\n\n"
                "💡 Para el `chat_id`, reenvía un mensaje del canal a @userinfobot\n\n"
                "Después usa el panel para pausar/horario/fotos.")
            return
        task, err = parse_add_command(parts[2])
        if err:
            send_message(chat_id, err); return
        pid = uuid.uuid4().hex[:6]
        with autopost_lock:
            autoposts[pid] = task
        save_async()
        send_message(chat_id, f"✅ Tarea `{pid}` creada.\n\n{autopost_menu_text()}",
                     keyboard=autopost_menu_keyboard())
    elif sub == 'del':
        if len(parts) < 3: send_message(chat_id, "❌ Uso: `/autopost del <id>`"); return
        pid = parts[2].strip()
        with autopost_lock:
            if pid in autoposts:
                del autoposts[pid]; save_async()
                send_message(chat_id, f"✅ `{pid}` eliminada.")
            else:
                send_message(chat_id, f"❌ No existe `{pid}`")
    elif sub == 'test':
        if len(parts) < 3: send_message(chat_id, "❌ Uso: `/autopost test <id>`"); return
        pid = parts[2].strip()
        t = autoposts.get(pid)
        if not t: send_message(chat_id, f"❌ No existe `{pid}`"); return
        send_message(chat_id, f"🧪 Probando `{pid}`...")
        ok = post_to_channel(t)
        send_message(chat_id, "✅ Enviado" if ok else "❌ Falló")
    else:
        send_message(chat_id, "❓ Subcomando desconocido. Usa `/autopost`")

# ==================== WEBHOOK ====================
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update: return 'OK', 200
    uid = update.get('update_id')
    if uid in processed_updates: return 'OK', 200
    processed_updates.add(uid)
    if len(processed_updates) > 1000: processed_updates.clear()

    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        user_id = msg['from']['id']
        fname = msg['from'].get('first_name', 'usuario')
        if 'text' not in msg: return 'OK', 200
        text = msg['text'].strip()

        if msg['chat']['type'] in ('group', 'supergroup') and text.startswith('/') and '@' in text.split()[0]:
            text = text.split()[0].split('@')[0] + ' ' + ' '.join(text.split()[1:])
            text = text.strip()

        if text == '/start':
            send_message(chat_id,
                f"¡Hola, {fname}! Este bot envía imágenes del sitio web gelbooru.com por etiquetas.\n\n"
                "Para obtener ayuda sobre las etiquetas, escribe /ayuda.\n\n"
                "¡Solo tienes que introducir las etiquetas en el chat y esperar a que se envíen!",
                keyboard={'inline_keyboard': [[{'text': '⚙️ Configuración', 'callback_data': 'cfg_main'}]]})
        elif text in ('/ayuda', '/help'):
            send_message(chat_id,
                "📖 *Ayuda — Etiquetas de Gelbooru*\n\n"
                "• *Artista:* `wlop`, `sakimichan`\n"
                "• *Personaje:* `rem_(re:zero)`, `hatsune_miku`\n"
                "• *Copyright:* `genshin_impact`\n"
                "• *General:* `1girl`, `solo`, `blue_hair`\n\n"
                "🔹 Escribe las etiquetas, o usa `/find <tags>`.\n\n"
                "💡 Usa `_` en vez de espacios, excluye con `-tag`.\n\n"
                "🔧 Comandos:\n"
                "• `/start` — Inicio\n"
                "• `/ayuda` — Esta ayuda\n"
                "• `/find <tags>` — Buscar\n"
                "• `/config` — Tu configuración")
        elif text == '/config':
            send_message(chat_id, main_menu_text(user_id), keyboard=main_menu_keyboard(user_id))
        elif text.startswith('/autopost'):
            handle_autopost(chat_id, user_id, text)
        elif text.startswith('/find'):
            p = text.split(maxsplit=1)
            if len(p) < 2: send_message(chat_id, "❌ Uso: `/find término`")
            else: handle_search(chat_id, user_id, p[1].strip())
        elif text.startswith('/'):
            send_message(chat_id, "❓ Comando no reconocido. Usa /ayuda")
        else:
            handle_search(chat_id, user_id, text)

    elif 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        user_id = cb['from']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        cb_id = cb['id']

        # Buscar más
        if data.startswith('more:'):
            sid = data.split(':', 1)[1]
            s = sessions.get(sid)
            if not s:
                answer_callback(cb_id, "❌ Sesión expirada"); return 'OK', 200
            answer_callback(cb_id, "🔄 Buscando...")
            send_next(chat_id, user_id, sid, s, reply_to=msg_id)

        # Config usuario
        elif data == 'cfg_main':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, main_menu_text(user_id), keyboard=main_menu_keyboard(user_id))
        elif data == 'cfg_info':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, info_menu_text(user_id), keyboard=info_menu_keyboard(user_id))
        elif data == 'cfg_media':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, media_menu_text(user_id), keyboard=media_menu_keyboard(user_id))
        elif data == 'cfg_rating':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, rating_menu_text(user_id), keyboard=rating_menu_keyboard(user_id))
        elif data == 'cfg_score':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, score_menu_text(user_id), keyboard=score_menu_keyboard(user_id))
        elif data == 'cfg_filters':
            if get_user_config(user_id)['rating'] != 'explicit':
                answer_callback(cb_id, "❌ Activa 'Explicit' primero"); return 'OK', 200
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, filters_menu_text(user_id), keyboard=filters_menu_keyboard(user_id))
        elif data.startswith('cfg_f_'):
            k = data.replace('cfg_f_', '')
            c = get_user_config(user_id)
            if k == 'none': c['fields'] = set()
            elif k == 'all': c['fields'] = set(ALL_FIELDS)
            elif k in ALL_FIELDS:
                c['fields'].discard(k) if k in c['fields'] else c['fields'].add(k)
            answer_callback(cb_id); save_async()
            edit_message(chat_id, msg_id, info_menu_text(user_id), keyboard=info_menu_keyboard(user_id))
        elif data.startswith('cfg_m_'):
            k = data.replace('cfg_m_', '')
            c = get_user_config(user_id)
            if k == 'all': c['media'] = set(ALL_MEDIA)
            elif k == 'none': c['media'] = set()
            elif k in ALL_MEDIA:
                c['media'].discard(k) if k in c['media'] else c['media'].add(k)
            answer_callback(cb_id); save_async()
            edit_message(chat_id, msg_id, media_menu_text(user_id), keyboard=media_menu_keyboard(user_id))
        elif data.startswith('cfg_r_'):
            v = data.replace('cfg_r_', '')
            if v == 'all': v = ''
            c = get_user_config(user_id)
            c['rating'] = v
            if v != 'explicit': c['content_filters'] = set()
            answer_callback(cb_id, f"🎚️ {v or 'todos'}"); save_async()
            edit_message(chat_id, msg_id, rating_menu_text(user_id), keyboard=rating_menu_keyboard(user_id))
        elif data.startswith('cfg_s_'):
            try: v = int(data.replace('cfg_s_', ''))
            except: v = 0
            get_user_config(user_id)['min_score'] = v
            answer_callback(cb_id, f"⭐ ≥{v}" if v else "🌐 Cualquiera"); save_async()
            edit_message(chat_id, msg_id, score_menu_text(user_id), keyboard=score_menu_keyboard(user_id))
        elif data.startswith('cfg_cf_'):
            k = data.replace('cfg_cf_', '')
            c = get_user_config(user_id)
            if k == 'all': c['content_filters'] = set(CONTENT_FILTERS.keys())
            elif k == 'none': c['content_filters'] = set()
            elif k in CONTENT_FILTERS:
                c['content_filters'].discard(k) if k in c['content_filters'] else c['content_filters'].add(k)
            answer_callback(cb_id); save_async()
            edit_message(chat_id, msg_id, filters_menu_text(user_id), keyboard=filters_menu_keyboard(user_id))

        # Autopost
        elif data == 'ap_new':
            answer_callback(cb_id, "Usa /autopost add ...")
        elif data == 'ap_refresh':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, autopost_menu_text(), keyboard=autopost_menu_keyboard())
        elif data.startswith('ap_menu_'):
            pid = data.replace('ap_menu_', '')
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, task_menu_text(pid), keyboard=task_menu_keyboard(pid))
        elif data.startswith('ap_toggle_'):
            pid = data.replace('ap_toggle_', '')
            t = autoposts.get(pid)
            if t:
                t['paused'] = not t.get('paused', False)
                answer_callback(cb_id, "⏸️ Pausado" if t['paused'] else "▶️ Reanudado")
                save_async()
            edit_message(chat_id, msg_id, task_menu_text(pid), keyboard=task_menu_keyboard(pid))
        elif data.startswith('ap_test_'):
            pid = data.replace('ap_test_', '')
            t = autoposts.get(pid)
            if t:
                answer_callback(cb_id, "🧪 Enviando...")
                ok = post_to_channel(t)
                send_message(chat_id, "✅ Enviado" if ok else "❌ Falló el envío")
        elif data.startswith('ap_photos_'):
            pid = data.replace('ap_photos_', '')
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, photos_menu_text(pid), keyboard=photos_menu_keyboard(pid))
        elif data.startswith('ap_setph_'):
            rest = data.replace('ap_setph_', '')
            pid, n = rest.rsplit('_', 1)
            try: n = int(n)
            except: n = 1
            t = autoposts.get(pid)
            if t:
                t['photos_per_post'] = n
                save_async()
                answer_callback(cb_id, f"🖼️ {n} fotos/post")
            edit_message(chat_id, msg_id, photos_menu_text(pid), keyboard=photos_menu_keyboard(pid))
        elif data.startswith('ap_sched_'):
            pid = data.replace('ap_sched_', '')
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, sched_menu_text(pid), keyboard=sched_menu_keyboard(pid))
        elif data.startswith('ap_s24_'):
            pid = data.replace('ap_s24_', '')
            t = autoposts.get(pid)
            if t:
                t['active_hours'] = {'start': 0, 'end': 24}
                save_async()
                answer_callback(cb_id, "🌐 24h activado")
            edit_message(chat_id, msg_id, sched_menu_text(pid), keyboard=sched_menu_keyboard(pid))
        elif data.startswith('ap_sstart_'):
            pid = data.replace('ap_sstart_', '')
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, hour_picker_text(pid, 'start'),
                         keyboard=hour_picker_keyboard(pid, 'start'))
        elif data.startswith('ap_send_'):
            pid = data.replace('ap_send_', '')
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, hour_picker_text(pid, 'end'),
                         keyboard=hour_picker_keyboard(pid, 'end'))
        elif data.startswith('ap_setstart_'):
            rest = data.replace('ap_setstart_', '')
            pid, h = rest.rsplit('_', 1)
            try: h = int(h)
            except: h = 0
            t = autoposts.get(pid)
            if t:
                t['active_hours']['start'] = h
                save_async()
                answer_callback(cb_id, f"🟢 Inicio: {h:02d}:00 UTC")
            edit_message(chat_id, msg_id, sched_menu_text(pid), keyboard=sched_menu_keyboard(pid))
        elif data.startswith('ap_setend_'):
            rest = data.replace('ap_setend_', '')
            pid, h = rest.rsplit('_', 1)
            try: h = int(h)
            except: h = 24
            t = autoposts.get(pid)
            if t:
                t['active_hours']['end'] = h
                save_async()
                answer_callback(cb_id, f"🔴 Fin: {h:02d}:00 UTC")
            edit_message(chat_id, msg_id, sched_menu_text(pid), keyboard=sched_menu_keyboard(pid))
        elif data.startswith('ap_del2_'):
            pid = data.replace('ap_del2_', '')
            with autopost_lock:
                if pid in autoposts:
                    del autoposts[pid]; save_async()
                    answer_callback(cb_id, f"🗑️ {pid} eliminado")
                else:
                    answer_callback(cb_id, "❌ No existe")
            edit_message(chat_id, msg_id, autopost_menu_text(), keyboard=autopost_menu_keyboard())

    return 'OK', 200

@app.route('/')
def index():
    return "Bot activo", 200

# ==================== SETUP ====================
_setup_done = False

def setup_webhook():
    ext = os.environ.get("RENDER_EXTERNAL_URL")
    if not ext:
        logger.warning("⚠️ RENDER_EXTERNAL_URL no definida"); return
    url = f"{ext}/webhook/{BOT_TOKEN}"
    try:
        r = requests.post(f"{API_URL}/setWebhook", json={'url': url})
        logger.info(f"✅ Webhook: {url}" if r.ok else f"❌ {r.text}")
    except Exception as e:
        logger.error(f"❌ setup_webhook: {e}")

@app.before_request
def _ensure_webhook():
    global _setup_done
    if not _setup_done:
        _setup_done = True
        try:
            setup_webhook()
            start_worker_once()
        except Exception:
            pass

if __name__ == '__main__':
    load_data()
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    start_worker_once()
    app.run(host='0.0.0.0', port=port)