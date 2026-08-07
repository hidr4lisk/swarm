"""
enjambre/toolbelt.py — herramientas de las sillas sobre el SISTEMA REAL (F3).

El cambio de paradigma de Swarm 2.0: las sillas NO trabajan encapsuladas en la carpeta git de la
mesa — operan la máquina a la que enchufás el pendrive (soporte en la PC del cliente). No hay
sandbox.

UN SOLO TOOLBELT, UN SOLO PERMISO: **el switch es el candado**. Con el toolbelt encendido, tanto
las sillas por API key como las CLI leen, editan archivos, tocan configuración y construyen, y
todo se aplica EN EL MOMENTO. Apagado, las sillas solo responden texto. No hay un segundo modo ni
un gate por comando: prenderlo ES el permiso.

  · LECTURA (system_report / inspect / read_file / list_dir): `inspect` corre SIN shell, con
    **allowlist** de binarios read-only + denylist de banderas que escriben (find -delete/-exec).
    La allowlist sigue existiendo para que una lectura no mute por accidente: lo que muta se pide
    explícito, por la herramienta que corresponde.
  · MUTACIÓN (write_file / apply_fix): se ejecutan solas y quedan en la **bitácora** (modelo
    Acción) como EJECUTADA, más un aviso en la mesa por SSE. Sin gate previo, esa visibilidad en
    vivo es la red que queda: el humano ve el cambio pasar mientras pasa.
  · La bóveda de API keys (`secrets.enc`, `.secrets.runtime.json`) no se lee NI se escribe con el
    toolbelt — sería el escape barato (volcar keys al transcript, o borrarlas de un sobrescritazo).

Opt-in: apagado salvo `SWARM_TOOLBELT` (setting/env). Un tool de mucho poder → arranca off.
OS-aware por `platform.system()`. Ver el "Threat model" ampliado del README.
"""
import contextvars
import os
import platform
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.utils import timezone

IS_WIN = platform.system() == 'Windows'
SHELL_NAME = 'cmd.exe' if IS_WIN else (os.environ.get('SHELL') or '/bin/sh')

MAX_OUT = 8000       # tope de caracteres de salida devuelta a la silla (y guardada)
MAX_FILE = 40000     # tope de bytes al leer un archivo
MAX_ROUNDS = 8       # tope de rondas de tool-use por turno (safety anti-loop / costo)
INSPECT_TIMEOUT = 30
APPLY_TIMEOUT = 120


def _env():
    """Environment de los comandos del toolbelt. Igual que el de las sillas CLI: si la silla
    corre `git` dentro de la carpeta de la mesa (que vive en el pendrive, exFAT, sin dueño), sin
    esto se planta con «dubious ownership». Ver `workspace.env_git_safe`."""
    from .workspace import env_git_safe
    return env_git_safe(os.environ.copy())


def _decodificar(b):
    """Bytes de un comando → texto. **UTF-8 primero**, y si no decodifica, la codificación de la
    consola del SO.

    Acá NO se puede fijar una sola: el toolbelt corre lo que la silla pida y en Windows conviven
    las dos familias — un CLI moderno (git, node, python) escupe UTF-8, mientras que `dir` o
    `ipconfig` salen en la codepage OEM. `text=True` a secas decodificaba SIEMPRE con la del SO
    (cp1252 en Windows) y convertía en mojibake toda salida UTF-8 con acentos. El orden importa:
    texto cp850/cp1252 con acentos casi nunca es UTF-8 válido, así que un decode estricto de
    UTF-8 es un test confiable de cuál de las dos es."""
    if not b:
        return ''
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        import locale
        return b.decode(locale.getpreferredencoding(False), errors='replace')

# ── Allowlist read-only (gate principal de `inspect`): binarios que NO mutan el sistema ──
_ALLOW_POSIX = {
    'ls', 'cat', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'zgrep', 'find', 'stat', 'file',
    'wc', 'sort', 'uniq', 'cut', 'tr', 'nl', 'tac', 'rev', 'du', 'df', 'free', 'uptime',
    'uname', 'hostname', 'whoami', 'id', 'groups', 'ps', 'pgrep', 'env', 'printenv', 'date',
    'cal', 'which', 'whereis', 'type', 'pwd', 'realpath', 'readlink', 'basename', 'dirname',
    'lsblk', 'lscpu', 'lsusb', 'lspci', 'lsmod', 'blkid', 'journalctl', 'dmesg', 'ss', 'netstat',
    'getent', 'host', 'dig', 'nslookup', 'arp', 'w', 'who', 'last', 'vmstat', 'iostat', 'mpstat',
    'tree', 'jq', 'xxd', 'od', 'strings', 'sha256sum', 'md5sum', 'cksum', 'diff', 'comm',
}
_ALLOW_WIN = {
    'dir', 'type', 'findstr', 'where', 'tasklist', 'systeminfo', 'ipconfig', 'netstat', 'whoami',
    'hostname', 'ver', 'vol', 'tree', 'set', 'path', 'fc', 'comp', 'net',  # `net` read-only por convención (net view/user); mutaciones caen en apply_fix
}
_ALLOW = _ALLOW_WIN if IS_WIN else _ALLOW_POSIX

