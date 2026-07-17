"""
enjambre/toolbelt.py — herramientas de las sillas api:* sobre el SISTEMA REAL (F3).

El cambio de paradigma de Swarm 2.0: las sillas por API key NO trabajan encapsuladas en la
carpeta git de la mesa — operan la máquina a la que enchufás el pendrive (soporte en la PC del
cliente). No hay sandbox. La red de seguridad es este módulo:

  · LECTURA (system_report / inspect / read_file / list_dir): read-only → se ejecuta SOLA. `inspect`
    corre SIN shell, con **allowlist** de binarios read-only (el gate principal) + denylist de
    banderas que escriben (find -delete/-exec).
  · MUTACIÓN (apply_fix): cambia el sistema → **NUNCA** se ejecuta sola. Crea una Acción
    `pendiente` y avisa en la mesa; el técnico la aprueba (se ejecuta con shell, ya revisada por un
    humano) o la rechaza. Toda acción — lectura o mutación — queda en la **bitácora** (modelo Acción).

Opt-in: apagado salvo `SWARM_TOOLBELT` (setting/env). Un tool de mucho poder → arranca off.
OS-aware por `platform.system()`. Ver el "Threat model" ampliado del README.
"""
import os
import platform
import shlex
import shutil
import subprocess
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
_DENY_FLAGS = ('-delete', '-exec', '-execdir', '-ok', '-fprint', '-fls', '-fprintf')
# Operadores de shell: en Windows `cmd /c` los interpreta → los bloqueamos (en POSIX corremos
# shell=False, así que son inertes y NO se bloquean, para no romper regex con `|`).
_DENY_OPS_WIN = ('&', '|', '>', '<', '^')


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
         'Lee el contenido de un archivo del sistema (acotado por tamaño). Read-only, auto.',
         'input_schema': {'type': 'object', 'properties': {
             'ruta': {'type': 'string', 'description': 'Ruta absoluta o ~ del archivo.'}},
             'required': ['ruta']}},
        {'name': 'list_dir', 'description':
         'Lista el contenido de un directorio del sistema. Read-only, auto.',
         'input_schema': {'type': 'object', 'properties': {
             'ruta': {'type': 'string', 'description': 'Ruta del directorio.'}},
             'required': ['ruta']}},
        {'name': 'apply_fix', 'description':
         'Propone un comando que MODIFICA el sistema (instalar, editar, reiniciar un servicio, etc.). '
         'NO se ejecuta solo: queda PENDIENTE hasta que el técnico lo apruebe en la Bitácora. NO '
         'asumas que corrió; explicá SIEMPRE el motivo. Corré primero inspect/read_file para justificar.',
         'input_schema': {'type': 'object', 'properties': {
             'comando': {'type': 'string', 'description': 'El comando exacto a ejecutar si se aprueba.'},
             'motivo': {'type': 'string', 'description': 'Por qué hace falta y qué esperás que logre.'}},
             'required': ['comando', 'motivo']}},
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
        "• apply_fix → para CUALQUIER cambio (instalar, editar, reiniciar, borrar). NO corre solo: "
        "queda pendiente de que un HUMANO lo apruebe. Nunca afirmes que un cambio ya se hizo; decí "
        "que lo dejaste propuesto. Justificá cada apply_fix con lo que viste en las lecturas.\n"
        "Trabajá como un sysadmin prudente: mirá antes de tocar, un cambio por vez, explicá el porqué."
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


# ── Ejecución de lecturas ─────────────────────────────────────────────────────────
def _correr_readonly(comando, timeout=INSPECT_TIMEOUT):
    """Corre `comando` read-only. Devuelve (salida, error). El gate es la allowlist del primer
    binario; en POSIX shell=False (metacaracteres inertes), en Windows cmd/c + bloqueo de operadores."""
    cmd = (comando or '').strip()
    if not cmd:
        return None, 'comando vacío'
    if IS_WIN:
        if any(op in cmd for op in _DENY_OPS_WIN):
            return None, 'operadores de shell (& | > < ^) no permitidos en inspect'
        parts = cmd.split()
        argv = ['cmd', '/c'] + parts
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
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return None, f'timeout tras {timeout}s'
    except FileNotFoundError:
        return None, f'«{base}» no está instalado en esta máquina'
    except Exception as e:  # noqa: BLE001
        return None, f'error: {e}'
    out = (r.stdout or '')
    if (r.stderr or '').strip():
        out += ('\n[stderr]\n' + r.stderr)
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


