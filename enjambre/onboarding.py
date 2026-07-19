"""
enjambre/onboarding.py — estado de la ESCALERA de arranque (los 3 escalones).

El primer contacto con Swarm no puede ser un formulario de credenciales: el escalón 0 (Chispa,
Pollinations anónimo) charla sin configurar nada, y de ahí se sube. Este módulo solo CALCULA el
estado de cada escalón; el copy vive en los templates (donde están las {% trans %}) y acá no se
toca la red — todo es local: DB, filesystem y bóveda.

Escalones:
  0 — silla gratis (sin key): hay una silla activa de un proveedor sin_key.
  1 — opencode CLI (login gratis): binario en PATH + credencial detectada. Dos señales DISTINTAS:
      instalado ≠ logueado, y "instalado pero sin login" es el estado más probable → CTA propio.
  2 — API keys: la bóveda tiene al menos un proveedor configurado.
"""
import shutil

from .clientes import CLIENTES, api_de

# Proveedores API que funcionan sin credencial (hoy: pollinations).
_SIN_KEY_APIS = {c['api'] for c in CLIENTES.values() if c.get('api') and c.get('sin_key')}


def escalones():
    """Estado de la escalera → lista de 3 dicts para el template. Sin red, barato de renderizar."""
    from . import conexiones, vault
    from .models import Participante

    sin_key_activa = any(api_de(p) in _SIN_KEY_APIS
                         for p in Participante.objects.filter(activo=True))
    oc_instalado = bool(shutil.which('opencode'))
    oc_logueado = bool(conexiones.detectar().get('opencode'))
    con_keys = bool(vault.configured_providers())

    return [
        {'n': 0, 'listo': sin_key_activa},
        {'n': 1, 'listo': oc_instalado and oc_logueado,
         'instalado': oc_instalado, 'logueado': oc_logueado},
        {'n': 2, 'listo': con_keys},
    ]


def completa():
    """True si la escalera está completa (escalón 2 listo) → el banner de home no se muestra."""
    from . import vault
    return bool(vault.configured_providers())