# Banderas que convierten un binario read-only en escritor (misuse de la allowlist, ej. `find`).
_DENY_FLAGS = ('-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fls', '-fprintf')
# Operadores de shell: en Windows `cmd /c` los interpreta → los bloqueamos (en POSIX corremos
# shell=False, así que son inertes y NO se bloquean, para no romper regex con `|`).
_DENY_OPS_WIN = ('&', '|', '>', '<', '^')

# Archivos de la bóveda: las herramientas del toolbelt NUNCA los leen. `.secrets.runtime.json`
# tiene las API keys EN CLARO mientras la bóveda está desbloqueada — sin este freno, una silla
# (o un prompt injection en un archivo que lee) podría volcarlas al transcript de la mesa.
# No es un sandbox (mismo usuario), pero cierra el escape accidental y el vector barato.
_SECRETOS_VAULT = ('secrets.enc', '.secrets.runtime.json')

# Credenciales del SISTEMA (no de Swarm): la bóveda no es el único secreto de la máquina. Una
# silla que lee `~/.ssh/id_rsa` o `/etc/shadow` lo vuelca al transcript de la mesa igual que
# volcaría una API key. Se chequea por CONTENCIÓN de path (no por nombre): los que arrancan con
# `/` son rutas absolutas; el resto son carpetas que pueden estar en cualquier HOME.
_RUTAS_VEDADAS = ('.ssh', '.gnupg', '.aws', '.docker', '.kube',
                  '/etc/shadow', '/etc/gshadow', '/etc/sudoers')


def _es_ruta_vedada(path):
    """True si `path` (resuelto) es un archivo de la bóveda o cae dentro de una ruta vedada
    del sistema. Antes comparaba SOLO el basename: `~/.ssh/id_rsa` pasaba libre y cualquier
    archivo llamado `secrets.enc` quedaba bloqueado sin importar dónde estuviera."""
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
    except (OSError, ValueError):
        return False
    if p.name in _SECRETOS_VAULT:
        return True
    partes = p.parts
    for vedada in _RUTAS_VEDADAS:
        if vedada.startswith('/'):
            try:
                if p == Path(vedada) or p.is_relative_to(Path(vedada)):
                    return True
            except (OSError, ValueError):
                continue
        elif vedada in partes:
            return True
    return False


def _menciona_secreto(texto):
    return any(s in (texto or '').lower() for s in _SECRETOS_VAULT)


# ── Carpeta de trabajo del turno (prolijidad por default, no jaula) ───────────────
# Regla de la casa: si el humano NO nombró una carpeta, lo que las sillas produzcan aterriza en la
# carpeta de la mesa — así el trabajo queda junto, versionado y descargable. Si nombró una ruta,
# ese turno corre libre (`carpeta_turno` queda en None) y las sillas escriben donde les pidieron.
#
# NO es contención dura: una silla CLI trae su propia shell y puede escribir en cualquier lado (ver
# el README, §Threat model). Acá se acota lo que SÍ pasa por nuestras manos — las herramientas de
# las sillas por API key — y se les dice a las CLI dónde dejar el trabajo.
_carpeta_turno = contextvars.ContextVar('swarm_carpeta_turno', default=None)


def carpeta_turno():
    """Carpeta de trabajo del turno en curso, o None si el turno corre libre."""
    return _carpeta_turno.get()


@contextmanager
def usar_carpeta(ruta):
    """Fija la carpeta de trabajo mientras dura el turno (la lee `ejecutar_tool`). `ruta` None
    = turno libre. ContextVar: no se pisa entre hilos (worker vs requests de la web)."""
    token = _carpeta_turno.set(str(ruta) if ruta else None)
    try:
        yield
    finally:
        _carpeta_turno.reset(token)


def _dentro_de(path, raiz):
    """True si `path` cae dentro de `raiz` (ambos resueltos)."""
    try:
        return Path(os.path.expanduser(str(path))).resolve().is_relative_to(Path(raiz).resolve())
    except (OSError, ValueError):
        return False


def _fuera_de_la_mesa(ruta):
    """Mensaje de rechazo si hay carpeta de turno y `ruta` queda afuera; '' si está todo bien."""
    raiz = carpeta_turno()
    if not raiz or _dentro_de(ruta, raiz):
        return ''
    return (f'(⚠️ fuera de la carpeta de esta mesa. Escribí bajo {raiz} para que el trabajo quede '
            f'junto y versionado. Si el humano te pidió EXPRESAMENTE esa otra ruta, decíselo y '
            f'esperá que te lo confirme en la mesa.)')


# ── Estado ──────────────────────────────────────────────────────────────────────
# El toggle de la UI persiste como un flag en el data dir (el pendrive): el usuario NO edita el
# launcher. Se lee EN VIVO en cada turno → prender/apagar tiene efecto al instante, sin reiniciar
# (web y worker comparten proceso en `serve`; y aunque no lo compartieran, ambos leen el mismo flag).
def _flag_path():
    return Path(getattr(settings, 'SWARM_DATA_DIR', Path(settings.BASE_DIR) / 'data')) / '.toolbelt_on'


