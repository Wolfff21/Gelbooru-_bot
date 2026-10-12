#!/usr/bin/env python3
"""
Bot de Telegram para buscar en Gelbooru - v3
Config por usuario con submenús + soporte imágenes/gifs/vídeos.
"""

import os
import sys
import logging
import json
import uuid
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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== ESTADO EN MEMORIA ====================
user_configs = {}      # user_id -> {'fields': set, 'rating': str, 'media': set}
sessions = {}          # session_id -> {...}
processed_updates = set()

ALL_FIELDS = ['id', 'uploader', 'date', 'size', 'source', 'score', 'rating', 'tags']
DEFAULT_FIELDS = {'id', 'date', 'size', 'score', 'rating', 'tags'}
ALL_MEDIA = ['image', 'gif', 'video']
DEFAULT_MEDIA = set(ALL_MEDIA)

VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.avi', '.mkv')

# ==================== GELBOORU API ====================
class GelbooruAPI:
    def __init__(self):
        self.api_key = os.environ.get("GELBOORU_API_KEY")
        self.user_id = os.environ.get("GELBOORU_USER_ID")
        self.base_url = "https://gelbooru.com/index.php"

    def _proxies(self):
        return {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None

    def _build_params(self, tags, page=1, limit=100):
        params = {
            "page": "dapi", "s": "post", "q": "index", "json": 1,
            "tags": tags, "pid": (page - 1) * limit, "limit": limit
        }
        if self.api_key and self.user_id:
            params["api_key"] = self.api_key
            params["user_id"] = self.user_id
        return params

    def fetch_images(self, tags, page=1, retry_count=0):
        try:
            resp = requests.get(
                self.base_url,
                headers={'User-Agent': USER_AGENT},
                params=self._build_params(tags, page),
                timeout=REQUEST_TIMEOUT,
                proxies=self._proxies()
            )
            if resp.status_code == 429 and retry_count < MAX_RETRIES:
                return self.fetch_images(tags, page, retry_count + 1)
            resp.raise_for_status()
            data = resp.json()
            posts = self._extract_posts(data)
            return self._extract_images(posts) if posts else []
        except Exception as e:
            logger.error(f"❌ fetch_images: {e}")
            return []

    def _extract_posts(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if 'post' in data:
                return data['post']
            if 'posts' in data:
                return data['posts']
            if 'error' in data:
                logger.error(f"API error: {data['error']}")
        return []

    def _extract_images(self, posts):
        images = []
        for post in posts:
            img = {
                'file_url': post.get('file_url'),
                'sample_url': post.get('sample_url'),
                'preview_url': post.get('preview_url'),
                'tags': post.get('tags', ''),
                'id': post.get('id'),
                'score': post.get('score', 0),
                'rating': post.get('rating', ''),
                'width': post.get('width', 0),
                'height': post.get('height', 0),
                'uploader': post.get('uploader') or '',
                'source': post.get('source') or '',
                'created_at': post.get('created_at') or '',
            }
            for k in ['file_url', 'sample_url', 'preview_url']:
                if img[k] and not img[k].startswith('http'):
                    img[k] = urljoin(SITE_BASE_URL, img[k])
            if not img['file_url'] and img['sample_url']:
                img['file_url'] = img['sample_url']
            if img['file_url']:
                images.append(img)
        return images

gelbooru = GelbooruAPI()

# ==================== TIPO DE MEDIA ====================
def get_media_type(url):
    if not url:
        return 'image'
    u = url.lower().split('?')[0]
    if u.endswith('.gif'):
        return 'gif'
    if u.endswith(VIDEO_EXTS):
        return 'video'
    return 'image'

# ==================== CONFIG POR USUARIO ====================
def get_user_config(user_id):
    if user_id not in user_configs:
        user_configs[user_id] = {
            'fields': set(DEFAULT_FIELDS),
            'rating': '',
            'media': set(DEFAULT_MEDIA),
        }
    return user_configs[user_id]

def format_date(s):
    if not s:
        return ''
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s

def format_caption(img, user_id):
    cfg = get_user_config(user_id)
    f = cfg['fields']
    if not f:
        return ""

    lines = []
    if 'id' in f and img.get('id'):
        lines.append(f"🖼️ *ID:* `{img['id']}`")
    if 'uploader' in f:
        lines.append(f"📤 *Uploader:* {img.get('uploader') or 'Anónimo'}")
    if 'date' in f and img.get('created_at'):
        lines.append(f"📅 *Posted:* {format_date(img['created_at'])}")
    if 'size' in f and img.get('width'):
        lines.append(f"📐 *Tamaño:* {img['width']}x{img['height']}")
    if 'source' in f and img.get('source'):
        lines.append(f"🔗 *Source:* {img['source']}")

    sr = []
    if 'score' in f:
        sr.append(f"⭐ {img.get('score', 0)}")
    if 'rating' in f:
        sr.append(f"📊 `{(img.get('rating') or '').upper()}`")
    if sr:
        lines.append("  |  ".join(sr))

    if 'tags' in f and img.get('tags'):
        tags = img['tags']
        if len(tags) > 900:
            tags = tags[:897] + '...'
        lines.append(f"🏷️ `{tags}`")

    return "\n".join(lines)

# ==================== MENÚS DE CONFIGURACIÓN ====================
def main_menu_text(user_id):
    cfg = get_user_config(user_id)
    rd = {'': '🌐 Todos', 'safe': '✅ Safe', 'general': '🟢 General',
          'questionable': '🟡 Questionable', 'explicit': '🔴 Explicit'}.get(cfg['rating'], cfg['rating'])

    media_labels = {'image': '📷', 'gif': '🎞️', 'video': '🎬'}
    media_icons = ' '.join(v for k, v in media_labels.items() if k in cfg['media']) or '❌ ninguna'

    return (
        "⚙️ *Configuración*\n\n"
        "Personaliza tus búsquedas. Los cambios son solo para ti.\n\n"
        f"📋 *Info:* {len(cfg['fields'])}/8 campos activos\n"
        f"🎚️ *Rating:* {rd}\n"
        f"🎬 *Tipo:* {media_icons}\n"
    )

def main_menu_keyboard(user_id):
    cfg = get_user_config(user_id)

    def r_btn(label, val):
        return {'text': ('✅ ' if cfg['rating'] == val else '') + label,
                'callback_data': f'cfg_r_{val or "all"}'}

    return {'inline_keyboard': [
        [{'text': '📋 Información de imagen', 'callback_data': 'cfg_info'}],
        [{'text': '🎬 Tipo de contenido', 'callback_data': 'cfg_media'}],
        [{'text': '🎚️ Filtrar por rating', 'callback_data': 'cfg_rating'}],
    ]}

def info_menu_text(user_id):
    cfg = get_user_config(user_id)
    return (
        "📋 *Información de imagen*\n\n"
        "Activa o desactiva qué datos aparecen en el mensaje.\n\n"
        f"📊 *Activos:* {len(cfg['fields'])}/8"
    )

def info_menu_keyboard(user_id):
    f = get_user_config(user_id)['fields']

    def b(label, key):
        return {'text': ('✅ ' if key in f else '❌ ') + label,
                'callback_data': f'cfg_f_{key}'}

    return {'inline_keyboard': [
        [b('🖼️ ID', 'id'), b('📤 Uploader', 'uploader')],
        [b('📅 Fecha', 'date'), b('📐 Tamaño', 'size')],
        [b('🔗 Source', 'source'), b('⭐ Score', 'score')],
        [b('📊 Rating', 'rating'), b('🏷️ Tags', 'tags')],
        [{'text': '🔕 Sin info (solo media)', 'callback_data': 'cfg_f_none'}],
        [{'text': '✅ Activar todo', 'callback_data': 'cfg_f_all'}],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def media_menu_text(user_id):
    cfg = get_user_config(user_id)
    n = len(cfg['media'])
    return (
        "🎬 *Tipo de contenido*\n\n"
        "Elige qué tipo de archivos quieres recibir.\n"
        "Puedes activar varios a la vez.\n\n"
        f"📊 *Activos:* {n}/3"
    )

def media_menu_keyboard(user_id):
    m = get_user_config(user_id)['media']

    def b(label, key):
        return {'text': ('✅ ' if key in m else '❌ ') + label,
                'callback_data': f'cfg_m_{key}'}

    return {'inline_keyboard': [
        [b('📷 Imágenes', 'image')],
        [b('🎞️ GIFs', 'gif')],
        [b('🎬 Vídeos', 'video')],
        [{'text': '✅ Todos', 'callback_data': 'cfg_m_all'},
         {'text': '🔕 Ninguno', 'callback_data': 'cfg_m_none'}],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

def rating_menu_text(user_id):
    cfg = get_user_config(user_id)
    rd = {'': '🌐 Todos', 'safe': '✅ Safe', 'general': '🟢 General',
          'questionable': '🟡 Questionable', 'explicit': '🔴 Explicit'}.get(cfg['rating'], cfg['rating'])
    return (
        "🎚️ *Filtrar por rating*\n\n"
        "Filtra las imágenes según su clasificación.\n\n"
        f"*Actual:* {rd}\n\n"
        "ℹ️ *Safe* → contenido seguro\n"
        "ℹ️ *General* → todo público\n"
        "ℹ️ *Questionable* → sugerente\n"
        "ℹ️ *Explicit* → contenido adulto"
    )

def rating_menu_keyboard(user_id):
    r = get_user_config(user_id)['rating']

    def b(label, val):
        return {'text': ('✅ ' if r == val else '') + label,
                'callback_data': f'cfg_r_{val or "all"}'}

    return {'inline_keyboard': [
        [b('🌐 Todos', '')],
        [b('✅ Safe', 'safe')],
        [b('🟢 General', 'general')],
        [b('🟡 Questionable', 'questionable')],
        [b('🔴 Explicit', 'explicit')],
        [{'text': '⬅️ Volver', 'callback_data': 'cfg_main'}],
    ]}

# ==================== TELEGRAM HELPERS ====================
def send_message(chat_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'text': text,
                'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)
        r = requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
        return r.json().get('result', {}).get('message_id') if r.ok else None
    except Exception as e:
        logger.error(f"send_message: {e}")
        return None

def edit_message(chat_id, msg_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'message_id': msg_id,
                'text': text, 'parse_mode': 'Markdown'}
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=10)
    except Exception as e:
        logger.error(f"edit_message: {e}")

def answer_callback(cb_id, text=None):
    try:
        data = {'callback_query_id': cb_id}
        if text:
            data['text'] = text
        requests.post(f"{API_URL}/answerCallbackQuery", json=data, timeout=5)
    except Exception as e:
        logger.error(f"answer_callback: {e}")

def send_media(chat_id, url, caption="", keyboard=None):
    """Envía imagen, gif o vídeo según la extensión."""
    mt = get_media_type(url)
    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'image/webp,image/apng,image/*,video/*,*/*;q=0.8',
            'Referer': 'https://gelbooru.com/'
        }
        proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None
        resp = requests.get(url, headers=headers, timeout=90, proxies=proxies)
        if resp.status_code != 200:
            return False

        ct = resp.headers.get('content-type', '').lower()

        if mt == 'video':
            endpoint = 'sendVideo'
            field = 'video'
            fname = 'video.mp4'
            if 'video' not in ct:
                ct = 'video/mp4'
        elif mt == 'gif':
            endpoint = 'sendAnimation'
            field = 'animation'
            fname = 'anim.gif'
            if 'gif' not in ct:
                ct = 'image/gif'
        else:
            endpoint = 'sendPhoto'
            field = 'photo'
            fname = 'image.jpg'
            if 'image' not in ct:
                ct = 'image/jpeg'

        files = {field: (fname, resp.content, ct)}
        data = {'chat_id': chat_id}
        if caption:
            data['caption'] = caption
            data['parse_mode'] = 'Markdown'
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)

        r = requests.post(f"{API_URL}/{endpoint}", files=files, data=data, timeout=90)
        if r.ok:
            return True

        # Fallback: enviar como documento
        files = {'document': (fname, resp.content, ct or 'application/octet-stream')}
        r2 = requests.post(f"{API_URL}/sendDocument", files=files, data=data, timeout=90)
        return r2.ok
    except Exception as e:
        logger.error(f"send_media: {e}")
        return False

# ==================== LÓGICA DE BÚSQUEDA ====================
def build_query(user_id, query):
    cfg = get_user_config(user_id)
    parts = [query]
    if cfg['rating']:
        parts.append(f"rating:{cfg['rating']}")
    return ' '.join(p for p in parts if p).strip()

def fetch_next_image(session):
    images = session['images']
    idx = session['current_index']
    page = session['page']
    sent = session['sent_ids']
    allowed = session['media']

    def ok_media(img):
        return get_media_type(img['file_url']) in allowed

    if not images or idx >= len(images):
        attempts = 0
        new_images = []
        while attempts < 8:
            logger.info(f"🔄 Página {page} de '{session['full_query']}'")
            raw = gelbooru.fetch_images(session['full_query'], page=page)
            new_images = [i for i in raw
                          if i.get('id') not in sent and ok_media(i)]
            if new_images:
                break
            attempts += 1
            page += 1

        if not new_images:
            return None

        session['images'] = new_images
        session['current_index'] = 0
        session['page'] = page
        images = new_images
        idx = 0

    img = images[idx]
    sent.add(img.get('id'))
    session['current_index'] = idx + 1
    return img

def send_next(chat_id, user_id, session_id, session, reply_to_msg_id=None):
    img = fetch_next_image(session)
    if not img:
        send_message(chat_id, f"❌ No se encontraron más resultados para `{session['query']}`")
        return False

    caption = format_caption(img, user_id)
    keyboard = {'inline_keyboard': [[
        {'text': '🔄 Más', 'callback_data': f'more:{session_id}'}
    ]]}

    ok = send_media(chat_id, img['file_url'], caption=caption, keyboard=keyboard)
    if ok and reply_to_msg_id:
        try:
            requests.post(f"{API_URL}/deleteMessage",
                          json={'chat_id': chat_id, 'message_id': reply_to_msg_id},
                          timeout=5)
        except Exception:
            pass
    return ok

def handle_search(chat_id, user_id, query):
    cfg = get_user_config(user_id)
    session_id = uuid.uuid4().hex[:12]
    sessions[session_id] = {
        'query': query,
        'full_query': build_query(user_id, query),
        'page': 1,
        'images': [],
        'current_index': 0,
        'sent_ids': set(),
        'media': set(cfg['media']),   # snapshot
    }
    status_id = send_message(chat_id, f"🔎 Buscando `{query}`...")
    send_next(chat_id, user_id, session_id, sessions[session_id], reply_to_msg_id=status_id)

# ==================== WEBHOOK ====================
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update:
        return 'OK', 200
    uid = update.get('update_id')
    if uid in processed_updates:
        return 'OK', 200
    processed_updates.add(uid)
    if len(processed_updates) > 1000:
        processed_updates.clear()

    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        user_id = msg['from']['id']
        first_name = msg['from'].get('first_name', 'usuario')
        if 'text' not in msg:
            return 'OK', 200
        text = msg['text'].strip()

        # En grupos, limpia mención @bot
        if msg['chat']['type'] in ('group', 'supergroup') and text.startswith('/') and '@' in text.split()[0]:
            text = text.split()[0].split('@')[0] + ' ' + ' '.join(text.split()[1:])
            text = text.strip()

        if text == '/start':
            send_message(
                chat_id,
                f"¡Hola, {first_name}! Este bot envía imágenes del sitio web gelbooru.com por etiquetas.\n\n"
                "Para obtener ayuda sobre las etiquetas, escribe /ayuda.\n\n"
                "¡Solo tienes que introducir las etiquetas en el chat y esperar a que se envíen!",
                keyboard={'inline_keyboard': [[
                    {'text': '⚙️ Configuración', 'callback_data': 'cfg_main'}
                ]]}
            )

        elif text in ('/ayuda', '/help'):
            send_message(chat_id, (
                "📖 *Ayuda — Etiquetas de Gelbooru*\n\n"
                "Gelbooru clasifica las imágenes con *etiquetas (tags)*. "
                "Categorías comunes:\n\n"
                "• *Artista:* `wlop`, `sakimichan`\n"
                "• *Personaje:* `rem_(re:zero)`, `hatsune_miku`\n"
                "• *Copyright:* `genshin_impact`, `blue_archive`\n"
                "• *General:* `1girl`, `solo`, `blue_hair`\n\n"
                "🔹 *Cómo buscar:*\n"
                "Escribe las etiquetas separadas por espacios, o usa `/find <tags>`.\n\n"
                "*Ejemplos:*\n"
                "`rem re:zero`\n"
                "`hatsune_miku blue_hair`\n"
                "`1girl solo`\n\n"
                "💡 *Tips:*\n"
                "• Usa `_` en vez de espacios dentro de una etiqueta\n"
                "• Combina varias etiquetas para precisar\n"
                "• Puedes excluir con `-tag` (ej: `1girl -loli`)\n\n"
                "🔧 *Comandos:*\n"
                "• `/start` — Inicio\n"
                "• `/ayuda` — Esta ayuda\n"
                "• `/find <tags>` — Buscar\n"
                "• `/config` — Tu configuración\n"
            ))

        elif text == '/config':
            send_message(chat_id, main_menu_text(user_id),
                         keyboard=main_menu_keyboard(user_id))

        elif text.startswith('/find'):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send_message(chat_id, "❌ Uso: `/find término`\nEjemplo: `/find rem`")
            else:
                handle_search(chat_id, user_id, parts[1].strip())

        elif text.startswith('/'):
            send_message(chat_id, "❓ Comando no reconocido. Usa /ayuda")

        else:
            handle_search(chat_id, user_id, text)

    # ---------- CALLBACKS ----------
    elif 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        user_id = cb['from']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        cb_id = cb['id']

        # Botón "Más"
        if data.startswith('more:'):
            session_id = data.split(':', 1)[1]
            session = sessions.get(session_id)
            if not session:
                answer_callback(cb_id, "❌ Sesión expirada")
                return 'OK', 200
            answer_callback(cb_id, "🔄 Buscando...")
            send_next(chat_id, user_id, session_id, session, reply_to_msg_id=msg_id)

        # ---- Navegación de menús ----
        elif data == 'cfg_main':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, main_menu_text(user_id),
                         keyboard=main_menu_keyboard(user_id))

        elif data == 'cfg_info':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, info_menu_text(user_id),
                         keyboard=info_menu_keyboard(user_id))

        elif data == 'cfg_media':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, media_menu_text(user_id),
                         keyboard=media_menu_keyboard(user_id))

        elif data == 'cfg_rating':
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, rating_menu_text(user_id),
                         keyboard=rating_menu_keyboard(user_id))

        # ---- Toggle de campos ----
        elif data.startswith('cfg_f_'):
            key = data.replace('cfg_f_', '')
            cfg = get_user_config(user_id)
            if key == 'none':
                cfg['fields'] = set()
                answer_callback(cb_id, "🔕 Info desactivada")
            elif key == 'all':
                cfg['fields'] = set(ALL_FIELDS)
                answer_callback(cb_id, "✅ Toda la info activada")
            elif key in ALL_FIELDS:
                if key in cfg['fields']:
                    cfg['fields'].discard(key)
                else:
                    cfg['fields'].add(key)
                answer_callback(cb_id)
            user_configs[user_id] = cfg
            edit_message(chat_id, msg_id, info_menu_text(user_id),
                         keyboard=info_menu_keyboard(user_id))

        # ---- Toggle de media ----
        elif data.startswith('cfg_m_'):
            key = data.replace('cfg_m_', '')
            cfg = get_user_config(user_id)
            if key == 'all':
                cfg['media'] = set(ALL_MEDIA)
                answer_callback(cb_id, "✅ Todos los tipos")
            elif key == 'none':
                cfg['media'] = set()
                answer_callback(cb_id, "🔕 Ningún tipo activo")
            elif key in ALL_MEDIA:
                if key in cfg['media']:
                    cfg['media'].discard(key)
                else:
                    cfg['media'].add(key)
                answer_callback(cb_id)
            user_configs[user_id] = cfg
            edit_message(chat_id, msg_id, media_menu_text(user_id),
                         keyboard=media_menu_keyboard(user_id))

        # ---- Rating ----
        elif data.startswith('cfg_r_'):
            val = data.replace('cfg_r_', '')
            if val == 'all':
                val = ''
            cfg = get_user_config(user_id)
            cfg['rating'] = val
            user_configs[user_id] = cfg
            answer_callback(cb_id, f"🎚️ Rating: {val or 'todos'}")
            edit_message(chat_id, msg_id, rating_menu_text(user_id),
                         keyboard=rating_menu_keyboard(user_id))

    return 'OK', 200

@app.route('/')
def index():
    return "Bot activo", 200

# ==================== WEBHOOK SETUP ====================
_setup_done = False

def setup_webhook():
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.warning("⚠️ RENDER_EXTERNAL_URL no definida")
        return
    webhook_url = f"{external_url}/webhook/{BOT_TOKEN}"
    try:
        r = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_url})
        logger.info(f"✅ Webhook: {webhook_url}" if r.ok else f"❌ {r.text}")
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
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    app.run(host='0.0.0.0', port=port)
         
