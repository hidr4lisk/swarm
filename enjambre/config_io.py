"""
enjambre/config_io.py — exportar/importar la configuración de las SILLAS a un archivo JSON.

Para qué: la config de sillas (nombres, modelos, prompts, retratos, colores, orden) es trabajo
del humano y vive en la DB. Al estrenar una versión de Swarm —pendrive nuevo, otra PC, un
`data/` limpio— esa config se perdía y había que rehacerla a mano. Con esto se baja un archivo
y se sube en la instalación nueva.

Dos decisiones de diseño que importan:

1. **Se exporta `cliente` + `modelo`, NO el `comando` crudo.** El `comando` es el argv que el
   worker corre por subprocess: aceptar argv arbitrario de un archivo sería ejecutar lo que
   diga el JSON. Acá el import valida el cliente contra `CLIENTES` y RECONSTRUYE el comando con
   `build_comando()` de esta versión. Efecto secundario bueno: un export viejo importado en una
   versión nueva hereda los arreglos de flags del cliente, en vez de arrastrar el argv obsoleto.

2. **No viajan credenciales.** Las API keys viven cifradas en la bóveda (`vault.py`), aparte;
   el export es config de sillas y nada más. El archivo se puede compartir sin miedo — salvo
   que hayas escrito algo sensible en un prompt, que sí viaja (es config tuya).

Formato: `{"swarm_config": 1, "exportado": <ISO>, "sillas": [...], "avatares": {...}}`.
La versión permite migrar el formato más adelante sin romper archivos viejos.
"""
import json
from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.utils.translation import gettext as _

from .clientes import CLIENTES, build_comando, cliente_de, modelo_de
from .models import AvataresEnjambre, Participante

FORMATO = 1
#: Tope del archivo subido. Los retratos son data-URIs de ~10-30 KB; con 9 sillas el export
#: real ronda 70 KB. 5 MB deja margen de sobra y frena un archivo absurdo antes de parsearlo.
MAX_BYTES = 5 * 1024 * 1024

#: Campos de texto que se copian tal cual, con su tope de largo (los mismos del modelo).
_TEXTO = {
    'nombre': 100, 'persona': None, 'recordatorio': None,
    'especialidad': 120, 'rol_tarjeta': 40, 'endpoint_model': 100, 'endpoint_url': 200,
}


def _limpio(val, tope):
    val = (val or '')
    if not isinstance(val, str):
        return ''
    val = val.strip()
    return val[:tope] if tope else val


def exportar():
    """Dict serializable con la config de todas las sillas + los retratos del humano/sistema."""
    sillas = []
    for p in Participante.objects.order_by('orden', 'key'):
        sillas.append({
            'key': p.key,
            'nombre': p.nombre,
            # cliente/modelo en vez del argv crudo (ver docstring del módulo)
            'cliente': cliente_de(p),
            'modelo': modelo_de(p),
            'endpoint_url': p.endpoint_url,
            'endpoint_model': p.endpoint_model,
            'persona': p.persona,
            'recordatorio': p.recordatorio,
            'especialidad': p.especialidad,
            'rol_tarjeta': p.rol_tarjeta,
            'color_ui': p.color_ui,
            'avatar': p.avatar,
            'activo': p.activo,
            'orden': p.orden,
        })
    esp = AvataresEnjambre.get()
    return {
        'swarm_config': FORMATO,
        'exportado': datetime.now(dt_timezone.utc).isoformat(timespec='seconds'),
        'sillas': sillas,
        'avatares': {
            'enjambre': esp.enjambre, 'humano': esp.humano,
            'color_enjambre': esp.color_enjambre, 'color_humano': esp.color_humano,
        },
    }


def exportar_json():
    """El export ya serializado (indent=2 para que sea diffeable en git si lo versionás)."""
    return json.dumps(exportar(), ensure_ascii=False, indent=2)


class ErrorImport(Exception):
    """El archivo no es un export de Swarm utilizable. El mensaje va derecho a la UI."""


