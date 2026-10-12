#!/usr/bin/env python3
"""
Bot de Telegram para buscar imágenes en Gelbooru
"""

import os
import sys
import logging
import json
from urllib.parse import urljoin

from flask import Flask, request
import requests

# ==================== CONFIGURACIÓN ====================
SITE_BASE_URL = "https://gelbooru.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()  # Proxy predeterminado opcional
# =======================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("❌ Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Estado en memoria
user_data = {}          # chat_id -> {query, page, images, current_index, sent_ids, filter_snapshot}
processed_updates = set()
admin_state = {}        # user_id -> {'action': 'editing_tags'}

# ==================== FILTROS GLOBALES ====================
global_filters = {
    'rating': '',       # '' | 'safe' | 'general' | 'questionable' | 'explicit'
    'extra_tags': ''    # tags que se añaden a TODAS las búsquedas
}

def apply_filters(query):
    parts = []
    if query:
        parts.append(query)
    if global_filters['extra_tags']:
        parts.append(global_filters['extra_tags'])
    if global_filters['rating']:
        parts.append(f"rating:{global_filters['rating']}")
    return ' '.join(parts).strip()

def filter_snapshot():
    return (global_filters['rating'], global_filters['extra_tags'])

# ==================== MÓDULO GELBOORU ====================
class GelbooruAPI:
    def __init__(self):
        self.api_key = os.environ.get("GELBOORU_API_KEY")
        self.user_id = os.environ.get("GELBOORU_USER_ID")
        self.base_url = "https://gelbooru.com/index.php"

    def _proxies(self):
        if PROXY_URL:
            return {'http': PROXY_URL, 'https': PROXY_URL}
        return None

    def _build_params(self, tags, page=1, limit=100):
        offset = (page - 1) * limit
        params = {
            "page": "dapi",
            "s": "post",
            "q": "index",
            "json": 1,
            "tags": tags,
            "pid": offset,
            "limit": limit
        }
        if self.api_key and self.user_id:
            params["api_key"] = self.api_key
            params["user_id"] = self.user_id
        return params

    def fetch_images(self, tags, page=1, retry_count=0):
        params = self._build_params(tags, page)
        try:
            headers = {'User-Agent': USER_AGENT}
            resp = requests.get(
                self.base_url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
                proxies=self._proxies()
            )
            if resp.status_code == 429 and retry_count < MAX_RETRIES:
                return self.fetch_images(tags, page, retry_count + 1)
            resp.raise_for_status()
            data = resp.json()
            posts = self._extract_posts(data)
            if not posts:
                return []
            return self._extract_images(posts)
        except Exception as e:
            logger.error(f"❌ Error en fetch_images: {e}")
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
                logger.error(f"❌ Error de API: {data['error']}")
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

# ==================== FUNCIONES TELEGRAM ====================
def send_message(chat_id, text, keyboard=None):
    try:
        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True
        }
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)
        r = requests.post(f"{API_URL}/sendMessage", json=data, timeout=10)
        if r.ok:
            return r.json().get('result', {}).get('message_id')
    except Exception as e:
        logger.error(f"❌ send_message: {e}")
    return None

def edit_message(chat_id, msg_id, text, keyboard=None):
    try:
        data = {
            'chat_id': chat_id,
            'message_id': msg_id,
            'text': text,
            'parse_mode': 'Markdown'
        }
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=10)
    except Exception as e:
        logger.error(f"❌ edit_message: {e}")

def answer_callback(callback_id, text=None):
    try:
        data = {'callback_query_id': callback_id}
        if text:
            data['text'] = text
        requests.post(f"{API_URL}/answerCallbackQuery", json=data, timeout=5)
    except Exception as e:
        logger.error(f"❌ answer_callback: {e}")

def send_photo(chat_id, photo_url, caption="", keyboard=None):
    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': 'https://gelbooru.com/'
        }
        proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None
        response = requests.get(photo_url, headers=headers, timeout=30, proxies=proxies)
        if response.status_code != 200:
            return False

        content_type = response.headers.get('content-type', 'image/jpeg')
        if 'image' not in content_type:
            content_type = 'image/jpeg'

        ext = 'jpg'
        if 'png' in content_type:
            ext = 'png'
        elif 'gif' in content_type:
            ext = 'gif'
        elif 'webp' in content_type:
            ext = 'webp'

        files = {'photo': (f'image.{ext}', response.content, content_type)}
        data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'Markdown'}
        if keyboard:
            data['reply_markup'] = json.dumps(keyboard)

        r = requests.post(f"{API_URL}/sendPhoto", files=files, data=data, timeout=30)
        if r.ok:
            return True

        # Fallback: enviar como documento
        files = {'document': (f'image.{ext}', response.content, content_type)}
        r2 = requests.post(f"{API_URL}/sendDocument", files=files, data=data, timeout=30)
        return r2.ok
    except Exception as e:
        logger.error(f"❌ send_photo: {e}")
        return False

# ==================== UTILIDADES ====================
def is_admin(user_id):
    return str(user_id) == ADMIN_USER_ID

