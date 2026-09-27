"""
enjambre/management/commands/enjambre_worker.py — el worker del Enjambre.

Reparto de tareas: `web` solo encola (Mensaje/Tarea) y streamea por SSE; este worker hace el
dispatch real de los CLIs y escribe las respuestas en la DB. Con `manage.py serve` corre en un
hilo del mismo proceso; también se puede levantar suelto en otra terminal.

Los CLIs se invocan directo del PATH de la máquina (`resolver_bin` los encuentra aunque la
shell no haya cargado su rc). Ver README.

Procesa cada pasada:
  1) Tareas en estado 'pendiente' → ejecutar_tarea (worktree aislado → commit → branch).
  2) Sesiones cuyo último mensaje es del humano (participante nulo) → las sillas responden.
     Con SWARM_WORKER_PARALELO > 1, varias mesas a la vez (ver `_responder_paralelo`).
"""
import concurrent.futures
import os
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections

import sys

from enjambre.engine import Enjambre
from enjambre.models import Sesion, Tarea, Topologia, WorkerRestart
from enjambre.workspace import ejecutar_tarea


class _LockedStream:
    """Envuelve `self.stdout`/`self.stderr` (OutputWrapper de Django) para que `.write()` sea
    atómico entre hilos. Solo se usa con SWARM_WORKER_PARALELO > 1: sin esto, dos turnos
    escribiendo a la vez entremezclan líneas a mitad de escritura."""

    def __init__(self, inner, lock):
        self._inner = inner
        self._lock = lock

    def write(self, *args, **kwargs):
        with self._lock:
            return self._inner.write(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


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
        #    pierde, no se re-procesa el mismo). Si el turno falla, el watermark NO se revierte
        #    (sería un reintento infinito de un pedido que rompe): `_responder` atrapa el error y
        #    lo postea en la mesa para que el humano lo vea y decida.
        #
        #    SWARM_WORKER_PARALELO (default 1, ver `_paralelo()`): con 1 este bloque es EXACTAMENTE
        #    el de siempre — secuencial, sin hilos. Con N > 1 se paraleliza SOLO este paso (Tareas
        #    y --auto siguen secuenciales). Regla que no se negocia: como mucho UN turno en vuelo
        #    por silla (dos turnos de la misma silla se comen la cuota/rate limit de su login o
        #    su key entre ellos). Si alguna silla de una mesa ya quedó reservada en ESTE tick, la
        #    mesa entera espera al siguiente sin mover su watermark — conservador a propósito.
        atendidas = set()
        paralelo = self._paralelo()
        if paralelo <= 1:
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
        else:
            # Selección: MISMO criterio que arriba (id > watermark), más la reserva por silla.
            # `Enjambre.sillas()` es la fuente real (∩ con las activas; vacío = ninguna).
            pendientes = []
            ocupadas = set()  # keys de Participante ya reservadas en ESTE tick
            for sesion in Sesion.objects.filter(activa=True):
                ultimo_humano = (sesion.mensajes
                                 .filter(participante__isnull=True, es_sistema=False)
                                 .order_by('-id').first())
                if not (ultimo_humano and ultimo_humano.id > sesion.ultimo_humano_respondido):
                    continue
                keys = {p.key for p in Enjambre(sesion).sillas()}
                if keys & ocupadas:
                    continue  # silla ocupada este tick → espera al siguiente, sin tocar watermark
                ocupadas |= keys
                Sesion.objects.filter(pk=sesion.pk).update(
                    ultimo_humano_respondido=ultimo_humano.id)
                sesion.ultimo_humano_respondido = ultimo_humano.id
                pendientes.append((sesion, ultimo_humano.texto))
                atendidas.add(sesion.pk)
            n += self._responder_paralelo(pendientes, paralelo)

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

    def _paralelo(self):
        """Techo de turnos EN VUELO del paso 2 (responder mesas). Default 1 = secuencial.
        Se lee al arrancar cada tick; cambiarlo es variable de entorno + reiniciar Swarm."""
        raw = (getattr(settings, 'SWARM_WORKER_PARALELO', '')
               or os.environ.get('SWARM_WORKER_PARALELO', '1'))
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _responder_paralelo(self, pendientes, n_paralelo):
        """Corre `pendientes` (lista de (sesion, texto), ya filtrada en `_tick` para que ninguna
        silla se repita en el lote) en hasta `n_paralelo` hilos a la vez. `_responder` ya atrapa
        y postea cualquier excepción del turno, así que un turno que explota no tumba al resto.

        Lo que sí es del worker y hay que cuidar acá:
          - stdout/stderr: envueltos con un lock mientras corre el lote.
          - conexiones a la DB: Django abre una por hilo y no la cierra sola → `close_all()` en
            el `finally` de cada hilo. SQLite aguanta los escritores concurrentes por el WAL +
            busy_timeout + IMMEDIATE de settings.py."""
        if not pendientes:
            return 0
        orig_stdout, orig_stderr = self.stdout, self.stderr
        lock = threading.Lock()
        self.stdout = _LockedStream(orig_stdout, lock)
        self.stderr = _LockedStream(orig_stderr, lock)
        try:
            def _trabajar(sesion, texto):
                try:
                    self.stdout.write(
                        f"  ▶ sesión #{sesion.pk}: respondiendo a «{texto[:50]}» (paralelo)")
                    self._responder(sesion, texto)
                finally:
                    connections.close_all()  # una conexión por hilo — SIEMPRE se cierra acá

            with concurrent.futures.ThreadPoolExecutor(max_workers=n_paralelo) as ex:
                futuros = [ex.submit(_trabajar, sesion, texto) for sesion, texto in pendientes]
                for f in concurrent.futures.as_completed(futuros):
                    f.result()  # _responder no propaga; esto solo re-lanzaría un bug del worker
        finally:
            self.stdout, self.stderr = orig_stdout, orig_stderr
        return len(pendientes)

    def _responder(self, sesion, texto):
        """Turno completo sobre un mensaje humano. Nunca propaga: si explota, el humano tiene que
        VERLO en la mesa. Antes la excepción subía al tick con el watermark ya avanzado → el
        mensaje quedaba marcado como respondido y la mesa muda, sin una sola pista (mesa 4)."""
        try:
            self._turno(sesion, texto)
        except Exception as e:  # noqa: BLE001
            self.stderr.write(f"    turno error #{sesion.pk}: {e}")
            try:
                enj = Enjambre(sesion)
                enj.guardar("Enjambre", f"(❌ el turno se cortó por un error: {e}). "
                                        f"El mensaje quedó sin responder — volvé a pedirlo.",
                            sistema=True)
                enj.log(f"✗ turno abortado: {e}", nivel='error')
            except Exception:  # noqa: BLE001 — si ni eso se puede guardar, ya está en stderr
                pass

    def _turno(self, sesion, texto):
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
