#!/usr/bin/env python3
"""
Bot de Telegram para buscar en Gelbooru - v12
+ Suscripción obligatoria al canal
+ Rotación de múltiples cuentas de Gelbooru
"""

import os
import sys
import json
import uuid
import logging
import threading
import itertools
import concurrent.futures
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

# Canal de soporte obligatorio
SUPPORT_CHANNEL_ID = os.environ.get("SUPPORT_CHANNEL_ID", "").strip()
SUPPORT_CHANNEL_LINK = os.environ.get("SUPPORT_CHANNEL_LINK", "").strip()

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
user_states = {}
sessions = {}
processed_updates = set()
persistent_sent = {}
_tag_type_cache = {}
_tag_cache_lock = threading.Lock()
_membership_cache = {}  # {user_id: (is_member:bool, timestamp:float)}
MEMBERSHIP_TTL = 300   # 5 min

ALL_FIELDS = ['id', 'uploader', 'date', 'size', 'source', 'score', 'rating',
              'tags', 'artist', 'character', 'copyright']
DEFAULT_FIELDS = {'id', 'date', 'size', 'score', 'rating', 'tags'}
ALL_MEDIA = ['image', 'gif', 'video']
DEFAULT_MEDIA = set(ALL_MEDIA)
VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.avi', '.mkv')

CONTENT_FILTERS = {
    'gay':      {'label': '🏳️‍🌈 Filtro etiquetas Gay',     'tags': ['yaoi', 'male/male']},
    'lesbian':  {'label': '👩‍❤️‍👩 Filtro etiquetas Lesbianas', 'tags': ['yuri', 'female/female']},
    'furry':    {'label': '🐾 Filtro Furry',              'tags': ['furry', 'anthro']},
    'scat':     {'label': '💩 Filtro Scat',               'tags': ['scat']},
    'futanari': {'label': '⚧️ Filtro Futanari',           'tags': ['futanari', 'futa']},
    'gore':     {'label': '🩸 Filtro Gore',               'tags': ['gore', 'blood', 'guro']},
}

AI_TAGS = ['ai_generated', 'ai-assisted', 'ai_assisted']
AI_MODES = ['any', 'only', 'exclude']
AI_MODE_LABEL = {
    'any': '🌐 Todos',
    'only': '✅ Solo IA',
    'exclude': '❌ Sin IA',
}

SCORE_PRESETS = [0, 25, 50, 100, 200, 500]

# ==================== JSON ====================
def _serialize_config(cfg):
    return {
        'fields': list(cfg.get('fields', [])),
        'rating': cfg.get('rating', ''),
        'media': list(cfg.get('media', [])),
        'min_score': cfg.get('min_score', 0),
        'content_filters': list(cfg.get('content_filters', [])),
        'include_tags': list(cfg.get('include_tags', [])),
        'exclude_tags': list(cfg.get('exclude_tags', [])),
        'ai_mode': cfg.get('ai_mode', 'any'),
    }

def _deserialize_config(d):
    return {
        'fields': set(d.get('fields', DEFAULT_FIELDS)),
        'rating': d.get('rating', ''),
        'media': set(d.get('media', DEFAULT_MEDIA)),
        'min_score': int(d.get('min_score', 0)),
        'content_filters': set(d.get('content_filters', [])),
        'include_tags': list(d.get('include_tags', [])),
        'exclude_tags': list(d.get('exclude_tags', [])),
        'ai_mode': d.get('ai_mode', 'any'),
    }

def save_data():
    try:
        with _save_lock:
            payload = {
                'user_configs': {str(k): _serialize_config(v) for k, v in user_configs.items()},
                'saved_at': datetime.utcnow().isoformat(),
            }
            tmp = DATA_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"❌ save_data: {e}")

def load_data():
    global user_configs
    try:
        if not os.path.exists(DATA_FILE):
            logger.info("📂 No hay JSON previo")
            return
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        user_configs = {int(k): _deserialize_config(v)
                        for k, v in payload.get('user_configs', {}).items()}
        logger.info(f"✅ JSON: {len(user_configs)} configs")
    except Exception as e:
        logger.error(f"❌ load_data: {e}")

def save_async():
    threading.Thread(target=save_data, daemon=True).start()