def forzado_por_env():
    """True si el toolbelt está forzado ON por setting/entorno (SWARM_TOOLBELT). Es el override
    avanzado del launcher: cuando está, la UI muestra el toggle bloqueado (se controla desde ahí)."""
    return bool(getattr(settings, 'SWARM_TOOLBELT', False) or os.environ.get('SWARM_TOOLBELT'))


def habilitado():
    """El toolbelt está OFF por default. Se prende desde la interfaz (toggle → flag en el data dir)
    o forzado por SWARM_TOOLBELT (setting/env, override avanzado)."""
    try:
        return forzado_por_env() or _flag_path().exists()
    except OSError:
        return forzado_por_env()


def set_habilitado(on):
    """Prende/apaga el toolbelt desde la UI (persistente en el data dir). Devuelve el estado real.
    Si está forzado por el entorno, no se puede apagar desde acá → queda ON."""
    p = _flag_path()
    try:
        if on:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('1')
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return habilitado()


# ── Esquemas de herramientas (formato Anthropic) + conversión a OpenAI ────────────
def tools_anthropic():
    return [
        {'name': 'system_report', 'description':
         'Snapshot read-only de ESTA máquina: SO, CPU, RAM, disco, uptime y procesos que más '
         'consumen. Sin argumentos. Se ejecuta al instante.',
         'input_schema': {'type': 'object', 'properties': {}}},
        {'name': 'inspect', 'description':
         'Corre un comando de SOLO LECTURA en esta máquina y devuelve su salida (allowlist de '
         'binarios read-only; sin pipes ni redirecciones). Para diagnosticar: ps, df, grep, ss, '
         'journalctl, etc. Se ejecuta SOLO (no pide permiso). Para CAMBIAR algo, usá apply_fix.',
         'input_schema': {'type': 'object', 'properties': {
             'comando': {'type': 'string', 'description': 'Ej: "ps aux", "df -h", "journalctl -u nginx -n 50".'}},
             'required': ['comando']}},
        {'name': 'read_file', 'description':
         f'Lee el contenido de un archivo del sistema, hasta {MAX_FILE} bytes por llamada. '
         'Read-only, auto. Si el archivo es más grande, la salida termina diciéndote en qué byte '
         'cortó: volvé a llamar con «desde» en ese número para seguir leyendo (y repetí hasta el '
         'final antes de sacar conclusiones sobre un archivo largo).',
         'input_schema': {'type': 'object', 'properties': {
             'ruta': {'type': 'string', 'description': 'Ruta absoluta o ~ del archivo.'},
             'desde': {'type': 'integer', 'description':
                       'Byte desde el que empezar a leer (default 0). Para continuar una lectura cortada.'}},
             'required': ['ruta']}},
        {'name': 'list_dir', 'description':
         'Lista el contenido de un directorio del sistema. Read-only, auto.',
         'input_schema': {'type': 'object', 'properties': {
             'ruta': {'type': 'string', 'description': 'Ruta del directorio.'}},
             'required': ['ruta']}},
        {'name': 'apply_fix', 'description':
         'Ejecuta un comando que MODIFICA el sistema (instalar, editar, reiniciar un servicio, etc.). '
         'CORRE EN EL MOMENTO sobre la máquina real — no hay aprobación previa ni deshacer. Queda '
         'registrado en la Bitácora con su motivo. Corré primero inspect/read_file para justificarlo, '
         'un cambio por vez, y NO toques nada que no te hayan pedido.',
         'input_schema': {'type': 'object', 'properties': {
             'comando': {'type': 'string', 'description': 'El comando exacto a ejecutar.'},
             'motivo': {'type': 'string', 'description': 'Por qué hace falta y qué esperás que logre.'}},
             'required': ['comando', 'motivo']}},
        {'name': 'write_file', 'description':
         'Escribe (crea o sobrescribe) un archivo con el contenido dado. Se aplica EN EL MOMENTO. '
         'Para editar un archivo existente, leelo antes con read_file y devolvé el contenido completo '
         'ya modificado — esta herramienta reemplaza el archivo entero, no parchea.',
         'input_schema': {'type': 'object', 'properties': {
             'ruta': {'type': 'string', 'description': 'Ruta absoluta o ~ del archivo a escribir.'},
             'contenido': {'type': 'string', 'description': 'Contenido completo final del archivo.'},
             'motivo': {'type': 'string', 'description': 'Qué estás haciendo y por qué.'}},
             'required': ['ruta', 'contenido']}},
    ]


def tools_openai():
    """Convierte los esquemas Anthropic al formato de function-calling de OpenAI."""
    return [{'type': 'function', 'function': {
        'name': t['name'], 'description': t['description'], 'parameters': t['input_schema'],
    }} for t in tools_anthropic()]


