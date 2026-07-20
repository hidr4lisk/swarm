"""
swarm/settings.py — settings mínimas de Hidr4lisk_Swarm.

Filosofía clone & run: todo lo configurable entra por variables de entorno (ver
`.env.example`); los defaults sirven para desarrollo local sin tocar nada.
Single-user: no hay login humano, todo corre con rango `control`.
"""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Single-user en tu máquina: el default inseguro está bien para desarrollo local.
# En cualquier despliegue expuesto, setear SWARM_SECRET_KEY en el entorno.
SECRET_KEY = os.environ.get('SWARM_SECRET_KEY', 'django-insecure-swarm-dev-key-cambiame')

DEBUG = os.environ.get('SWARM_DEBUG', '1') == '1'

ALLOWED_HOSTS = [h for h in os.environ.get('SWARM_ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'enjambre',
]

# Sin login humano, pero sessions+auth quedan para que request.user exista (AnonymousUser)
# y CSRF proteja los POST de la mesa.
# `messages` es el canal de avisos «acción hecha / algo falló» de las vistas que redirigen:
# el aviso se calcula en el POST y se pinta después del redirect (lo muestra base_swarm.html,
# una sola vez). Guardado en la sesión, que ya está montada.
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

ROOT_URLCONF = 'swarm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'enjambre.context_processors.version',
            ],
        },
    },
]

WSGI_APPLICATION = 'swarm.wsgi.application'

# DATABASE_URL (postgres en el compose); sin setear cae a SQLite local para desarrollo.
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    ),
}
# SQLite con escritores concurrentes (en `serve`, worker en un hilo + web en otros): sin WAL ni
# busy_timeout, dos escrituras a la vez terminan en «database is locked». WAL además deja que las
# lecturas (SSE cada 2s) no bloqueen al escritor.
if DATABASES['default']['ENGINE'].endswith('sqlite3'):
    DATABASES['default'].setdefault('OPTIONS', {}).update({
        'timeout': 20,
        'init_command': 'PRAGMA journal_mode=WAL; PRAGMA busy_timeout=20000;',
    })

LANGUAGE_CODE = 'es'
# UI bilingüe: se traduce el CHROME (botones, títulos, la guía Ayuda) vía i18n estándar.
# El CONTENIDO (mensajes de las sillas, lo persistido en DB) no se traduce: lo definen
# las personas de cada silla. El botón ES/EN de la navbar setea la cookie de idioma.
LANGUAGES = [('es', 'Español'), ('en', 'English')]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = os.environ.get('SWARM_TZ', 'America/Argentina/Buenos_Aires')
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Enjambre — el motor de la mesa ──────────────────────────────────────────────

# Carpetas de trabajo persistentes por mesa. Vacío = ~/.enjambre/mesas.
ENJAMBRE_MESAS_DIR = os.environ.get('ENJAMBRE_MESAS_DIR', '')

# Git worktrees aislados de las Tareas. Vacío = ~/.enjambre/workspaces.
ENJAMBRE_WORKSPACES_DIR = os.environ.get('ENJAMBRE_WORKSPACES_DIR', '')

# uid/gid del worker del host. Si el contenedor web corre como root, chownea a estos las
# carpetas/archivos de mesa que crea, para que el worker pueda hacerles git init (si no, quedan
# root:root → 'Permission denied' al fabricar). Override por .env si tu usuario no es 1000.
ENJAMBRE_HOST_UID = int(os.environ.get('ENJAMBRE_HOST_UID', '1000'))
ENJAMBRE_HOST_GID = int(os.environ.get('ENJAMBRE_HOST_GID', '1000'))

# Resolver de rol pluggable: callable(creador) -> 'control' | 'consulta'.
# None (default) = todo es rango `control` — el modelo single-user de Swarm.
ENJAMBRE_ROLE_RESOLVER = None

# Cómo te llaman las sillas en la mesa. Vacío = 'Humano'.
ENJAMBRE_TITULO_HUMANO = os.environ.get('ENJAMBRE_TITULO_HUMANO', '')

# ── Swarm 2.0 — ruta por API key / portátil / toolbelt ──────────────────────────
# Directorio de datos PERSISTENTE (bóveda de API keys + lo que no es la DB). En el pendrive el
# launcher lo apunta a <bundle>/data para que sobreviva a una actualización del app/. Vacío =
# <repo>/data (dev).
SWARM_DATA_DIR = os.environ.get('SWARM_DATA_DIR') or str(BASE_DIR / 'data')

# TOOLBELT: si las sillas por API key pueden operar el SISTEMA REAL (F3). Apagado por default —
# es un tool de mucho poder. Prender con SWARM_TOOLBELT=1 (solo en máquinas que estés autorizado
# a atender). Ver "Threat model" del README.
SWARM_TOOLBELT = os.environ.get('SWARM_TOOLBELT', '')

# base_url del proveedor "OpenAI-compatible" (para apuntar a Groq/DeepSeek/LM Studio/etc.).
# Vacío = api.openai.com. Solo afecta a las sillas api-openai.
SWARM_OPENAI_BASE_URL = os.environ.get('SWARM_OPENAI_BASE_URL', '')