# ==================== SUSCRIPCIÓN AL CANAL ====================
def is_user_member(user_id):
    """
    Verifica si el usuario es miembro del canal de soporte.
    Usa cache de 5 minutos para evitar llamar a la API en cada mensaje.
    Si no hay canal configurado → permite siempre.
    """
    if not SUPPORT_CHANNEL_ID:
        return True

    now = datetime.utcnow().timestamp()
    cached = _membership_cache.get(user_id)
    if cached and (now - cached[1]) < MEMBERSHIP_TTL:
        return cached[0]

    try:
        r = requests.post(f"{API_URL}/getChatMember",
                          json={"chat_id": SUPPORT_CHANNEL_ID, "user_id": user_id},
                          timeout=8)
        if r.ok:
            data = r.json()
            if data.get("ok"):
                status = data["result"].get("status", "")
                member = status in ("creator", "administrator", "member")
                _membership_cache[user_id] = (member, now)
                return member
        _membership_cache[user_id] = (False, now)
        return False
    except Exception as e:
        logger.error(f"❌ is_user_member: {e}")
        # Si falla la verificación, dejar pasar para no romper el bot
        return cached[0] if cached else True

def invalidate_membership(user_id):
    _membership_cache.pop(user_id, None)

def ask_to_join(chat_id):
    """Manda mensaje pidiendo unirse al canal."""
    link = SUPPORT_CHANNEL_LINK or "https://t.me/"
    kb = {
        "inline_keyboard": [
            [{"text": "📢 Unirse al canal", "url": link}],
            [{"text": "✅ Ya me uní, verificar", "callback_data": "check_membership"}],
        ]
    }
    send_message(
        chat_id,
        "⚠️ *Acceso restringido*\n\n"
        "Para usar este bot debes unirte primero a nuestro canal oficial "
        "de soporte e información.\n\n"
        "Una vez dentro, pulsa *✅ Ya me uní, verificar*.",
        keyboard=kb
    )

# ==================== GELBOORU API (rotación de cuentas) ====================
class GelbooruAPI:
    def __init__(self):
        # Carga cuentas desde GELBOORU_ACCOUNTS (JSON array)
        accounts = []
        raw = os.environ.get("GELBOORU_ACCOUNTS", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and item.get('api_key') and item.get('user_id'):
                            accounts.append({
                                'api_key': str(item['api_key']).strip(),
                                'user_id': str(item['user_id']).strip(),
                            })
            except Exception as e:
                logger.error(f"❌ GELBOORU_ACCOUNTS parse error: {e}")

        # Fallback: variables individuales
        if not accounts:
            k = os.environ.get("GELBOORU_API_KEY")
            u = os.environ.get("GELBOORU_USER_ID")
            if k and u:
                accounts.append({'api_key': k.strip(), 'user_id': u.strip()})

        self.accounts = accounts
        self.account_cycle = itertools.cycle(self.accounts) if self.accounts else None
        self.account_lock = threading.Lock()
        self.base_url = "https://gelbooru.com/index.php"
        logger.info(f"🔑 Cuentas Gelbooru cargadas: {len(self.accounts)}")

    def _proxies(self):
        return {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None

    def _next_account(self):
        if not self.account_cycle:
            return None, None
        with self.account_lock:
            acc = next(self.account_cycle)
        return acc['api_key'], acc['user_id']

    def _base_params(self):
        return {"page": "dapi", "s": "post", "q": "index", "json": 1}

    def _build_params(self, tags, page=1, limit=100):
        params = {"page": "dapi", "s": "post", "q": "index", "json": 1,
                  "tags": tags, "pid": (page - 1) * limit, "limit": limit}
        k, u = self._next_account()
        if k and u:
            params["api_key"] = k
            params["user_id"] = u
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

    def lookup_tag_type(self, tag_name):
        """Consulta el tipo de tag rotando cuenta."""
        try:
            params = {"page": "dapi", "s": "tag", "q": "index", "json": 1,
                      "name": tag_name, "limit": 1}
            k, u = self._next_account()
            if k and u:
                params["api_key"] = k
                params["user_id"] = u
            r = requests.get(self.base_url, params=params,
                             headers={'User-Agent': USER_AGENT},
                             timeout=10, proxies=self._proxies())
            if r.ok:
                data = r.json()
                td = data.get('tag', [])
                if isinstance(td, dict):
                    td = [td]
                if td and isinstance(td[0], dict):
                    try:
                        return int(td[0].get('type', 0))
                    except (ValueError, TypeError):
                        return None
        except Exception as e:
            logger.debug(f"lookup_tag_type falló {tag_name}: {e}")
        return None

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
            for kk in ['file_url', 'sample_url']:
                if img[kk] and not img[kk].startswith('http'):
                    img[kk] = urljoin(SITE_BASE_URL, img[kk])
            if not img['file_url'] and img['sample_url']:
                img['file_url'] = img['sample_url']
            if img['file_url']:
                out.append(img)
        return out

gelbooru = GelbooruAPI()

# ==================== TAG TYPE LOOKUP ====================
def _lookup_tag_type(tag_name):
    with _tag_cache_lock:
        if tag_name in _tag_type_cache:
            return _tag_type_cache[tag_name]
    tp = gelbooru.lookup_tag_type(tag_name)
    if tp is not None:
        with _tag_cache_lock:
            _tag_type_cache[tag_name] = tp
        return tp
    # No cachear fallos → reintenta luego
    return 0

def categorize_tags(tags_str, max_tags=60):
    empty = {'artist': [], 'character': [], 'copyright': [], 'general': [], 'meta': []}
    if not tags_str:
        return empty
    tags = tags_str.split()[:max_tags]

    with _tag_cache_lock:
        missing = [t for t in tags if t not in _tag_type_cache]

    if missing:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_lookup_tag_type, missing))
        except Exception as e:
            logger.error(f"❌ categorize_tags: {e}")

    result = {'artist': [], 'character': [], 'copyright': [], 'general': [], 'meta': []}
    type_map = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'meta'}
    with _tag_cache_lock:
        for t in tags:
            tp = _tag_type_cache.get(t, 0)
            cat = type_map.get(tp, 'general')
            result[cat].append(t)
    return result