def system_prompt():
    """Encuadre OS-aware que se pasa como `system` a la API: qué máquina es y las reglas del juego."""
    return (
        f"Sos una silla de SOPORTE del Enjambre operando DIRECTAMENTE sobre esta máquina real "
        f"(no un sandbox). Sistema: {platform.platform()} · shell: {SHELL_NAME} · host: {platform.node()}.\n"
        "Tenés herramientas para operarla:\n"
        "• system_report / inspect / read_file / list_dir → SOLO LECTURA, se ejecutan solas. Usalas "
        "libremente para diagnosticar antes de opinar. NO inventes salidas: si algo falla, decilo.\n"
        "• write_file → crear o reescribir un archivo. Reemplaza el archivo ENTERO: si vas a editar "
        "uno que existe, leelo primero con read_file y devolvé el contenido completo ya modificado.\n"
        "• apply_fix → cualquier otro cambio (instalar, mover, reiniciar un servicio, borrar).\n"
        "Las dos últimas se aplican EN EL MOMENTO sobre la máquina real: no hay aprobación previa "
        "ni deshacer. Por eso trabajá como un sysadmin prudente: mirá ANTES de tocar, un cambio por "
        "vez, justificá cada uno con lo que viste en las lecturas, y NO toques nada que no te hayan "
        "pedido. Nunca borres ni sobrescribas de forma masiva. Si un pedido te parece peligroso o "
        "ambiguo, NO lo ejecutes: pedí que te lo confirmen.\n"
        "Todo tu turno queda en la BITÁCORA de la mesa: contá de forma concreta qué corriste, qué "
        "archivos tocaste (con rutas) y qué cambiaste."
    )


# ── Bitácora ──────────────────────────────────────────────────────────────────────
def _log(sesion, participante, herramienta, comando, salida, estado, es_mutacion=False, motivo=''):
    from .models import Accion
    return Accion.objects.create(
        sesion=sesion, participante=participante,
        emisor=(participante.nombre if participante else ''),
        herramienta=herramienta, es_mutacion=es_mutacion,
        comando=(comando or '')[:4000], motivo=(motivo or '')[:2000],
        salida=(salida or '')[:20000], estado=estado,
        resuelto_at=(None if estado == Accion.Estado.PENDIENTE else timezone.now()),
    )


def _aviso(sesion, texto):
    """Aviso de sistema en la mesa (lo que el humano VE pasar por SSE). Único punto donde el
    toolbelt crea Mensajes: emisor 'Enjambre' + es_sistema=True, uniforme en todos los sitios."""
    from .models import Mensaje
    return Mensaje.objects.create(sesion=sesion, emisor='Enjambre', es_sistema=True, texto=texto)


# ── Ejecución de lecturas ─────────────────────────────────────────────────────────
def _correr_readonly(comando, timeout=INSPECT_TIMEOUT):
    """Corre `comando` read-only. Devuelve (salida, error). El gate es la allowlist del primer
    binario; en POSIX shell=False (metacaracteres inertes), en Windows cmd/c + bloqueo de operadores."""
    cmd = (comando or '').strip()
    if not cmd:
        return None, 'comando vacío'
    if _menciona_secreto(cmd):
        return None, 'los archivos de la bóveda de API keys no se leen con el toolbelt'
    if IS_WIN:
        if any(op in cmd for op in _DENY_OPS_WIN):
            return None, 'operadores de shell (& | > < ^) no permitidos en inspect'
        parts = cmd.split()
        # String CRUDO a cmd.exe (no lista): con lista, subprocess re-quotea cada pedazo y rompe
        # los argumentos con comillas (`type "C:\Program Files\x.log"` llegaba mangled). Pasar el
        # string con shell=False en Windows lo entrega verbatim como command line.
        argv = 'cmd /c ' + cmd
        first = parts[0]
    else:
        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            return None, f'comando mal formado: {e}'
        if not parts:
            return None, 'comando vacío'
        argv = parts
        first = parts[0]
    base = os.path.basename(first).lower()
    if base not in _ALLOW:
        return None, (f'«{base}» no está en la allowlist de solo-lectura. Para cambiar el sistema '
                      f'usá apply_fix (queda pendiente de aprobación).')
    low = cmd.lower()
    for flag in _DENY_FLAGS:
        if flag in low.split():
            return None, f'la bandera «{flag}» escribe/ejecuta — no va en inspect; usá apply_fix.'
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False,
                           env=_env())
    except subprocess.TimeoutExpired:
        return None, f'timeout tras {timeout}s'
    except FileNotFoundError:
        return None, f'«{base}» no está instalado en esta máquina'
    except Exception as e:  # noqa: BLE001
        return None, f'error: {e}'
    out, err = _decodificar(r.stdout), _decodificar(r.stderr)
    if err.strip():
        out += ('\n[stderr]\n' + err)
    out = out.strip() or '(sin salida)'
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + '\n…[salida truncada]'
    return out, None


