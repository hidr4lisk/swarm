"""
enjambre/onboarding.py — estado de la ESCALERA de arranque (2 escalones).

Este módulo solo CALCULA el estado de cada escalón; el copy vive en los templates (donde están las
{% trans %}) y acá no se toca la red — todo es local: DB, filesystem y bóveda.

Escalones:
  1 — opencode CLI (login gratis): binario en PATH + credencial detectada. Dos señales DISTINTAS:
      instalado ≠ logueado, y "instalado pero sin login" es el estado más probable → CTA propio.
  2 — API keys: la bóveda tiene al menos un proveedor configurado.

(Hubo un escalón 0 «silla gratis sin key» —Chispa, sobre el tier anónimo de Pollinations— retirado
el 2026-07-20 cuando ese tier murió: pasó a créditos «pollen» y devolvía HTTP 402. Pollinations
sigue, pero como proveedor por API key del escalón 2. Ver providers/pollinations.py.)
"""


def escalones():
    """Estado de la escalera → lista de 2 dicts para el template. Sin red, barato de renderizar."""
    from . import conexiones, vault

    oc_instalado = bool(conexiones.resolver_bin('opencode'))
    oc_logueado = bool(conexiones.detectar().get('opencode'))
    con_keys = bool(vault.configured_providers())

    return [
        {'n': 1, 'listo': oc_instalado and oc_logueado,
         'instalado': oc_instalado, 'logueado': oc_logueado},
        {'n': 2, 'listo': con_keys},
    ]


def listos(esc):
    """Cuántos escalones están listos. Va al `<summary>` plegado ('1 de 2'), así el estado se
    ve sin desplegar la escalera."""
    return sum(1 for e in esc if e['listo'])


def completa():
    """True si la escalera está completa (escalón 2 listo) → el banner de home no se muestra."""
    from . import vault
    return bool(vault.configured_providers())
