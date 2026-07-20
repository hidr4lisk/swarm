#!/usr/bin/env python3
"""
scripts/actualizar_version_web.py — deja `index.html` (la landing de GitHub Pages) apuntando al
zip de la ÚLTIMA versión publicada, con la versión en el nombre del archivo.

Por qué existe: el asset del release se llama `swarm-portable_vX.Y.Z.zip` para que el que lo baja
sepa qué versión tiene en el pendrive. Pero eso rompe el link cómodo `releases/latest/download/
<nombre>`, que exige un nombre FIJO y predecible. Solución: el workflow corre esto después de
publicar el release y pushea la landing con la URL concreta de esa versión. Sin JS, sin consultar
la API de GitHub en runtime, sin CDN — coherente con el resto de la casa.

Uso:  python3 scripts/actualizar_version_web.py v0.3.3 [index.html]

Idempotente: si ya está en esa versión no toca nada (y el workflow no commitea).
"""
import re
import sys
from pathlib import Path

REPO = 'hidr4lisk/swarm'

# Matchea la URL del zip tanto en su forma vieja (`latest/download/swarm-portable.zip`) como en
# la nueva ya versionada — si no, el segundo release no encontraría qué reemplazar.
URL = re.compile(
    r'https://github\.com/' + re.escape(REPO) +
    r'/releases/(?:latest/download|download/v[\d.]+)/swarm-portable(?:_v[\d.]+)?\.zip'
)
# El nombre del archivo mostrado como texto (el `<a>` del paso 0 de «Arrancar»).
NOMBRE = re.compile(r'swarm-portable(?:_v[\d.]+)?\.zip')
# Las etiquetas del botón: ES en el HTML, EN en el diccionario I18N. El sufijo « · vX.Y.Z» se
# reemplaza entero para no ir acumulando versiones al re-correr.
CTA_ES = re.compile(r'(data-i18n="cta_download">⬇ DESCARGAR)[^<]*(</a>)')
CTA_EN = re.compile(r"(cta_download: '⬇ DOWNLOAD)[^']*(')")


def actualizar(texto, tag):
    """Devuelve el HTML apuntando a `tag`. `tag` viene como 'v0.3.3'."""
    zip_nuevo = f'swarm-portable_{tag}.zip'
    url_nueva = f'https://github.com/{REPO}/releases/download/{tag}/{zip_nuevo}'
    texto = URL.sub(url_nueva, texto)
    # Ojo con el orden: la URL ya quedó con el nombre nuevo adentro, así que este sub sobre el
    # nombre suelto es inofensivo (ya coincide) y arregla el texto visible del enlace.
    texto = NOMBRE.sub(zip_nuevo, texto)
    texto = CTA_ES.sub(rf'\1 · {tag}\2', texto)
    texto = CTA_EN.sub(rf'\1 · {tag}\2', texto)
    return texto


def main():
    if len(sys.argv) < 2 or not re.fullmatch(r'v[\d.]+', sys.argv[1]):
        sys.exit(f"uso: {sys.argv[0]} vX.Y.Z [index.html]")
    tag = sys.argv[1]
    ruta = Path(sys.argv[2] if len(sys.argv) > 2 else 'index.html')
    antes = ruta.read_text(encoding='utf-8')
    despues = actualizar(antes, tag)
    if antes == despues:
        print(f"index.html ya estaba en {tag} — sin cambios")
        return 0
    ruta.write_text(despues, encoding='utf-8')
    print(f"index.html actualizado a {tag}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