def _system_report():
    """Snapshot read-only, OS-aware, best-effort (cada parte envuelta)."""
    L = [f"Sistema: {platform.platform()}",
         f"Máquina: {platform.machine()} · Host: {platform.node()}",
         f"CPUs: {os.cpu_count()}"]
    if not IS_WIN:
        try:
            la = os.getloadavg()
            L.append(f"Load avg (1/5/15m): {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}")
        except (OSError, AttributeError):
            pass
    try:
        du = shutil.disk_usage('/' if not IS_WIN else 'C:\\')
        gb = 1024 ** 3
        L.append(f"Disco: {du.used/gb:.1f} GB usados / {du.total/gb:.1f} GB "
                 f"({du.used/du.total*100:.0f}%)")
    except Exception:  # noqa: BLE001
        pass
    # RAM + uptime + top procesos: por comando read-only, best-effort.
    extras = ([('ps', 'ps -eo pid,pcpu,pmem,comm --sort=-pcpu'), ('mem', 'free -h'),
               ('up', 'uptime')] if not IS_WIN
              else [('ps', 'tasklist'), ('mem', 'systeminfo')])
    for etiqueta, c in extras:
        out, err = _correr_readonly(c, timeout=15)
        if out:
            head = '\n'.join(out.splitlines()[:12])
            L.append(f"\n[{etiqueta}] $ {c}\n{head}")
    return '\n'.join(L)


def _read_file(ruta, desde=0):
    """Lee el archivo desde el byte `desde`, hasta MAX_FILE bytes. Si queda cola, la cierra
    diciendo con qué `desde` seguir: sin eso una silla que abre un archivo grande se queda con la
    primera tajada y no tiene forma de pedir el resto (mesa 7: README de 87 KB, informe sin hacer)."""
    p = os.path.abspath(os.path.expanduser((ruta or '').strip()))
    if _es_ruta_vedada(p):
        return '(⛔ archivo de credenciales — no se lee con el toolbelt)'
    if not os.path.exists(p):
        return f'(❌ no existe: {p})'
    if os.path.isdir(p):
        return f'(❌ {p} es un directorio — usá list_dir)'
    try:
        desde = max(0, int(desde or 0))
    except (TypeError, ValueError):
        desde = 0
    try:
        total = os.path.getsize(p)
        with open(p, 'rb') as f:
            if desde:
                f.seek(desde)
            data = f.read(MAX_FILE)
    except Exception as e:  # noqa: BLE001
        return f'(❌ no se pudo leer: {e})'
    if desde >= total and total:
        return f'(fin del archivo: son {total} bytes y pediste desde {desde})'
    text = data.decode('utf-8', errors='replace')
    fin = desde + len(data)
    if fin < total:
        text += (f'\n…[cortado en el byte {fin} de {total}. Seguí leyendo con '
                 f'read_file(ruta="{p}", desde={fin})]')
    elif desde:
        text += f'\n…[fin del archivo ({total} bytes)]'
    return text or '(archivo vacío)'


def _list_dir(ruta):
    p = os.path.abspath(os.path.expanduser((ruta or '.').strip()))
    if _es_ruta_vedada(p):
        return '(⛔ carpeta de credenciales — no se lista con el toolbelt)'
    if not os.path.isdir(p):
        return f'(❌ no es un directorio: {p})'
    try:
        entradas = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
    except Exception as e:  # noqa: BLE001
        return f'(❌ no se pudo listar: {e})'
    filas = []
    for e in entradas[:500]:
        try:
            marca = '/' if e.is_dir() else ''
            size = '' if e.is_dir() else f'  {e.stat().st_size}b'
        except OSError:
            marca, size = '', ''
        filas.append(f"{e.name}{marca}{size}")
    extra = f"\n…(+{len(entradas) - 500} más)" if len(entradas) > 500 else ''
    return f"{p}:\n" + '\n'.join(filas) + extra if filas else f"{p}: (vacío)"


def _correr_mutacion(comando, timeout=APPLY_TIMEOUT, cwd=None):
    """Corre un comando que MUTA el sistema, con shell (el agente escribe pipes/redirecciones).
    Devuelve (salida, rc). Sin allowlist: con el toolbelt encendido el permiso ya está dado —
    el freno es el switch, y el registro es la Bitácora. Ver el bloque de arriba.

    `cwd` (la carpeta de la mesa, si el turno la tiene): las rutas RELATIVAS del comando caen ahí
    en vez de en el cwd del proceso. No encierra nada — una ruta absoluta sigue yendo donde diga."""
    try:
        r = subprocess.run(comando, capture_output=True, timeout=timeout, shell=True,
                           env=_env(),
                           cwd=(str(cwd) if cwd and os.path.isdir(str(cwd)) else None))
        out, err = _decodificar(r.stdout), _decodificar(r.stderr)
        if err.strip():
            out += ('\n[stderr]\n' + err)
        out = out.strip() or '(sin salida)'
        if len(out) > MAX_OUT:
            out = out[:MAX_OUT] + '\n…[salida truncada]'
        return out, r.returncode
    except subprocess.TimeoutExpired:
        return f'timeout tras {timeout}s', -1
    except Exception as e:  # noqa: BLE001
        return f'error: {e}', -1


