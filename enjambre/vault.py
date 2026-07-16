"""
enjambre/vault.py — bóveda de API keys cifrada por passphrase (Swarm portable).

Swarm clásico NO guardaba credenciales (usaba el login de tus CLIs). La ruta portable, en
cambio, lleva las API keys en el pendrive → hay que guardarlas, pero cifradas: si roban el
pendrive sin la passphrase, no hay tokens en claro.

Diseño:
  · `secrets.enc` (en disco): JSON con `salt` (b64), `providers` (nombres EN CLARO, para que la
    UI liste qué hay configurado sin desbloquear) y `blob` (token Fernet con los secretos).
    La clave Fernet sale de scrypt(passphrase, salt) — nunca se guarda.
  · `.secrets.runtime.json` (0600): al DESBLOQUEAR se escriben los tokens descifrados acá; es el
    canal entre el proceso web (donde se tipea la passphrase) y el worker (que despacha las
    sillas api:* y necesita la key). Al BLOQUEAR/cerrar se borra. Mientras está desbloqueado, los
    tokens viven en ese archivo 0600 — es el trade-off del modo local single-user (documentado en
    el threat model del README).

Nada de esto loguea jamás el contenido de una key.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken

# Proveedores por API key soportados (espejo de los api-* de clientes.py).
PROVIDERS = ('anthropic', 'openai', 'openrouter')

# Passphrase mínima al CREAR la bóveda. El salt va EN CLARO en secrets.enc (para listar
# providers sin abrir), así que una passphrase vacía/trivial se forzaría al toque → rompería
# el threat model. Se exige solo al crear; después la passphrase ya quedó fijada.
MIN_PASSPHRASE = 8

# Parámetros scrypt (interactivos, razonables en una notebook modesta).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
# scrypt necesita ~128·N·r·p = 32 MiB para estos parámetros; el default de OpenSSL (maxmem=32 MiB)
# lo roza y corta con «memory limit exceeded». Le damos holgura explícita (64 MiB) — despreciable
# en cualquier PC donde corra Swarm.
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _data_dir():
    d = Path(getattr(settings, 'SWARM_DATA_DIR', Path(settings.BASE_DIR) / 'data'))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vault_path():
    return _data_dir() / 'secrets.enc'


def _runtime_path():
    return _data_dir() / '.secrets.runtime.json'


def _derive(passphrase, salt):
    """passphrase + salt → clave Fernet (urlsafe b64 de 32 bytes) vía scrypt."""
    raw = hashlib.scrypt(passphrase.encode('utf-8'), salt=salt,
                         n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
                         maxmem=_SCRYPT_MAXMEM)
    return base64.urlsafe_b64encode(raw)


# ── Estado ────────────────────────────────────────────────────────────────────
def has_vault():
    """True si ya existe una bóveda (al menos una key guardada alguna vez)."""
    return _vault_path().exists()


def is_unlocked():
    return _runtime_path().exists()


def configured_providers():
    """Nombres de proveedores con key guardada (en claro en secrets.enc). Sin desbloquear."""
    try:
        meta = json.loads(_vault_path().read_text())
        return list(meta.get('providers', []))
    except (OSError, ValueError):
        return []


# ── Descifrado / lectura ────────────────────────────────────────────────────────
def _decrypt(passphrase):
    """Devuelve el dict {provider: token} descifrado, o lanza InvalidToken/ValueError."""
    meta = json.loads(_vault_path().read_text())
    salt = base64.b64decode(meta['salt'])
    f = Fernet(_derive(passphrase, salt))
    data = f.decrypt(meta['blob'].encode())
    return json.loads(data)


def verify(passphrase):
    """¿La passphrase abre la bóveda? (no escribe runtime)."""
    if not has_vault():
        return False
    try:
        _decrypt(passphrase)
        return True
    except (InvalidToken, ValueError, KeyError, OSError):
        return False


def unlock(passphrase):
    """Descifra y deja los tokens en el runtime 0600 para que el worker los lea. True si abrió."""
    if not has_vault():
        return False
    try:
        tokens = _decrypt(passphrase)
    except (InvalidToken, ValueError, KeyError, OSError):
        return False
    _write_runtime(tokens)
    return True


def lock():
    """Borra el runtime descifrado (bloquea)."""
    try:
        _runtime_path().unlink()
    except OSError:
        pass


def get_key(provider):
    """Token del proveedor si la bóveda está desbloqueada; '' si no. Lo usa el motor."""
    try:
        tokens = json.loads(_runtime_path().read_text())
        return tokens.get(provider, '') or ''
    except (OSError, ValueError):
        return ''


# ── Escritura ────────────────────────────────────────────────────────────────
def _write_runtime(tokens):
    p = _runtime_path()
    p.write_text(json.dumps(tokens))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # Windows: permisos POSIX limitados (queda en el espacio del usuario)


def _write_vault(passphrase, tokens, salt):
    f = Fernet(_derive(passphrase, salt))
    blob = f.encrypt(json.dumps(tokens).encode()).decode()
    meta = {'salt': base64.b64encode(salt).decode(),
            'providers': sorted(tokens.keys()),
            'blob': blob}
    _vault_path().write_text(json.dumps(meta))


def set_key(passphrase, provider, token):
    """Guarda/actualiza la key de un proveedor. La PRIMERA key fija la passphrase de la bóveda
    (mín. MIN_PASSPHRASE); las siguientes deben usar la MISMA passphrase (si no, no puede
    descifrar lo existente). Al guardar OK deja la bóveda DESBLOQUEADA: acabás de probar la
    passphrase, así que la key queda usable sin un paso extra. Devuelve (ok, error)."""
    if provider not in PROVIDERS:
        return False, 'proveedor desconocido'
    token = (token or '').strip()
    if not token:
        return False, 'la key está vacía'
    # Una API key es siempre ASCII imprimible. Si trae un emoji/carácter raro (típico: pegaste sin
    # querer el texto de un error), rechazala acá: si no, urllib revienta al mandar el header
    # Authorization con un latin-1 codec error (bug real cazado en la VM).
    if not token.isascii() or any(ord(ch) < 0x20 for ch in token):
        return False, 'la API key tiene caracteres inválidos (¿pegaste texto de más?)'
    if has_vault():
        meta = json.loads(_vault_path().read_text())
        salt = base64.b64decode(meta['salt'])
        try:
            tokens = _decrypt(passphrase)
        except (InvalidToken, ValueError, KeyError):
            return False, 'passphrase incorrecta'
    else:
        # Crear la bóveda: exigir una passphrase real (no vacía ni solo espacios ni corta).
        if len(passphrase or '') < MIN_PASSPHRASE or not (passphrase or '').strip():
            return False, f'elegí una passphrase de al menos {MIN_PASSPHRASE} caracteres'
        salt = os.urandom(16)
        tokens = {}
    tokens[provider] = token
    _write_vault(passphrase, tokens, salt)
    _write_runtime(tokens)     # auto-desbloqueo: la key queda activa en un solo paso
    return True, ''


def remove_key(passphrase, provider):
    """Borra la key de un proveedor. Devuelve (ok, error)."""
    if not has_vault():
        return False, 'no hay bóveda'
    meta = json.loads(_vault_path().read_text())
    salt = base64.b64decode(meta['salt'])
    try:
        tokens = _decrypt(passphrase)
    except (InvalidToken, ValueError, KeyError):
        return False, 'passphrase incorrecta'
    tokens.pop(provider, None)
    _write_vault(passphrase, tokens, salt)
    if is_unlocked():
        _write_runtime(tokens)
    return True, ''
