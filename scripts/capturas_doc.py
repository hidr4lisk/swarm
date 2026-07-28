#!/usr/bin/env python3
"""
scripts/capturas_doc.py — rehace las capturas de la app que muestran el README y la landing
(`docs/img/{conexiones,ayuda,ayuda-en,mesa}.png`).

Existen porque son doc que envejece en silencio: una captura vieja sigue mostrando una pantalla
que ya no existe (o peor, una promesa que dejó de ser cierta) y ningún test la mira.

Uso — con Swarm corriendo contra un `data/` LIMPIO y temporal, y el worker apagado:

    SP=$(mktemp -d)
    SWARM_DATA_DIR=$SP/data DATABASE_URL="sqlite:///$SP/db.sqlite3" \\
        python manage.py serve --no-browser --no-worker --port 8791 &
    SWARM_DATA_DIR=$SP/data DATABASE_URL="sqlite:///$SP/db.sqlite3" \\
        python manage.py shell < scripts/capturas_demo.py     # siembra la mesa de ejemplo
    uv run --with playwright python scripts/capturas_doc.py http://127.0.0.1:8791 docs/img

`data/` limpio no es un detalle: la captura tiene que mostrar lo que ve alguien que abre Swarm
por primera vez, no las sillas ni la bóveda de quien la saca.

Tres cosas que ya se pagaron una vez:
  · en la mesa, `wait_until='networkidle'` NUNCA se cumple (los dos SSE dejan la red abierta);
  · el `footer` es `position:fixed` → en una captura de página entera queda cruzado por el medio
    del contenido, hay que soltarlo a `static` antes de disparar;
  · el idioma se fija con la cookie `django_language` (lo mismo que deja el botón EN de la
    navbar al postear a `set_language`), no clickeando el botón.
"""
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8791').rstrip('/')
DEST = (sys.argv[2] if len(sys.argv) > 2 else 'docs/img').rstrip('/')
MESA = sys.argv[3] if len(sys.argv) > 3 else '/sesion/1/'

# (archivo, ruta, ancho, alto, ¿página entera?, idioma)
TOMAS = [
    ('conexiones.png', '/conexiones/', 1440, 900, False, 'es'),
    ('ayuda.png', '/ayuda/', 1440, 900, True, 'es'),
    ('ayuda-en.png', '/ayuda/', 1440, 900, True, 'en'),
    ('mesa.png', MESA, 1440, 1100, False, 'es'),
]


def main():
    with sync_playwright() as p:
        nav = p.chromium.launch()
        for archivo, ruta, ancho, alto, entera, lang in TOMAS:
            ctx = nav.new_context(viewport={'width': ancho, 'height': alto}, device_scale_factor=1)
            ctx.add_cookies([{'name': 'django_language', 'value': lang, 'url': BASE}])
            pag = ctx.new_page()
            pag.goto(BASE + ruta, wait_until='domcontentloaded')
            pag.wait_for_timeout(1800)          # markdown renderizado, avatares, estados
            if entera:
                pag.add_style_tag(content='footer{position:static !important;}')
                pag.wait_for_timeout(200)
            if ruta == MESA and pag.locator('#enj-main[data-flujo="closed"]').count():
                pag.click('#flujo-toggle')      # el log del worker es la mitad de esa imagen
                pag.wait_for_timeout(600)
            pag.screenshot(path=f'{DEST}/{archivo}', full_page=entera)
            print('✓', archivo)
            ctx.close()
        nav.close()


if __name__ == '__main__':
    main()