# ==================== HELPERS ====================
def get_media_type(url):
    if not url: return 'image'
    u = url.lower().split('?')[0]
    if u.endswith('.gif'): return 'gif'
    if u.endswith(VIDEO_EXTS): return 'video'
    return 'image'

def get_user_config(uid):
    if uid not in user_configs:
        user_configs[uid] = {'fields': set(DEFAULT_FIELDS), 'rating': '',
                             'media': set(DEFAULT_MEDIA), 'min_score': 0,
                             'content_filters': set(),
                             'include_tags': [], 'exclude_tags': [],
                             'ai_mode': 'any'}
    c = user_configs[uid]
    c.setdefault('min_score', 0)
    c.setdefault('content_filters', set())
    c.setdefault('include_tags', [])
    c.setdefault('exclude_tags', [])
    c.setdefault('ai_mode', 'any')
    return c

def format_date(s):
    if not s: return ''
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

def format_caption(img, uid):
    f = get_user_config(uid)['fields']
    if not f: return ""
    lines = []

    needs_cat = any(x in f for x in ('artist', 'character', 'copyright'))
    cat = categorize_tags(img.get('tags', '')) if needs_cat else None

    if 'artist' in f and cat and cat['artist']:
        a = ' '.join(cat['artist'])
        if len(a) > 300: a = a[:297] + '...'
        lines.append(f"🎨 *Artista:* `{a}`")
    if 'character' in f and cat and cat['character']:
        c = ' '.join(cat['character'])
        if len(c) > 300: c = c[:297] + '...'
        lines.append(f"👤 *Personajes:* `{c}`")
    if 'copyright' in f and cat and cat['copyright']:
        cp = ' '.join(cat['copyright'])
        if len(cp) > 300: cp = cp[:297] + '...'
        lines.append(f"©️ *Franquicia:* `{cp}`")
    if 'id' in f and img.get('id'):
        lines.append(f"🖼️ *ID:* `{img['id']}`")
    if 'uploader' in f:
        lines.append(f"📤 *Uploader:* {img.get('uploader') or 'Anónimo'}")
    if 'date' in f and img.get('created_at'):
        lines.append(f"📅 *Posted:* {format_date(img['created_at'])}")
    if 'size' in f and img.get('width'):
        lines.append(f"📐 *Tamaño:* {img['width']}x{img['height']}")
    if 'source' in f and img.get('source'):
        s = img['source']
        if len(s) > 200: s = s[:197] + '...'
        lines.append(f"🔗 *Source:* {s}")
    sr = []
    if 'score' in f: sr.append(f"⭐ {img.get('score', 0)}")
    if 'rating' in f: sr.append(f"📊 `{(img.get('rating') or '').upper()}`")
    if sr: lines.append("  |  ".join(sr))
    if 'tags' in f and img.get('tags'):
        t = img['tags']
        if len(t) > 900: t = t[:897] + '...'
        lines.append(f"🏷️ `{t}`")
    return "\n".join(lines)