def format_image_caption(img_data):
    tags = img_data.get('tags', '')
    if len(tags) > 900:
        tags = tags[:897] + '...'
    return f"🏷️ `{tags}`"

def format_filters():
    rating_display = {
        '': '🌐 Todos',
        'safe': '✅ Safe',
        'general': '🟢 General',
        'questionable': '🟡 Questionable',
        'explicit': '🔴 Explicit'
    }.get(global_filters['rating'], global_filters['rating'])

    text = "⚙️ *Configuración de filtros*\n\n"
    text += f"🎚️ *Rating:* {rating_display}\n"
    text += f"🏷️ *Tags extra:* `{global_filters['extra_tags'] or 'ninguno'}`\n\n"
    text += "_Estos filtros se aplican a las búsquedas de todos los usuarios._"
    return text

def get_filter_keyboard():
    return {
        'inline_keyboard': [
            [
                {'text': '🌐 Todos', 'callback_data': 'filter_rating_all'},
                {'text': '✅ Safe', 'callback_data': 'filter_rating_safe'},
            ],
            [
                {'text': '🟢 General', 'callback_data': 'filter_rating_general'},
                {'text': '🟡 Questionable', 'callback_data': 'filter_rating_questionable'},
            ],
            [
                {'text': '🔴 Explicit', 'callback_data': 'filter_rating_explicit'},
            ],
            [
                {'text': '✏️ Editar tags extra', 'callback_data': 'filter_edit_tags'},
            ],
        ]
    }

# ==================== LÓGICA DE BÚSQUEDA ====================
def find_image(chat_id, query):
    """
    Devuelve la siguiente imagen no enviada para la búsqueda actual.
    Sin cache, sin esperas. Paginación automática.
    """
    full_query = apply_filters(query)
    snapshot = filter_snapshot()

    user = user_data.get(chat_id)
    # Si cambió la query o los filtros → reiniciar sesión
    if (not user or user.get('query') != query
            or user.get('filter_snapshot') != snapshot):
        user = {
            'query': query,
            'filter_snapshot': snapshot,
            'page': 1,
            'images': [],
            'current_index': 0,
            'sent_ids': set()
        }
        user_data[chat_id] = user

    images = user.get('images', [])
    current_index = user.get('current_index', 0)
    page = user.get('page', 1)
    sent_ids = user.get('sent_ids', set())

    # Si no quedan imágenes pendientes, cargar siguiente página
    if not images or current_index >= len(images):
        attempts = 0
        new_images = []
        while attempts < 5:
            logger.info(f"🔄 Página {page} para '{full_query}'")
            raw = gelbooru.fetch_images(full_query, page=page)
            new_images = [i for i in raw if i.get('id') not in sent_ids]
            if new_images:
                break
            attempts += 1
            page += 1

        if not new_images:
            return None, f"❌ No se encontraron más imágenes para `{query}`"

        user['images'] = new_images
        user['current_index'] = 0
        user['page'] = page
        user_data[chat_id] = user
        images = new_images
        current_index = 0

    img_data = images[current_index]
    sent_ids.add(img_data.get('id'))
    user['current_index'] = current_index + 1
    user['sent_ids'] = sent_ids
    user_data[chat_id] = user

    return img_data, None

