#!/usr/bin/env python3
"""
scripts/actualizar_version.py — deja la versión publicada en los dos lugares que la muestran.

  1. `swarm/version.py`  → el footer de la app ("Hidr4lisk_Swarm v0.5.1").
  2. `index.html`        → la línea debajo del botón de descarga de la landing.

Lo corre el workflow de release. **El orden importa**: esto va ANTES de `build_bundle.sh`, así
el zip que se descarga lleva su versión adentro; el push de los dos archivos va al final, cuando
el release ya existe.

Por qué la versión es texto y no el nombre del asset: el botón de la web apunta a
`releases/latest/download/swarm-portable.zip`, que exige un nombre FIJO. Si el zip llevara la
versión en el nombre, esa URL se rompería en cada release.

Uso:  python3 scripts/actualizar_version.py v0.5.1
Idempotente: si ya está en esa versión no toca nada (y el workflow no commitea).
"""
import re
import sys
from pathlib import Path

VERSION_PY = re.compile(r"^__version__ = '[^']*'$", re.M)
DL_VER = re.compile(r'(<p class="dl-ver" id="dl-ver">)v[\d.]+(\s*·)')


def main():
    if len(sys.argv) < 2 or not re.fullmatch(r'v[\d.]+', sys.argv[1]):
        sys.exit(f"uso: {sys.argv[0]} vX.Y.Z")
    tag = sys.argv[1]
    raiz = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent
    tocados = []

    # 1) la versión que muestra la app
    ruta = raiz / 'swarm' / 'version.py'
    antes = ruta.read_text(encoding='utf-8')
    despues, n = VERSION_PY.subn(f"__version__ = '{tag}'", antes)
    if not n:
        sys.exit("no encontré __version__ en swarm/version.py")
    if despues != antes:
        ruta.write_text(despues, encoding='utf-8')
        tocados.append('swarm/version.py')

    # 2) la versión debajo del botón de descarga de la landing
    ruta = raiz / 'index.html'
    antes = ruta.read_text(encoding='utf-8')
    despues, n = DL_VER.subn(rf'\g<1>{tag}\g<2>', antes)
    if not n:
        sys.exit('no encontré la línea <p class="dl-ver" id="dl-ver"> en index.html')
    if despues != antes:
        ruta.write_text(despues, encoding='utf-8')
        tocados.append('index.html')

    print(f"{tag} → " + (", ".join(tocados) if tocados else "ya estaba, sin cambios"))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