# ==================== MENÚS ====================
def main_menu_text(uid):
    c = get_user_config(uid)
    rd = {'': '🌐 Todos', 'safe': '✅ Safe', 'general': '🟢 General',
          'questionable': '🟡 Questionable', 'explicit': '🔴 Explicit'}.get(c['rating'], c['rating'])
    mi = ' '.join(v for k, v in {'image':'📷','gif':'🎞️','video':'🎬'}.items() if k in c['media']) or '❌'
    st = f"⭐ ≥{c['min_score']}" if c['min_score'] > 0 else "🌐 Cualquiera"
    ft = f"🚫 {len(c['content_filters'])} activos" if c['content_filters'] else "🌐 Ninguno"
    ai = AI_MODE_LABEL.get(c.get('ai_mode', 'any'), '🌐 Todos')
    return ("⚙️ *Configuración*\n\n"
            "Personaliza tus búsquedas. Los cambios son solo para ti.\n\n"
            f"📋 *Info:* {len(c['fields'])}/11 campos\n"
            f"🎬 *Tipo:* {mi}\n"
            f"🎚️ *Rating:* {rd}\n"
            f"🤖 *IA:* {ai}\n"
            f"⭐ *Score mín:* {st}\n"
            f"🚫 *Filtros:* {ft}\n"
            f"➕ *Incluir:* {len(c['include_tags'])} tags\n"
            f"➖ *Excluir:* {len(c['exclude_tags'])} tags\n")

def main_menu_keyboard(uid):
    c = get_user_config(uid)
    rows = [
        [{'text': '📋 Información de imagen', 'callback_data': 'cfg_info'}],
        [{'text': '🎬 Tipo de contenido', 'callback_data': 'cfg_media'}],
        [{'text': '🎚️ Filtrar por rating', 'callback_data': 'cfg_rating'}],
        [{'text': '🤖 Contenido IA', 'callback_data': 'cfg_ai'}],
        [{'text': '⭐ Score mínimo', 'callback_data': 'cfg_score'}],
    ]
    if c['rating'] == 'explicit':
        rows.append([{'text': '🚫 Filtros de contenido', 'callback_data': 'cfg_filters'}])
    rows.append([{'text': '➕ Incluir tags personalizados', 'callback_data': 'cfg_inc'}])
    rows.append([{'text': '➖ Excluir tags personalizados', 'callback_data': 'cfg_exc'}])
    rows.append([{'text': '❓ Cómo usar el menú', 'callback_data': 'cfg_help'}])
    return {'inline_keyboard': rows}

def ai_menu_text(uid):
    c = get_user_config(uid)
    mode = c.get('ai_mode', 'any')
    cur = AI_MODE_LABEL.get(mode, '🌐 Todos')
    extra = ""
    if mode == 'only':
        extra = "\n\n✅ Solo se mostrarán imágenes con tag `ai_generated`."
    elif mode == 'exclude':
        extra = "\n\n❌ Se excluirán todas las imágenes con tags IA."
    else:
        extra = "\n\n🌐 No se aplica ningún filtro de IA."
    return ("🤖 *Contenido IA*\n\n"
            "Elige cómo tratar el contenido generado por inteligencia artificial.\n\n"
            f"*Actual:* {cur}" + extra +
            "\n\n💡 También puedes usar el comando `/ia` para cambiar rápido.")

