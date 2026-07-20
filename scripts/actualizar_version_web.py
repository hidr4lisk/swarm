#!/usr/bin/env python3
"""
scripts/actualizar_version_web.py — escribe la versión publicada debajo del botón de descarga
de `index.html` (la landing de GitHub Pages).

Por qué existe: el botón apunta a `releases/latest/download/swarm-portable.zip`, que siempre
sirve la última versión pero no dice CUÁL. El nombre del asset se deja fijo a propósito (si
llevara la versión, esa URL cómoda dejaría de existir): la versión se muestra como texto.

Lo corre el workflow `release.yml` después de publicar el release, y pushea la landing. Sin JS
y sin consultar la API de GitHub en runtime — coherente con el resto de la casa.

Uso:  python3 scripts/actualizar_version_web.py v0.4.1 [index.html]

Idempotente: si ya está en esa versión no toca nada (y el workflow no commitea).
"""
import re
import sys
from pathlib import Path

# La línea que se reescribe. El texto de la derecha (SO y peso) se conserva tal cual esté.
LINEA = re.compile(r'(<p class="dl-ver" id="dl-ver">)v[\d.]+(\s*·)')


def actualizar(texto, tag):
    """Devuelve el HTML con `tag` debajo del botón. `tag` viene como 'v0.4.1'."""
    nuevo, n = LINEA.subn(rf'\g<1>{tag}\g<2>', texto)
    if not n:
        raise SystemExit("no encontré la línea <p class=\"dl-ver\" id=\"dl-ver\"> en el HTML")
    return nuevo


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
