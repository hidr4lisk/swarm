"""
providers/errores.py — clasificador de los marcadores de error del camino API.

El contrato de providers.chat() es «nunca lanza: devuelve texto o un marcador (❌ …)». La cascada
de resiliencia necesita distinguir qué marcadores valen un reintento y cuáles son terminales, SIN
romper ese contrato: se clasifica el string, no una excepción. Los marcadores son NUESTROS
(_http_json y los clientes los emiten), así que el parseo es estable y está bajo test.

- 'ok'          → no es un marcador de error ((sin respuesta) tampoco lo es: el modelo contestó vacío)
- 'reintentable'→ transitorio: 429/5xx/timeout/red caída/cuerpo no-JSON → retry con backoff
- 'terminal'    → no va a mejorar reintentando: 4xx de request/key/modelo, proveedor desconocido
"""
import re

_RE_HTTP = re.compile(r'^\(❌ HTTP (\d{3})')

# Transitorios: too many requests, upstream sobrecargado, gateways. 529 = overloaded (Anthropic).
REINTENTABLES = {408, 409, 425, 429, 500, 502, 503, 504, 529}

_MARCADORES_REINTENTABLES = (
    '(⏰',                                 # timeout del request
    '(❌ sin conexión',                    # URLError: DNS/red — transitorio, pero _http_json NO lo
                                           # reintenta (sin red, 3 esperas son tiempo muerto)
    '(❌ respuesta no-JSON del proveedor',  # 200 con basura (observado en Pollinations al pasarse del límite)
)


def clasificar(salida):
    """'ok' | 'reintentable' | 'terminal' para un marcador devuelto por el camino API."""
    s = (salida or '').strip()
    m = _RE_HTTP.match(s)
    if m:
        return 'reintentable' if int(m.group(1)) in REINTENTABLES else 'terminal'
    for pref in _MARCADORES_REINTENTABLES:
        if s.startswith(pref):
            return 'reintentable'
    if s.startswith('(❌'):
        return 'terminal'   # sin key / key inválida / proveedor desconocido / respuesta inesperada
    return 'ok'