def ai_menu_keyboard(uid):
    c = get_user_config(uid)
    mode = c.get('ai_mode', 'any')
    def b(label, val):
        return {'text': ('✅ ' if mode == val else '') + label, 'callback_data': f'cfg_ai_{val}'}
    return {'inline_keyboard': [
        [b('🌐 Todos', 'any')],
        [b('✅ Solo IA', 'only')],
        [b('❌ Sin IA', 'exclude')],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def info_menu_text(uid):
    c = get_user_config(uid)
    return ("📋 *Información de imagen*\n\n"
            "Activa o desactiva qué datos aparecen debajo de cada imagen.\n\n"
            f"📊 *Campos activos:* {len(c['fields'])}/11\n\n"
            "⚠️ _Artista/Personaje/Franquicia requieren consulta extra "
            "la primera vez que se ven._")

def info_menu_keyboard(uid):
    f = get_user_config(uid)['fields']
    def b(l, k): return {'text': ('✅ ' if k in f else '❌ ') + l, 'callback_data': f'cfg_f_{k}'}
    return {'inline_keyboard': [
        [b('🖼️ ID', 'id'), b('📤 Uploader', 'uploader')],
        [b('📅 Fecha', 'date'), b('📐 Tamaño', 'size')],
        [b('🔗 Source', 'source'), b('⭐ Score', 'score')],
        [b('📊 Rating', 'rating'), b('🏷️ Tags', 'tags')],
        [b('🎨 Artista', 'artist'), b('👤 Personajes', 'character')],
        [b('©️ Franquicia', 'copyright')],
        [{'text': '🔕 Sin info (solo media)', 'callback_data': 'cfg_f_none'}],
        [{'text': '✅ Activar todo', 'callback_data': 'cfg_f_all'}],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def media_menu_text(uid):
    return ("🎬 *Tipo de contenido*\n\n"
            "Elige qué tipo de archivos quieres recibir en tus búsquedas.\n\n"
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
        note = "\n\n💡 Con `Explicit` aparece el botón `🚫 Filtros de contenido`."
    return ("🎚️ *Filtrar por rating*\n\n"
            "Filtra las imágenes según su clasificación.\n\n"
            f"*Actual:* {rd}\n\n"
            "ℹ️ *Safe* → contenido seguro\n"
            "ℹ️ *General* → todo público\n"
            "ℹ️ *Questionable* → sugerente\n"
            "ℹ️ *Explicit* → contenido adulto" + note)

def rating_menu_keyboard(uid):
    r = get_user_config(uid)['rating']
    def b(l, v): return {'text': ('✅ ' if r == v else '') + l, 'callback_data': f'cfg_r_{v or "all"}'}
    return {'inline_keyboard': [
        [b('🌐 Todos', '')],
        [b('✅ Safe', 'safe')],
        [b('🟢 General', 'general')],
        [b('🟡 Questionable', 'questionable')],
        [b('🔴 Explicit', 'explicit')],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def score_menu_text(uid):
    s = get_user_config(uid)['min_score']
    return ("⭐ *Score mínimo*\n\n"
            "Solo se mostrarán imágenes con score igual o superior.\n\n"
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
            "Activa los filtros para *excluir* esas categorías.\n\n"
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

def tags_menu_text(uid, kind):
    c = get_user_config(uid)
    tags = c['include_tags'] if kind == 'inc' else c['exclude_tags']
    kind_label = "incluir" if kind == 'inc' else "excluir"
    prepend = "" if kind == 'inc' else "-"
    return (f"{'➕' if kind == 'inc' else '➖'} *Tags personalizados para {kind_label}*\n\n"
            f"Estos tags se añaden a *todas* tus búsquedas.\n"
            f"Formato aplicado: `{prepend}<tag>`\n\n"
            f"📊 *Tags actuales:* {len(tags)}\n\n"
            + ("_No hay ninguno todavía._"
               if not tags else "\n".join(f"• `{t}`" for t in tags)))

def tags_menu_keyboard(uid, kind):
    c = get_user_config(uid)
    tags = c['include_tags'] if kind == 'inc' else c['exclude_tags']
    rows = [[{'text': '➕ Añadir tag', 'callback_data': f'cfg_{kind}_add'}]]
    for i, t in enumerate(tags):
        rows.append([{'text': f'🗑️ {t}', 'callback_data': f'cfg_{kind}_del_{i}'}])
    rows.append([{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}])
    return {'inline_keyboard': rows}

def help_menu_text():
    return (
        "❓ *Cómo usar el menú de configuración*\n\n"
        "*📋 Información de imagen*\n"
        "Elige qué datos aparecen debajo de cada imagen, incluyendo "
        "*Artista*, *Personajes* y *Franquicia*.\n\n"
        "*🎬 Tipo de contenido*\n"
        "Filtra entre imágenes, GIFs o vídeos.\n\n"
        "*🎚️ Filtrar por rating*\n"
        "• `Safe` → seguro\n"
        "• `General` → público\n"
        "• `Questionable` → sugerente\n"
        "• `Explicit` → adulto\n\n"
        "*🤖 Contenido IA*\n"
        "3 modos:\n"
        "• `🌐 Todos` → sin filtro IA\n"
        "• `✅ Solo IA` → solo imágenes con tag `ai_generated`\n"
        "• `❌ Sin IA` → excluye todo lo IA\n\n"
        "*⭐ Score mínimo*\n"
        "Solo recibirás imágenes con score ≥ al elegido.\n\n"
        "*🚫 Filtros de contenido* _(solo con rating explicit)_\n"
        "Excluye categorías: gay, lesbianas, furry, scat, futanari, gore.\n\n"
        "*➕ Incluir tags*\n"
        "Tags que se añaden a todas tus búsquedas.\n\n"
        "*➖ Excluir tags*\n"
        "Tags que se quitan de todas tus búsquedas.\n\n"
        "💡 _Nota:_ Artista/Personajes/Franquicia la primera vez tardan un poco "
        "(el bot aprende los tipos de tags y los cachea)."
    )

def help_menu_keyboard():
    return {'inline_keyboard': [[{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}]]}

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

def answer_callback(cb_id, text=None, show_alert=False):
    try:
        data = {'callback_query_id': cb_id}
        if text: data['text'] = text
        if show_alert: data['show_alert'] = True
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

# ==================== BÚSQUEDA ====================
def build_query(uid, query):
    c = get_user_config(uid)
    parts = [query]

    ai_mode = c.get('ai_mode', 'any')
    if ai_mode == 'only':
        parts.append('ai_generated')
    elif ai_mode == 'exclude':
        for t in AI_TAGS:
            parts.append(f"-{t}")

    if c['rating']: parts.append(f"rating:{c['rating']}")
    if c.get('min_score', 0) > 0: parts.append(f"score:>={c['min_score']}")
    for t in c.get('include_tags', []):
        parts.append(t)
    for t in c.get('exclude_tags', []):
        parts.append(f"-{t}")
    for k in c.get('content_filters', set()):
        for t in CONTENT_FILTERS.get(k, {}).get('tags', []):
            parts.append(f"-{t}")
    return ' '.join(p for p in parts if p).strip()

def get_or_create_sent_ids(chat_id, full_query):
    d = persistent_sent.setdefault(chat_id, {})
    if full_query not in d and len(d) >= 5:
        try:
            oldest = next(iter(d))
            del d[oldest]
        except Exception:
            pass
    if full_query not in d:
        d[full_query] = set()
    return d[full_query]

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
        err_text = f"❌ No se encontraron más resultados para `{s['query']}`"
        if reply_to:
            edit_message(chat_id, reply_to, err_text)
        else:
            send_message(chat_id, err_text)
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
    full_query = build_query(uid, query)
    sent_ids = get_or_create_sent_ids(chat_id, full_query)
    sid = uuid.uuid4().hex[:12]
    sessions[sid] = {'query': query, 'full_query': full_query,
                     'page': 1, 'images': [], 'current_index': 0,
                     'sent_ids': sent_ids,
                     'media': set(c['media']),
                     'min_score': c.get('min_score', 0)}
    st = send_message(chat_id, f"🔎 Buscando `{query}`...")
    send_next(chat_id, uid, sid, sessions[sid], reply_to=st)

def cycle_ai_mode(uid):
    c = get_user_config(uid)
    cur = c.get('ai_mode', 'any')
    try:
        idx = AI_MODES.index(cur)
    except ValueError:
        idx = 0
    new_mode = AI_MODES[(idx + 1) % len(AI_MODES)]
    c['ai_mode'] = new_mode
    save_async()
    return new_mode

# ==================== WEBHOOK ====================
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update: return 'OK', 200
    uid = update.get('update_id')
    if uid in processed_updates: return 'OK', 200
    processed_updates.add(uid)
    if len(processed_updates) > 1000: processed_updates.clear()

    # ---------- SUSCRIPCIÓN OBLIGATORIA ----------
    user_id = None
    chat_id = None
    cb_data = None
    cb_id = None
    if 'message' in update:
        user_id = update['message']['from']['id']
        chat_id = update['message']['chat']['id']
    elif 'callback_query' in update:
        user_id = update['callback_query']['from']['id']
        chat_id = update['callback_query']['message']['chat']['id']
        cb_data = update['callback_query'].get('data', '')
        cb_id = update['callback_query']['id']

    if user_id and SUPPORT_CHANNEL_ID:
        # Caso especial: usuario pulsó "Ya me uní"
        if cb_data == 'check_membership':
            invalidate_membership(user_id)
            if is_user_member(user_id):
                answer_callback(cb_id, "✅ ¡Verificado! Ya puedes usar el bot.")
                try:
                    requests.post(f"{API_URL}/deleteMessage",
                                  json={'chat_id': chat_id,
                                        'message_id': update['callback_query']['message']['message_id']},
                                  timeout=5)
                except Exception:
                    pass
                send_message(chat_id,
                    "✅ *¡Gracias por unirte!*\n\n"
                    "Ya puedes usar el bot. Escribe /ayuda para ver cómo buscar.")
            else:
                answer_callback(cb_id, "❌ Aún no estás en el canal. Únete primero.", show_alert=True)
                ask_to_join(chat_id)
            return 'OK', 200

        # Verificación normal
        if not is_user_member(user_id):
            ask_to_join(chat_id)
            return 'OK', 200
    # ---------- FIN SUSCRIPCIÓN ----------

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

        if user_id in user_states:
            action = user_states[user_id].get('action')
            if text in ('/cancel', '/cancelar'):
                del user_states[user_id]
                send_message(chat_id, "❌ Cancelado"); return 'OK', 200
            tag = text.strip().lstrip('-').replace(' ', '_')
            if not tag:
                send_message(chat_id, "❌ Tag vacío, intenta de nuevo o /cancel")
                return 'OK', 200
            c = get_user_config(user_id)
            if action == 'add_include':
                if tag not in c['include_tags']:
                    c['include_tags'].append(tag)
                del user_states[user_id]; save_async()
                send_message(chat_id,
                    f"✅ Tag `{tag}` añadido a *incluir*.\n\n{tags_menu_text(user_id, 'inc')}",
                    keyboard=tags_menu_keyboard(user_id, 'inc'))
            elif action == 'add_exclude':
                if tag not in c['exclude_tags']:
                    c['exclude_tags'].append(tag)
                del user_states[user_id]; save_async()
                send_message(chat_id,
                    f"✅ Tag `{tag}` añadido a *excluir*.\n\n{tags_menu_text(user_id, 'exc')}",
                    keyboard=tags_menu_keyboard(user_id, 'exc'))
            return 'OK', 200

        if text == '/start':
            send_message(chat_id,
                f"¡Hola, {fname}! Este bot envía imágenes del sitio web gelbooru.com por etiquetas.\n\n"
                "Para obtener ayuda sobre las etiquetas, escribe /ayuda.\n\n"
                "¡Solo tienes que introducir las etiquetas en el chat y esperar a que se envíen!",
                keyboard={'inline_keyboard': [[{'text': '⚙️ Configuración', 'callback_data': 'cfg_main'}]]})
        elif text in ('/ayuda', '/help'):
            send_message(chat_id,
                "📖 *Ayuda — Etiquetas de Gelbooru*\n\n"
                "Gelbooru clasifica las imágenes con *etiquetas (tags)*. "
                "Categorías comunes:\n\n"
                "• *Artista:* `wlop`, `sakimichan`\n"
                "• *Personaje:* `rem_(re:zero)`, `hatsune_miku`\n"
                "• *Copyright:* `genshin_impact`, `blue_archive`\n"
                "• *General:* `1girl`, `solo`, `blue_hair`\n"
                "• *Meta:* información técnica\n\n"
                "🔹 *Cómo buscar:*\n"
                "Escribe las etiquetas separadas por espacios, o usa `/find <tags>`.\n\n"
                "*Ejemplos:*\n"
                "`rem re:zero`\n"
                "`hatsune_miku blue_hair`\n"
                "`1girl solo`\n\n"
                "💡 *Tips:*\n"
                "• Usa `_` en vez de espacios dentro de una etiqueta\n"
                "• Combina varias etiquetas para precisar la búsqueda\n"
                "• Puedes excluir con `-tag` (ej: `1girl -loli`)\n\n"
                "🔧 *Comandos:*\n"
                "• `/start` — Inicio\n"
                "• `/ayuda` — Esta ayuda\n"
                "• `/find <tags>` — Buscar\n"
                "• `/ia` — Cambiar modo IA (todos/solo/sin)\n"
                "• `/config` — Tu configuración")
        elif text == '/config':
            send_message(chat_id, main_menu_text(user_id), keyboard=main_menu_keyboard(user_id))
        elif text == '/ia':
            new_mode = cycle_ai_mode(user_id)
            label = AI_MODE_LABEL.get(new_mode, '🌐 Todos')
            send_message(chat_id, f"🤖 Modo IA cambiado a: *{label}*")
        elif text.startswith('/find'):
            p = text.split(maxsplit=1)
            if len(p) < 2: send_message(chat_id, "❌ Uso: `/find término`\nEjemplo: `/find rem`")
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

        if data.startswith('more:'):
            sid = data.split(':', 1)[1]
            s = sessions.get(sid)
            if not s:
                answer_callback(cb_id, "❌ Sesión expirada"); return 'OK', 200
            answer_callback(cb_id, "🔄 Buscando...")
            send_next(chat_id, user_id, sid, s, reply_to=msg_id)

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
        elif data == 'cfg_ai':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, ai_menu_text(user_id), keyboard=ai_menu_keyboard(user_id))
        elif data == 'cfg_score':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, score_menu_text(user_id), keyboard=score_menu_keyboard(user_id))
        elif data == 'cfg_filters':
            if get_user_config(user_id)['rating'] != 'explicit':
                answer_callback(cb_id, "❌ Activa 'Explicit' primero"); return 'OK', 200
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, filters_menu_text(user_id), keyboard=filters_menu_keyboard(user_id))
        elif data == 'cfg_help':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, help_menu_text(), keyboard=help_menu_keyboard())

        elif data == 'cfg_inc':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, tags_menu_text(user_id, 'inc'),
                         keyboard=tags_menu_keyboard(user_id, 'inc'))
        elif data == 'cfg_exc':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, tags_menu_text(user_id, 'exc'),
                         keyboard=tags_menu_keyboard(user_id, 'exc'))
        elif data == 'cfg_inc_add':
            user_states[user_id] = {'action': 'add_include'}
            answer_callback(cb_id)
            send_message(chat_id,
                "➕ *Añadir tag para INCLUIR*\n\n"
                "Envíame el tag que quieres incluir.\nEjemplo: `1girl`\n\n"
                "Puedes usar `/cancel` para cancelar.")
        elif data == 'cfg_exc_add':
            user_states[user_id] = {'action': 'add_exclude'}
            answer_callback(cb_id)
            send_message(chat_id,
                "➖ *Añadir tag para EXCLUIR*\n\n"
                "Envíame el tag que quieres excluir.\nEjemplo: `loli`\n\n"
                "Puedes usar `/cancel` para cancelar.")
        elif data.startswith('cfg_inc_del_'):
            try: idx = int(data.replace('cfg_inc_del_', ''))
            except: idx = -1
            c = get_user_config(user_id)
            if 0 <= idx < len(c['include_tags']):
                removed = c['include_tags'].pop(idx)
                save_async()
                answer_callback(cb_id, f"🗑️ {removed}")
            edit_message(chat_id, msg_id, tags_menu_text(user_id, 'inc'),
                         keyboard=tags_menu_keyboard(user_id, 'inc'))
        elif data.startswith('cfg_exc_del_'):
            try: idx = int(data.replace('cfg_exc_del_', ''))
            except: idx = -1
            c = get_user_config(user_id)
            if 0 <= idx < len(c['exclude_tags']):
                removed = c['exclude_tags'].pop(idx)
                save_async()
                answer_callback(cb_id, f"🗑️ {removed}")
            edit_message(chat_id, msg_id, tags_menu_text(user_id, 'exc'),
                         keyboard=tags_menu_keyboard(user_id, 'exc'))

        elif data.startswith('cfg_ai_'):
            v = data.replace('cfg_ai_', '')
            if v not in AI_MODES:
                v = 'any'
            c = get_user_config(user_id)
            c['ai_mode'] = v
            save_async()
            label = AI_MODE_LABEL.get(v, '🌐 Todos')
            answer_callback(cb_id, f"🤖 {label}")
            edit_message(chat_id, msg_id, ai_menu_text(user_id), keyboard=ai_menu_keyboard(user_id))

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
        except Exception:
            pass

if __name__ == '__main__':
    load_data()
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    app.run(host='0.0.0.0', port=port)