def _parsear(raw):
    if len(raw) > MAX_BYTES:
        raise ErrorImport(_("El archivo es demasiado grande (tope 5 MB)."))
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ErrorImport(_("El archivo no es JSON válido."))
    if not isinstance(data, dict) or 'swarm_config' not in data:
        raise ErrorImport(_("Eso no parece un export de Swarm (falta «swarm_config»)."))
    if data.get('swarm_config') != FORMATO:
        raise ErrorImport(_("Formato %(f)s desconocido (esta versión lee el %(ok)s).")
                          % {'f': data.get('swarm_config'), 'ok': FORMATO})
    if not isinstance(data.get('sillas'), list):
        raise ErrorImport(_("El export no trae una lista de sillas."))
    return data


def importar(raw, reemplazar=False, avatar_limpio=str, color_limpio=str):
    """Aplica un export a la DB. Devuelve un reporte {'creadas','actualizadas','borradas','avisos'}.

    `reemplazar=True` borra las sillas que NO estén en el archivo (deja la DB igual al export);
    por defecto FUSIONA: pisa las que coinciden por `key` y agrega las que faltan, sin tocar el resto.

    Los sanitizadores de avatar/color se inyectan (viven en views.py junto a los del form) para
    que el archivo no pueda meter un data-URI gigante ni un color con markup.

    Todo va en UNA transacción: si una silla del archivo está rota, no queda nada a medias.
    """
    data = _parsear(raw)
    rep = {'creadas': 0, 'actualizadas': 0, 'borradas': 0, 'avisos': []}
    keys_archivo = set()

    with transaction.atomic():
        for i, s in enumerate(data['sillas'], 1):
            if not isinstance(s, dict):
                rep['avisos'].append(_("Silla #%(i)s: no es un objeto, salteada.") % {'i': i})
                continue
            key = _limpio(s.get('key'), 50)
            nombre = _limpio(s.get('nombre'), 100)
            if not key or not nombre:
                rep['avisos'].append(_("Silla #%(i)s: sin key o sin nombre, salteada.") % {'i': i})
                continue

            cliente = _limpio(s.get('cliente'), 40)
            if cliente not in CLIENTES:
                rep['avisos'].append(
                    _("«%(n)s»: el cliente «%(c)s» no existe en esta versión, salteada.")
                    % {'n': nombre, 'c': cliente})
                continue

            modelo = _limpio(s.get('modelo'), 100)
            cmd, cmdt = build_comando(cliente, modelo)
            campos = {
                'nombre': nombre,
                'comando': cmd,
                'comando_trabajo': cmdt,
                # Silla HTTP (Ollama): el endpoint manda; las CLI lo dejan vacío.
                'endpoint_url': _limpio(s.get('endpoint_url'), _TEXTO['endpoint_url']),
                'endpoint_model': _limpio(s.get('endpoint_model'), _TEXTO['endpoint_model']),
                'persona': _limpio(s.get('persona'), None),
                'recordatorio': _limpio(s.get('recordatorio'), None),
                'especialidad': _limpio(s.get('especialidad'), _TEXTO['especialidad']),
                'rol_tarjeta': _limpio(s.get('rol_tarjeta'), _TEXTO['rol_tarjeta']),
                'color_ui': color_limpio(s.get('color_ui')),
                'avatar': avatar_limpio(s.get('avatar')),
                'activo': bool(s.get('activo')),
                'orden': s.get('orden') if isinstance(s.get('orden'), int) else 0,
            }
            if cliente == 'ollama' and not campos['endpoint_url']:
                rep['avisos'].append(
                    _("«%(n)s»: silla local sin endpoint_url, salteada.") % {'n': nombre})
                continue

            keys_archivo.add(key)
            # [1] = «creada». No desempaquetar con `_` acá: en este módulo `_` es gettext.
            creada = Participante.objects.update_or_create(key=key, defaults=campos)[1]
            rep['creadas' if creada else 'actualizadas'] += 1

        if reemplazar and keys_archivo:
            sobrantes = Participante.objects.exclude(key__in=keys_archivo)
            rep['borradas'] = sobrantes.count()
            sobrantes.delete()

        av = data.get('avatares')
        if isinstance(av, dict):
            esp = AvataresEnjambre.get()
            esp.enjambre = avatar_limpio(av.get('enjambre'))
            esp.humano = avatar_limpio(av.get('humano'))
            esp.color_enjambre = color_limpio(av.get('color_enjambre'))
            esp.color_humano = color_limpio(av.get('color_humano'))
            esp.save()

    return rep
