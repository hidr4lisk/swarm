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
    'django.contrib.staticfiles',
    'enjambre',
]

# Sin login humano, pero sessions+auth quedan para que request.user exista (AnonymousUser)
# y CSRF proteja los POST de la mesa.
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
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

# Wrapper de dispatch de los CLIs. Si está seteada, el motor antepone este runner
# (contenedor descartable); vacía = llamada directa al CLI en PATH del worker.
ENJAMBRE_RUNNER = os.environ.get('ENJAMBRE_RUNNER', '')

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