def _write_file(ruta, contenido):
    """Escribe el archivo completo. Devuelve (mensaje, ok). Crea los directorios que falten."""
    p = os.path.abspath(os.path.expanduser((ruta or '').strip()))
    if not p or os.path.isdir(p):
        return f'(❌ ruta inválida para escribir: {p})', False
    # La bóveda no se escribe (igual que no se lee): sobrescribir secrets.enc borra las API keys.
    if _es_ruta_vedada(p) or _menciona_secreto(p):
        return '(⛔ archivo de credenciales — no se toca con el toolbelt)', False
    afuera = _fuera_de_la_mesa(p)
    if afuera:
        return afuera, False
    existia = os.path.exists(p)
    try:
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(contenido or '')
    except Exception as e:  # noqa: BLE001
        return f'(❌ no se pudo escribir: {e})', False
    n = len((contenido or '').encode('utf-8'))
    return f"{'Sobrescrito' if existia else 'Creado'}: {p} ({n} bytes)", True


# ── Dispatcher que llaman los providers ───────────────────────────────────────────
def ejecutar_tool(name, args, sesion, participante):
    """Ejecuta una herramienta y devuelve el texto (tool_result). Registra la Acción en la bitácora.
    Las lecturas corren solas; apply_fix queda PENDIENTE (no ejecuta) y avisa en la mesa."""
    from .models import Accion
    args = args or {}
    if name == 'system_report':
        out = _system_report()
        _log(sesion, participante, 'system_report', '(snapshot)', out, Accion.Estado.EJECUTADA)
        return out
    if name == 'inspect':
        comando = (args.get('comando') or '').strip()
        out, err = _correr_readonly(comando)
        if err:
            _log(sesion, participante, 'inspect', comando, err, Accion.Estado.RECHAZADA)
            return f'(⛔ inspect no permitido: {err})'
        _log(sesion, participante, 'inspect', comando, out, Accion.Estado.EJECUTADA)
        return out
    if name == 'read_file':
        ruta = (args.get('ruta') or '').strip()
        desde = args.get('desde') or 0
        out = _read_file(ruta, desde)
        etiqueta = f'{ruta} (desde {desde})' if desde else ruta
        _log(sesion, participante, 'read_file', etiqueta, out, Accion.Estado.EJECUTADA)
        return out
    if name == 'list_dir':
        ruta = (args.get('ruta') or '').strip()
        out = _list_dir(ruta)
        _log(sesion, participante, 'list_dir', ruta, out, Accion.Estado.EJECUTADA)
        return out
    # Mutaciones: con el toolbelt encendido corren EN EL MOMENTO, igual que las de una silla CLI.
    # El permiso es el switch (apagado por default); el registro es la Bitácora, con estado
    # EJECUTADA — ya pasó, no hay nada que aprobar. Se avisa en la mesa para que el humano lo VEA
    # pasar por SSE: sin gate previo, la visibilidad en vivo es la única red que queda.
    if name == 'apply_fix':
        comando = (args.get('comando') or '').strip()
        motivo = (args.get('motivo') or '').strip()
        if not comando:
            return '(❌ apply_fix necesita un comando)'
        out, rc = _correr_mutacion(comando, cwd=carpeta_turno())
        estado = Accion.Estado.EJECUTADA if rc == 0 else Accion.Estado.ERROR
        _log(sesion, participante, 'apply_fix', comando, out, estado,
             es_mutacion=True, motivo=motivo)
        icono = '🔧' if rc == 0 else '⚠️'
        _aviso(sesion,
               f"{icono} {participante.nombre if participante else 'Una silla'} ejecutó un CAMBIO "
               f"en el sistema (rc {rc}):\n\n$ {comando}\n"
               f"Motivo: {motivo or '(no especificado)'}\n\n{out[:1500]}")
        return f'(rc {rc})\n{out}'
    if name == 'write_file':
        ruta = (args.get('ruta') or '').strip()
        motivo = (args.get('motivo') or '').strip()
        out, ok = _write_file(ruta, args.get('contenido'))
        _log(sesion, participante, 'write_file', ruta, out,
             Accion.Estado.EJECUTADA if ok else Accion.Estado.ERROR,
             es_mutacion=True, motivo=motivo)
        if ok:
            _aviso(sesion,
                   f"📝 {participante.nombre if participante else 'Una silla'} escribió un archivo:\n"
                   f"{out}" + (f"\nMotivo: {motivo}" if motivo else ''))
        return out
    return f'(❌ herramienta desconocida: {name})'


