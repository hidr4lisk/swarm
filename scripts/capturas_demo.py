"""Siembra la mesa de DEMO que sale en la captura del README (docs/img/mesa.png).

Es una puesta en escena, pero de algo que la app hace tal cual: un pedido con /armar, el líder
que planifica y fabrica, el trabajador que revisa, y los dos commits del turno. Los textos son
los mismos de la captura anterior (junio) para que la doc no cambie de historia, solo de UI.
Están recortados para que la conversación entre completa en una pantalla de 1100px.

Corre con: manage.py shell < scripts/capturas_demo.py  (contra un data/ temporal, nunca el real,
y con el worker apagado: `serve --no-worker`, si no toma el mensaje humano y contesta de verdad).
El que dispara las capturas es `scripts/capturas_doc.py`; ahí está el instructivo completo.
"""
from datetime import timedelta

from django.utils import timezone

from enjambre.models import LogMesa, Mensaje, Participante, Sesion
from enjambre.topologia import Topologia

Mensaje.objects.all().delete()
LogMesa.objects.all().delete()
Sesion.objects.all().delete()


def silla(key, nombre, comando, color, orden):
    p, _ = Participante.objects.get_or_create(key=key, defaults={'nombre': nombre})
    p.nombre, p.comando, p.color_ui, p.activo, p.orden = nombre, comando, color, True, orden
    p.save()
    return p


claude = silla('claude', 'Claude Code', ['claude', '-p'], '#e06c75', 0)
opencode = silla('opencode', 'OpenCode', ['opencode', 'run'], '#98b4a6', 1)
Participante.objects.exclude(pk__in=[claude.pk, opencode.pk]).update(activo=False)

s = Sesion.objects.create(nombre='ordenar descargas', topologia=Topologia.LIDER, lider=claude)
s.participantes.set([claude, opencode])

t0 = timezone.now() - timedelta(minutes=6)


def msg(emisor, texto, part=None, tokens=0, costo=0, seg=0):
    m = Mensaje.objects.create(sesion=s, emisor=emisor, participante=part, texto=texto,
                               tokens=tokens, costo=costo)
    Mensaje.objects.filter(pk=m.pk).update(creado_at=t0 + timedelta(seconds=seg))


def log(nivel, texto, seg=0, detalle=''):
    lg = LogMesa.objects.create(sesion=s, nivel=nivel, texto=texto, detalle=detalle)
    LogMesa.objects.filter(pk=lg.pk).update(creado_at=t0 + timedelta(seconds=seg))


msg('Humano', '/armar háganlo: ordenar.sh en bash, con un modo --dry-run que muestre qué movería '
    'sin tocar nada', seg=0)

msg('Claude Code', """**Archivos tocados:** `ordenar.sh` (creado), `NOTAS.md` (actualizado).

- **`--dry-run`**: imprime `MOVER: archivo → carpeta/` sin llamar `mkdir` ni `mv`.
- **Colisiones**: si `foto.jpg` ya existe en `Imagenes/`, escribe `foto_1.jpg`. Solo en modo real.
- **Sin extensión o desconocida**: imprime `IGNORADO` con motivo y sigue. No falla en silencio.

@opencode, si ves algo que ajustar en la lógica de colisiones, sumá.""",
    claude, tokens=1027, costo=0.0057, seg=38)

msg('Claude Code', '📝 Guardado (commit 7a8b4df):\n NOTAS.md   |  8 ++++++\n ordenar.sh | 95 '
    '+++++++++++++++++++++++++++++++++++++++\n 2 files changed, 103 insertions(+)', claude, seg=39)

msg('OpenCode', """Revisé la lógica de colisiones y el `--dry-run`: ambos correctos.

Propongo dos adiciones menores: carpeta `Videos/` (mp4, avi, mkv) y `Otros/` para extensiones sin
categoría, en vez de ignorarlas. Dejé el pendiente escrito en `NOTAS.md` para cuando decidas.""",
    opencode, tokens=1236, costo=0.0059, seg=87)

msg('OpenCode', '📝 Guardado (commit 7cef633):\n NOTAS.md | 3 ++-\n 1 file changed, 2 '
    'insertions(+), 1 deletion(-)', opencode, seg=88)

log('info', '📥 turno tomado: «Tengo la carpeta de Descargas hecha un caos. Quiero un script que la o»', 0)
log('paso', '▶ Claude Code · charla (timeout 180s)', 1)
log('ok', '✓ Claude Code respondió (12.7s · ~695 tok · $0.0060)', 13)
log('paso', '▶ OpenCode · charla (timeout 180s)', 14)
log('ok', '✓ OpenCode respondió (6.5s · ~735 tok · $0.0029)', 20)
log('ok', '🏁 turno completo (19.2s)', 20)
log('info', '📥 turno tomado: «/armar háganlo: ordenar.sh en bash, con un modo --dry-run que muestre »', 30)
log('paso', '▶ Claude Code · fabrica en la mesa (timeout 600s)', 31)
log('ok', '✓ Claude Code respondió (37.5s · ~1027 tok · $0.0057)', 38)
log('ok', '✓ commit 7a8b4df · Claude Code', 39, detalle=' ordenar.sh | 95 +++++++++++++++++')
log('paso', '▶ OpenCode · fabrica en la mesa (timeout 600s)', 40)
log('ok', '✓ OpenCode respondió (29.2s · ~1236 tok · $0.0059)', 87)
log('ok', '✓ commit 7cef633 · OpenCode', 88, detalle=' NOTAS.md | 3 ++-')
log('ok', '🏁 turno completo (66.9s)', 88)

print('mesa demo:', s.pk, '· sillas activas:', Participante.objects.filter(activo=True).count())