def handle_search(chat_id, query):
    """Busca y envía la primera imagen para la query dada."""
    status_id = send_message(chat_id, f"🔎 Buscando `{query}`...")
    img_data, error = find_image(chat_id, query)

    if error:
        if status_id:
            edit_message(chat_id, status_id, error)
        else:
            send_message(chat_id, error)
        return

    caption = format_image_caption(img_data)
    keyboard = {
        'inline_keyboard': [[{'text': '🔄 Más', 'callback_data': 'more'}]]
    }
    ok = send_photo(chat_id, img_data['file_url'], caption=caption, keyboard=keyboard)

    if status_id:
        if ok:
            try:
                requests.post(f"{API_URL}/deleteMessage",
                              json={'chat_id': chat_id, 'message_id': status_id},
                              timeout=5)
            except Exception:
                pass
        else:
            edit_message(chat_id, status_id, "❌ Error al enviar la imagen.")

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

    # ---------- MENSAJES ----------
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        user_id = msg['from']['id']
        first_name = msg['from'].get('first_name', 'usuario')

        if 'text' not in msg:
            return 'OK', 200

        text = msg['text'].strip()

        # Admin editando tags extra
        if user_id in admin_state and admin_state[user_id].get('action') == 'editing_tags':
            if text == '/cancel':
                del admin_state[user_id]
                send_message(chat_id, "❌ Edición cancelada")
            else:
                global_filters['extra_tags'] = text
                user_data.clear()  # forzar re-búsqueda con nuevos filtros
                del admin_state[user_id]
                send_message(chat_id, f"✅ Tags extra actualizados: `{text}`")
            return 'OK', 200

        # -------- COMANDOS --------
        if text == '/start':
            rows = [[{'text': '⚙️ Configuración', 'callback_data': 'config'}]] if is_admin(user_id) else []
            send_message(
                chat_id,
                f"¡Hola, {first_name}! Este bot envía imágenes del sitio web gelbooru.com por etiquetas.\n\n"
                "Para obtener ayuda sobre las etiquetas, escribe /ayuda.\n\n"
                "¡Solo tienes que introducir las etiquetas en el chat y esperar a que se envíen!",
                keyboard={'inline_keyboard': rows} if rows else None
            )

        elif text in ('/ayuda', '/help'):
            ayuda = (
                "📖 *Ayuda — Etiquetas de Gelbooru*\n\n"
                "Gelbooru clasifica las imágenes con *etiquetas (tags)*. "
                "Categorías comunes:\n\n"
                "• *Artista:* `wlop`, `sakimichan`\n"
                "• *Personaje:* `rem_(re:zero)`, `hatsune_miku`\n"
                "• *Copyright:* `genshin_impact`, `blue_archive`\n"
                "• *General:* `1girl`, `solo`, `blue_hair`\n"
                "• *Meta:* info técnica\n\n"
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
            )
            if is_admin(user_id):
                ayuda += "• `/config` — Filtros globales (admin)\n"
            send_message(chat_id, ayuda)

        elif text == '/config':
            if not is_admin(user_id):
                send_message(chat_id, "❌ No tienes permiso.")
            else:
                send_message(chat_id, format_filters(), keyboard=get_filter_keyboard())

        elif text.startswith('/find'):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send_message(chat_id, "❌ Uso: `/find término`\nEjemplo: `/find rem`")
            else:
                handle_search(chat_id, parts[1].strip())

        elif text.startswith('/'):
            send_message(chat_id, "❓ Comando no reconocido. Usa /ayuda")

        else:
            # Texto libre → tratado como tags
            handle_search(chat_id, text)

    # ---------- CALLBACKS ----------
    elif 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        user_id = cb['from']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        cb_id = cb['id']

        # Botón "Más"
        if data == 'more':
            if chat_id not in user_data or not user_data[chat_id].get('query'):
                answer_callback(cb_id, "❌ No hay búsqueda activa")
                return 'OK', 200

            query = user_data[chat_id]['query']
            answer_callback(cb_id, "🔄 Buscando...")

            img_data, error = find_image(chat_id, query)
            if error:
                send_message(chat_id, error)
                return 'OK', 200

            caption = format_image_caption(img_data)
            keyboard = {
                'inline_keyboard': [[{'text': '🔄 Más', 'callback_data': 'more'}]]
            }
            ok = send_photo(chat_id, img_data['file_url'], caption=caption, keyboard=keyboard)
            if ok:
                try:
                    requests.post(f"{API_URL}/deleteMessage",
                                  json={'chat_id': chat_id, 'message_id': msg_id},
                                  timeout=5)
                except Exception:
                    pass
            else:
                send_message(chat_id, "❌ Error al enviar la imagen.")

        # Botón "Configuración"
        elif data == 'config':
            if not is_admin(user_id):
                answer_callback(cb_id, "❌ Solo admin")
                return 'OK', 200
            answer_callback(cb_id)
            edit_message(chat_id, msg_id, format_filters(), keyboard=get_filter_keyboard())

        # Cambio de rating
        elif data.startswith('filter_rating_'):
            if not is_admin(user_id):
                answer_callback(cb_id, "❌ Solo admin")
                return 'OK', 200
            rating = data.replace('filter_rating_', '')
            if rating == 'all':
                rating = ''
            global_filters['rating'] = rating
            user_data.clear()  # limpiar sesiones para que apliquen los filtros
            answer_callback(cb_id, f"✅ Rating: {rating or 'todos'}")
            edit_message(chat_id, msg_id, format_filters(), keyboard=get_filter_keyboard())

        # Editar tags extra
        elif data == 'filter_edit_tags':
            if not is_admin(user_id):
                answer_callback(cb_id, "❌ Solo admin")
                return 'OK', 200
            admin_state[user_id] = {'action': 'editing_tags'}
            answer_callback(cb_id)
            send_message(
                chat_id,
                "✏️ *Envía los tags extra* que se añadirán a TODAS las búsquedas.\n"
                "Ejemplo: `-ai_generated -watermark`\n\n"
                "Envía `/cancel` para cancelar."
            )

    return 'OK', 200

@app.route('/')
def index():
    return "Bot activo", 200

# ==================== SETUP WEBHOOK ====================
_setup_done = False

def setup_webhook():
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        logger.warning("⚠️ RENDER_EXTERNAL_URL no definida, no se registra webhook")
        return
    webhook_url = f"{external_url}/webhook/{BOT_TOKEN}"
    try:
        r = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_url})
        if r.ok:
            logger.info(f"✅ Webhook registrado: {webhook_url}")
        else:
            logger.error(f"❌ Error webhook: {r.text}")
    except Exception as e:
        logger.error(f"❌ Excepción webhook: {e}")

@app.before_request
def _ensure_webhook():
    global _setup_done
    if not _setup_done:
        _setup_done = True
        try:
            setup_webhook()
        except Exception as e:
            logger.error(f"❌ setup_webhook: {e}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    app.run(host='0.0.0.0', port=port)