# ── Resolución de pendientes (la llama el endpoint de la web al aprobar/rechazar) ──
# Con el toolbelt unificado ya NADA crea acciones PENDIENTES: las mutaciones corren en el acto.
# Esto queda para resolver las pendientes que hayan quedado en la base de una versión anterior
# (si se borrara, esas acciones viejas quedarían colgadas sin forma de aprobarlas ni rechazarlas).
def ejecutar_pendiente(accion, aprobada_por):
    """Aprueba y ejecuta una Acción pendiente EN EL HOST (shell=True: ya la revisó un humano).
    Actualiza la Acción y postea el resultado en la mesa. Idempotente sobre no-pendientes."""
    from .models import Accion
    if accion.estado != Accion.Estado.PENDIENTE:
        return accion
    try:
        r = subprocess.run(accion.comando, capture_output=True, env=_env(),
                           timeout=APPLY_TIMEOUT, shell=True)
        out, err = _decodificar(r.stdout), _decodificar(r.stderr)
        if err.strip():
            out += ('\n[stderr]\n' + err)
        out = out.strip() or '(sin salida)'
        if len(out) > MAX_OUT:
            out = out[:MAX_OUT] + '\n…[truncado]'
        estado = Accion.Estado.EJECUTADA if r.returncode == 0 else Accion.Estado.ERROR
        rc = r.returncode
    except subprocess.TimeoutExpired:
        out, estado, rc = f'timeout tras {APPLY_TIMEOUT}s', Accion.Estado.ERROR, -1
    except Exception as e:  # noqa: BLE001
        out, estado, rc = f'error: {e}', Accion.Estado.ERROR, -1
    accion.salida, accion.estado = out, estado
    accion.aprobada_por, accion.resuelto_at = aprobada_por, timezone.now()
    accion.save()
    icono = '✅' if estado == Accion.Estado.EJECUTADA else '⚠️'
    _aviso(accion.sesion,
           f"{icono} {aprobada_por} aprobó y se ejecutó (rc {rc}):\n$ {accion.comando}\n\n{out[:1500]}")
    return accion


def rechazar_pendiente(accion, por, motivo=''):
    """Marca una Acción pendiente como rechazada (no se ejecuta) y lo avisa en la mesa."""
    from .models import Accion
    if accion.estado != Accion.Estado.PENDIENTE:
        return accion
    accion.estado = Accion.Estado.RECHAZADA
    accion.aprobada_por, accion.resuelto_at = por, timezone.now()
    accion.salida = (motivo or 'Rechazada por el técnico.')[:20000]
    accion.save()
    _aviso(accion.sesion,
           f"🚫 {por} rechazó el cambio propuesto:\n$ {accion.comando}"
           + (f"\nNota: {motivo}" if motivo else ''))
    return accion


# ── Sillas CLI operando la máquina ────────────────────────────────────────────────
# Los dos backends llegan al mismo lugar por caminos distintos. Las sillas por API key operan la
# máquina con las herramientas de arriba, que Swarm ejecuta una por una (y por eso puede anotar
# cada una en la Bitácora). Con una silla CLI no hay dónde interceptar: claude/opencode/agy traen
# SUS PROPIAS herramientas y nosotros solo les pasamos un prompt por stdin y leemos el texto que
# devuelven — así que lo que se anota es el turno entero, no cada acción.
#
# El control es el MISMO para ambos:
#   · el CANDADO es el switch del toolbelt (apagado por default): prenderlo ES el permiso;
#   · el REGISTRO es la Bitácora — todo queda como acción EJECUTADA (ya pasó, no hay nada que
#     aprobar), con qué corrió, en qué carpeta y qué hizo.
#
# Antes de esto una silla CLI en `/armar` ya alcanzaba toda la PC (el `cwd` de un subprocess no
# es una jaula y el comando de fabricar trae Bash habilitado) pero NADA lo anotaba. El modo
# máquina no agrega poder que no existiera: lo hace explícito y lo deja auditado.

def cwd_maquina():
    """Carpeta donde arranca una silla CLI en modo máquina. Default: el HOME del usuario —
    punto de partida sensato para operar el equipo. `SWARM_TOOLBELT_CWD` lo cambia.

    No es una restricción: el agente tiene shell y puede moverse. Es dónde empieza a mirar."""
    ruta = (getattr(settings, 'SWARM_TOOLBELT_CWD', '')
            or os.environ.get('SWARM_TOOLBELT_CWD') or str(Path.home()))
    p = Path(ruta).expanduser()
    return str(p) if p.is_dir() else str(Path.home())


