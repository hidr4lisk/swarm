"""
enjambre/management/commands/enjambre_worker.py — el worker del Enjambre.

Corre aparte de `web` (en el compose es su propio contenedor, con el docker.sock; en dev,
otra terminal): `web` solo encola (Mensaje/Tarea) y streamea por SSE; este worker hace el
dispatch real de los CLIs y escribe las respuestas en la DB.

Con ENJAMBRE_RUNNER apuntando a runner/enjambre-run.sh los CLIs corren en contenedores
descartables; vacío = directo del PATH (dev). Ver README y docker-compose.yml.

Procesa cada pasada:
  1) Tareas en estado 'pendiente' → ejecutar_tarea (worktree aislado → commit → branch).
  2) Sesiones cuyo último mensaje es del humano (participante nulo) → las sillas responden.
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections

import sys

from enjambre.engine import Enjambre
from enjambre.models import Sesion, Tarea, Topologia, WorkerRestart
from enjambre.workspace import ejecutar_tarea


class Command(BaseCommand):
    help = "Worker del Enjambre (host): ejecuta Tareas pendientes y responde preguntas."

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Una sola pasada y salir (test).')
        parser.add_argument('--intervalo', type=float, default=3.0, help='Segundos entre pasadas.')

    def handle(self, *args, **opts):
        if not getattr(settings, 'ENJAMBRE_RUNNER', ''):
            self.stdout.write(self.style.WARNING(
                "ENJAMBRE_RUNNER vacío: los CLIs se invocan directo del PATH (modo dev)."))
        self.stdout.write(self.style.SUCCESS(
            "Worker del Enjambre arrancado." + (" (una pasada)" if opts['once'] else "")))
        while True:
            try:
                n = self._tick()
                if n:
                    self.stdout.write(f"  procesados: {n}")
            except Exception as e:  # noqa: BLE001 — un fallo de pasada no debe matar el worker
                self.stderr.write(f"  tick error: {e}")
                # Si la DB se reinició (deploy, restart de compose), la conexión queda muerta y
                # SIN esto cada tick siguiente falla con "connection already closed" para siempre
                # (mesas mudas hasta reiniciar el worker a mano). Cerrar fuerza reconexión limpia.
                connections.close_all()
            if opts['once']:
                break
            time.sleep(opts['intervalo'])

    def _tick(self):
        n = 0
        # -1) Pedido de REINICIO del worker (web→worker): recargar código nuevo. Borramos la fila
        #     ANTES de salir para que el worker relanzado no vuelva a reiniciarse en bucle. Al
        #     salir, el supervisor (compose `restart: unless-stopped`; systemd en dev) lo levanta
        #     de nuevo con el código fresco.
        if WorkerRestart.objects.exists():
            sol = (WorkerRestart.objects.order_by('creado_at').values_list('solicitante', flat=True)
                   .first() or '')
            WorkerRestart.objects.all().delete()
            self.stdout.write(self.style.WARNING(
                f"  ♻️ Reinicio solicitado ({sol}) — saliendo; el supervisor relanza el worker."))
            sys.exit(0)

        # 1) Tareas de fabricación pendientes. Las HUÉRFANAS (participante nulo: la silla se
        #    borró tras encolarse, SET_NULL) NO se pueden ejecutar y, si se colaran al loop,
        #    reventaban el tick en bucle (AttributeError sobre .participante) dejando MUDAS todas
        #    las mesas. Se marcan error y se sacan de la cola.
        huerfanas = Tarea.objects.filter(
            estado=Tarea.Estado.PENDIENTE, participante__isnull=True)
        for tarea in huerfanas:
            tarea.estado = Tarea.Estado.ERROR
            tarea.salida = "(❌ tarea huérfana: la silla asignada se borró; reasignala y reencolá)"
            tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
            self.stderr.write(f"  ✗ Tarea #{tarea.pk} huérfana (sin silla) → error")
            n += 1
        for tarea in Tarea.objects.filter(
                estado=Tarea.Estado.PENDIENTE, participante__isnull=False):
            self.stdout.write(f"  ▶ Tarea #{tarea.pk} ({tarea.participante.key}): {tarea.titulo}")
            ejecutar_tarea(tarea)
            tarea.refresh_from_db()
            self.stdout.write(f"    estado: {tarea.estado}")
            n += 1

        # 2) Preguntas sin responder. Antes se miraba SOLO el tail; si el humano escribía DURANTE
        #    un turno largo, al terminar el tail era de una silla y su mensaje quedaba enterrado
        #    para siempre. Ahora: el ÚLTIMO mensaje humano (participante nulo, no-sistema) con
        #    id > watermark dispara un turno. El watermark se sube ANTES de responder, así un
        #    mensaje posteado durante el turno tiene id mayor y lo agarra el próximo tick (no se
        #    pierde, no se re-procesa el mismo).
        atendidas = set()
        for sesion in Sesion.objects.filter(activa=True):
            ultimo_humano = (sesion.mensajes
                             .filter(participante__isnull=True, es_sistema=False)
                             .order_by('-id').first())
            if ultimo_humano and ultimo_humano.id > sesion.ultimo_humano_respondido:
                Sesion.objects.filter(pk=sesion.pk).update(
                    ultimo_humano_respondido=ultimo_humano.id)
                sesion.ultimo_humano_respondido = ultimo_humano.id
                self.stdout.write(f"  ▶ sesión #{sesion.pk}: respondiendo a «{ultimo_humano.texto[:50]}»")
                self._responder(sesion, ultimo_humano.texto)
                atendidas.add(sesion.pk)
                n += 1

        # 3) Modo --auto: sesiones que iteran SOLAS hacia su objetivo (sin que el
        #    humano dispare cada /seguí). El engine (auto_paso) chequea los límites (tope de costo,
        #    máx de iteraciones) y el freno /alto; cada tick avanza UNA iteración. Saltea las que
        #    ya atendieron un mensaje humano este tick (no doble-iterar).
        for sesion in Sesion.objects.filter(activa=True, continuo=True, auto=True):
            if sesion.pk in atendidas:
                continue
            self.stdout.write(f"  🤖 sesión #{sesion.pk}: auto-iteración (objetivo activo)")
            try:
                Enjambre(sesion).auto_paso()
            except Exception as e:  # noqa: BLE001 — un fallo no debe tumbar el tick
                self.stderr.write(f"    auto error #{sesion.pk}: {e}")
            n += 1
        return n

    def _responder(self, sesion, texto):
        enj = Enjambre(sesion)
        # Turno fresco: limpiar cualquier /alto viejo (de cuando la mesa estaba quieta) para que no
        # aborte este turno antes de empezar. Si el humano tira /alto DURANTE el turno, la web prende
        # el flag después de esta limpieza y el engine lo ve entre sillas.
        enj.limpiar_alto()
        enj.log(f"📥 turno tomado: «{texto[:70]}»", nivel='info')
        t0 = time.monotonic()
        # Modo líder: el líder reparte subtareas, las sillas ejecutan y el líder integra.
        # liderar() ya degrada solo (sin líder → plana; @mención → solo esa silla).
        if sesion.topologia == Topologia.LIDER and sesion.lider_id:
            enj.liderar(texto)
        else:
            enj.responder(texto)              # plana (o mención): responder respeta el @
        enj.log(f"🏁 turno completo ({time.monotonic() - t0:.1f}s)", nivel='ok')
