"""
enjambre/ratelimit.py — espaciador de requests por proveedor (para los tiers gratis).

Pollinations anónimo admite 1 request cada 15 s (5 s con el token gratis). Sin esto, una mesa
con la silla del escalón 0 se come 429s en cadena. `esperar(key)` bloquea hasta que la ventana
del proveedor esté libre y la sella; se llama ANTES del urlopen (no consume el timeout HTTP).

Dos capas:
- MEMORIA (Lock + dict): alcanza para el caso portable — el worker es un hilo único y las mesas
  se procesan secuencialmente.
- ARCHIVO (`<SWARM_DATA_DIR>/.ratelimit.json`, escrito con os.replace, atómico): cubre lo que la
  memoria no ve — otro proceso (un manage.py suelto en otra
  terminal) y el reinicio del proceso.

Lock multiproceso por DIRECTORIO (os.mkdir, atómico en Linux y Windows) — NO fcntl/flock, que no
existe en el Python de Windows y el target es un pendrive Win+Linux. Si el lock queda huérfano
(proceso muerto a mitad de escritura), se roba pasado _LOCK_STALE_S.

`castigar(key, segundos)` estira la ventana tras un 429 (honrando Retry-After si vino).
"""
import json
import os
import threading
import time
from pathlib import Path

from django.conf import settings

# Intervalos por proveedor: (sin token, con token). Solo los tiers gratis se espacian; los
# proveedores pagos no aparecen acá y esperar() con intervalo 0 es no-op.
INTERVALOS = {'pollinations': (15.0, 5.0)}

_LOCK_STALE_S = 30
_lock = threading.Lock()
_ultimo = {}    # key → epoch del último request (capa memoria)
_castigo = {}   # key → (hasta_epoch, intervalo_extra) tras un 429


def _archivo():
    return Path(getattr(settings, 'SWARM_DATA_DIR', Path(settings.BASE_DIR) / 'data')) / '.ratelimit.json'


def _lock_dir():
    return _archivo().with_suffix('.lock')


def _con_lock_de_archivo(fn):
    """Corre fn() con el lock multiproceso tomado (mkdir atómico). Si el lock está huérfano hace
    más de _LOCK_STALE_S, se roba. Si el FS no colabora (pendrive read-only), fn corre igual sin
    lock — peor caso: dos procesos se pisan un timestamp, que solo relaja el espaciado un request."""
    d = _lock_dir()
    tomado = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.mkdir(d)
            tomado = True
            break
        except FileExistsError:
            try:
                if time.time() - d.stat().st_mtime > _LOCK_STALE_S:
                    os.rmdir(d)  # huérfano → robar
                    continue
            except OSError:
                pass
            time.sleep(0.05)
        except OSError:
            break  # FS raro → sin lock
    try:
        return fn()
    finally:
        if tomado:
            try:
                os.rmdir(d)
            except OSError:
                pass


def _leer_archivo():
    try:
        return json.loads(_archivo().read_text())
    except (OSError, ValueError):
        return {}


def _sellar_archivo(key, epoch):
    def _w():
        data = _leer_archivo()
        data[key] = epoch
        tmp = _archivo().with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, _archivo())
        except OSError:
            pass  # pendrive lleno/RO → la capa memoria sigue cubriendo este proceso
    _con_lock_de_archivo(_w)


def intervalo_de(key):
    """Segundos entre requests para `key`. 0 = sin límite. Para pollinations depende de si hay
    token en la bóveda (desbloqueada): el tier registrado triplica el ritmo."""
    par = INTERVALOS.get(key)
    if not par:
        return 0.0
    sin_token, con_token = par
    try:
        from . import vault
        return con_token if vault.get_key(key) else sin_token
    except Exception:  # noqa: BLE001 — sin vault utilizable, asumir el tier lento
        return sin_token


def esperar(key):
    """Bloquea hasta que la ventana de `key` esté libre y la sella. No-op si key no tiene límite."""
    intervalo = intervalo_de(key)
    if not key or intervalo <= 0:
        return
    with _lock:
        ahora = time.time()
        ultimo = max(_ultimo.get(key, 0.0), _leer_archivo().get(key, 0.0))
        hasta, extra = _castigo.get(key, (0.0, 0.0))
        if ahora < hasta:
            intervalo = max(intervalo, extra)
        falta = (ultimo + intervalo) - ahora
        if falta > 0:
            time.sleep(falta)
        sello = time.time()
        _ultimo[key] = sello
    _sellar_archivo(key, sello)


def castigar(key, segundos=0.0):
    """Tras un 429: estira la ventana de `key` unos minutos. `segundos` = Retry-After si vino;
    si no, el doble del intervalo actual."""
    if not key:
        return
    extra = float(segundos) if segundos else intervalo_de(key) * 2
    with _lock:
        _castigo[key] = (time.time() + 300, extra)