def encuadre_cli(carpeta=None):
    """Encuadre para una silla CLI que opera la máquina real. Paralelo al de las sillas API,
    con la diferencia clave: acá las herramientas son las del propio CLI, así que los cambios
    NO pasan por aprobación previa — se le avisa para que sea prudente y explícita.

    `carpeta`: la carpeta de la mesa cuando el turno es prolijo (el humano no pidió otra ruta).
    La silla sigue alcanzando toda la máquina — lo que cambia es dónde DEJA lo que produce.
    Sin esto, un líder en modo máquina escribía su entrega en cualquier lado (mesa 4)."""
    lugar = (
        f"Tu carpeta de trabajo es:\n\n    {carpeta}\n\n"
        "Ahí es tu directorio actual y ahí va TODO lo que produzcas (informes, código, notas), "
        "salvo que el humano te haya pedido EXPRESAMENTE otra ruta: en ese caso hacelo donde "
        "te dijo. Lo que quede en esa carpeta se commitea solo y el equipo lo ve; lo que dejes "
        "afuera no lo ve nadie. Ahí vive `NOTAS.md`, la MEMORIA COMPARTIDA de la mesa: leélo "
        "antes de trabajar y dejá ahí decisiones/TODOs para los próximos turnos.\n"
        "Para DIAGNOSTICAR o arreglar el equipo sí tenés acceso a todo el sistema.\n"
    ) if carpeta else (
        f"Tu directorio inicial es {cwd_maquina()}, pero tenés acceso a todo el equipo.\n"
    )
    return (
        f"IMPORTANTE: el TOOLBELT está ENCENDIDO y estás operando DIRECTAMENTE la máquina real "
        f"de la persona que te pregunta — no un sandbox. "
        f"Sistema: {platform.platform()} · shell: {SHELL_NAME} · host: {platform.node()}. "
        f"{lugar}"
        "Usá tus propias herramientas (leer, editar, ejecutar) sobre este sistema. Lo que hacés "
        "NO espera aprobación: se aplica en el momento. Por eso: mirá ANTES de tocar, un cambio "
        "por vez, y NO toques nada que no te hayan pedido. Nunca borres ni sobrescribas de forma "
        "masiva.\n"
        "Todo tu turno queda en la BITÁCORA de la mesa, que es donde el equipo TE VE TRABAJAR: "
        "contá SIEMPRE, de forma concreta, qué comandos corriste, qué archivos miraste y qué "
        "cambiaste (con rutas). Es tu registro público, no un resumen para quedar bien. "
        "Si un pedido te parece peligroso o ambiguo, NO lo ejecutes: pedí que te lo confirmen."
    )


def encuadre_api():
    """Encuadre de una silla por API key en modo máquina. Corto A PROPÓSITO: las reglas del juego
    ya le llegan como `system` (system_prompt(), que arma chat_agentic). Acá solo se le recuerda,
    dentro de la conversación de la mesa, que tiene herramientas de verdad — sin esto una silla
    tiende a razonar en abstracto y contestar «habría que revisar X» en vez de ir a mirarlo."""
    return (
        "IMPORTANTE: el TOOLBELT está ENCENDIDO y tenés herramientas REALES sobre esta máquina "
        "(inspect, read_file, list_dir, write_file, apply_fix). No teorices ni supongas: si hace "
        "falta un dato del sistema, andá a buscarlo; si hay que cambiar algo, hacelo. Contá "
        "concretamente qué miraste y qué tocaste, con rutas."
    )


def encuadre_api_mesa(carpeta):
    """Encuadre de una silla API en un turno ORDENADO (la mesa tiene carpeta). La diferencia con
    una silla CLI es que la API no tiene cwd: sus herramientas escriben por RUTA ABSOLUTA, así que
    la carpeta de la mesa no la deduce sola — hay que dársela. Sin esto la silla escribe en
    cualquier lado (o en ningún lado) y el commit de la mesa sale vacío."""
    return (
        f"IMPORTANTE: el TOOLBELT está ENCENDIDO y tenés herramientas REALES sobre esta máquina. "
        f"La CARPETA DE TRABAJO de esta mesa es:\n\n    {carpeta}\n\n"
        "Dejá ahí lo que produzcas, SIEMPRE con rutas absolutas bajo esa carpeta (tus herramientas "
        "no tienen directorio actual: una ruta relativa no cae ahí). Para DIAGNOSTICAR el equipo "
        "leé lo que necesites de todo el sistema; lo que cambia es dónde dejás el resultado — si el "
        "humano te pidió tocar otra ruta, decíselo en la mesa y esperá que te lo confirme. "
        "Usá list_dir/read_file para ver qué hay antes de escribir, y write_file para crear o "
        "reescribir. Ahí vive `NOTAS.md`, la MEMORIA COMPARTIDA de la mesa: leélo antes de trabajar "
        "y dejá ahí decisiones/TODOs para los próximos turnos. Al terminar contá CONCRETAMENTE qué "
        "archivos tocaste. No afirmes cambios que no hiciste: lo que quede en la carpeta se "
        "commitea y el equipo ve el diff."
    )


def log_cli(sesion, participante, argv, cwd, salida):
    """Anota en la Bitácora un turno de silla CLI en modo máquina.

    Es la vidriera del producto: acá el equipo VE cómo trabaja una silla CLI sobre la máquina.
    Por eso se guarda el comando real, la carpeta y la respuesta completa de la silla (donde
    cuenta qué hizo). Estado EJECUTADA — ya corrió, no hay pendiente que aprobar."""
    from .models import Accion
    return _log(
        sesion, participante, 'cli_maquina',
        f"$ {' '.join(argv)}\n  (cwd: {cwd})", salida, Accion.Estado.EJECUTADA,
        es_mutacion=True, motivo='Turno de silla CLI con el toolbelt encendido.',
    )