def _read_file(ruta):
    p = os.path.abspath(os.path.expanduser((ruta or '').strip()))
    if not os.path.exists(p):
        return f'(❌ no existe: {p})'
    if os.path.isdir(p):
        return f'(❌ {p} es un directorio — usá list_dir)'
    try:
        with open(p, 'rb') as f:
            data = f.read(MAX_FILE + 1)
    except Exception as e:  # noqa: BLE001
        return f'(❌ no se pudo leer: {e})'
    text = data[:MAX_FILE].decode('utf-8', errors='replace')
    if len(data) > MAX_FILE:
        text += f'\n…[truncado a {MAX_FILE} bytes]'
    return text or '(archivo vacío)'


def _list_dir(ruta):
    p = os.path.abspath(os.path.expanduser((ruta or '.').strip()))
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
        out = _read_file(ruta)
        _log(sesion, participante, 'read_file', ruta, out, Accion.Estado.EJECUTADA)
        return out
    if name == 'list_dir':
        ruta = (args.get('ruta') or '').strip()
        out = _list_dir(ruta)
        _log(sesion, participante, 'list_dir', ruta, out, Accion.Estado.EJECUTADA)
        return out
    if name == 'apply_fix':
        comando = (args.get('comando') or '').strip()
        motivo = (args.get('motivo') or '').strip()
        if not comando:
            return '(❌ apply_fix necesita un comando)'
        acc = _log(sesion, participante, 'apply_fix', comando,
                   'Pendiente de aprobación del técnico.', Accion.Estado.PENDIENTE,
                   es_mutacion=True, motivo=motivo)
        # Avisar en la mesa para que el humano lo vea (aparece por SSE).
        from .models import Mensaje
        Mensaje.objects.create(
            sesion=sesion, emisor='Enjambre', es_sistema=True,
            texto=(f"🔒 {participante.nombre if participante else 'Una silla'} propone un CAMBIO en el "
                   f"sistema (queda pendiente de tu aprobación en la Bitácora):\n\n$ {comando}\n"
                   f"Motivo: {motivo or '(no especificado)'}"))
        return (f'apply_fix #{acc.pk} ENCOLADO — pendiente de aprobación humana. NO corrió todavía. '
                f'No asumas su resultado; seguí con lo que puedas sin depender de este cambio.')
    return f'(❌ herramienta desconocida: {name})'


# ── Resolución de pendientes (la llama el endpoint de la web al aprobar/rechazar) ──
def ejecutar_pendiente(accion, aprobada_por):
    """Aprueba y ejecuta una Acción pendiente EN EL HOST (shell=True: ya la revisó un humano).
    Actualiza la Acción y postea el resultado en la mesa. Idempotente sobre no-pendientes."""
    from .models import Accion, Mensaje
    if accion.estado != Accion.Estado.PENDIENTE:
        return accion
    try:
        r = subprocess.run(accion.comando, capture_output=True, text=True,
                           timeout=APPLY_TIMEOUT, shell=True)
        out = (r.stdout or '')
        if (r.stderr or '').strip():
            out += ('\n[stderr]\n' + r.stderr)
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
    Mensaje.objects.create(
        sesion=accion.sesion, emisor='Enjambre', es_sistema=True,
        texto=f"{icono} {aprobada_por} aprobó y se ejecutó (rc {rc}):\n$ {accion.comando}\n\n{out[:1500]}")
    return accion


def rechazar_pendiente(accion, por, motivo=''):
    """Marca una Acción pendiente como rechazada (no se ejecuta) y lo avisa en la mesa."""
    from .models import Accion, Mensaje
    if accion.estado != Accion.Estado.PENDIENTE:
        return accion
    accion.estado = Accion.Estado.RECHAZADA
    accion.aprobada_por, accion.resuelto_at = por, timezone.now()
    accion.salida = (motivo or 'Rechazada por el técnico.')[:20000]
    accion.save()
    Mensaje.objects.create(
        sesion=accion.sesion, emisor='Enjambre', es_sistema=True,
        texto=f"🚫 {por} rechazó el cambio propuesto:\n$ {accion.comando}"
              + (f"\nNota: {motivo}" if motivo else ''))
    return accion
