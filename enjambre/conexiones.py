"""
enjambre/conexiones.py — detección de credenciales de los CLIs (pantalla Conexiones).

Reporta solo EXISTENCIA por archivo: acá jamás se lee, muestra ni loguea el
contenido de una credencial. La app no pide ni guarda credenciales propias: el
login vive en tu terminal (`claude login`, etc.) y el runner lo monta RO.

Dos modos de detección, mismo resultado {cli: bool}:
  · directo — os.path.exists, cuando las rutas son visibles (dev en el host).
  · sonda DooD — dentro del contenedor `worker` las rutas viven en el HOST, así
    que se prueba montarlas en un contenedor efímero sin red que solo corre
    `true`; si el path no existe el daemon corta con error (--mount no crea nada).

El worker persiste el resultado en <data>/conexiones.json al arrancar; la web lo
lee de ahí (no tiene docker.sock) y cae al chequeo directo si no existe (dev).
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from django.utils import timezone

# Qué necesita cada CLI y cómo se loguea (para mostrar en la pantalla).
CLIS = {
    'claude': {
        'nombre': 'Claude Code',
        'env': 'SWARM_CLAUDE_CREDS',
        'default': '~/.claude/.credentials.json',
        'login': 'claude  (y dentro: /login)',
    },
    'opencode': {
        'nombre': 'OpenCode',
        'env': 'SWARM_OPENCODE_CREDS',
        'default': '~/.local/share/opencode/auth.json',
        'login': 'opencode auth login',
    },
    'agy': {
        'nombre': 'Antigravity',
        'env': 'SWARM_AGY_CREDS',
        'default': '~/.gemini/antigravity-cli',
        'login': 'agy  (login OAuth de Google)',
    },
}


def ruta_creds(cli):
    """Ruta configurada (o default) de la credencial del CLI, como string del HOST."""
    return os.environ.get(CLIS[cli]['env']) or str(Path(CLIS[cli]['default']).expanduser())


def ruta_corta(ruta):
    """Versión para MOSTRAR: colapsa el home a `~` (no expone el usuario del host en
    pantalla ni en capturas). Solo display; la detección usa la ruta completa.
    Por regex y no Path.home(): en el worker Docker las rutas son del HOST (/home/x)
    y el home del contenedor es /root — no coincidirían. Cubre Linux y Windows."""
    import re
    return re.sub(r'^(/home/[^/]+|/root|[A-Za-z]:[\\/]Users[\\/][^\\/]+)(?=[\\/]|$)', '~', ruta)


def _sonda_docker(ruta):
    """¿Existe `ruta` en el HOST? Intenta montarla RO en un contenedor efímero sin red
    que solo corre `true`. --mount (a diferencia de -v) no crea paths inexistentes:
    si falta, el daemon devuelve error y eso ES el resultado. No se lee contenido."""
    img = os.environ.get('ENJAMBRE_IMG', 'swarm-runner:latest')
    try:
        r = subprocess.run(
            ['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
             '--mount', f'type=bind,source={ruta},target=/sonda,readonly',
             '--entrypoint', 'true', img],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — sin docker/timeout = no detectado, no rompe
        return False


def detectar(con_sonda=False):
    """{cli: bool} por existencia. `con_sonda=True` solo donde hay docker.sock (worker)."""
    estados = {}
    for cli in CLIS:
        ruta = ruta_creds(cli)
        if os.path.exists(ruta):
            estados[cli] = True
        elif con_sonda and os.environ.get('SWARM_DOOD'):
            estados[cli] = _sonda_docker(ruta)
        else:
            estados[cli] = False
    return estados


# ── Resolución de binarios (doble-clic sin PATH de shell) ────────────────────────
# Los instaladores de los CLIs agregan su dir solo al rc de la shell (.zshrc/.bashrc).
# Lanzado con doble-clic desde el escritorio, ese PATH no viene → which() falla aunque
# el CLI esté instalado y logueado (bug real: Lab 2026-07-19, opencode en
# ~/.opencode/bin agregado únicamente en .zshrc). Fallback: dirs típicos de instalación.
_DIRS_BIN = [
    '~/.opencode/bin',           # instalador oficial de opencode (Linux/mac/Windows)
    '~/.local/bin',              # pipx/uv/instaladores de claude y agy
    '~/.claude/local',           # claude instalado local
    '~/bin',
    '~/AppData/Roaming/npm',     # npm global (Windows)
    '~/.npm-global/bin',
    '/usr/local/bin',
    '/opt/homebrew/bin',
]


def resolver_bin(cli):
    """Ruta del ejecutable del CLI, o None. PATH primero; si no está, los dirs típicos.
    shutil.which(path=…) aplica PATHEXT en Windows (encuentra .cmd/.exe solo)."""
    hit = shutil.which(cli)
    if hit:
        return hit
    extra = os.pathsep.join(str(Path(d).expanduser()) for d in _DIRS_BIN)
    return shutil.which(cli, path=extra)


def archivo_estado():
    """<data>/conexiones.json — al lado de mesas/ para que web y worker lo compartan."""
    from .workspace import mesas_dir
    return Path(mesas_dir()).parent / 'conexiones.json'


def guardar_estado(estados):
    archivo_estado().write_text(json.dumps({
        'detectado': estados,
        'chequeado_at': timezone.now().isoformat(timespec='seconds'),
    }, indent=2))


def leer_estado():
    """Lo que persistió el worker, o None (→ la vista cae al chequeo directo)."""
    try:
        return json.loads(archivo_estado().read_text())
    except (OSError, ValueError):
        return None
