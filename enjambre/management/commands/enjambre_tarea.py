"""
enjambre/management/commands/enjambre_tarea.py — crear y correr una Tarea.

Dispatcha una silla a fabricar sobre un repo, SIEMPRE en un worktree aislado, y deja
la branch `enjambre/tarea-<id>` para revisión. No hace push ni PR.

    python manage.py enjambre_tarea \\
        --repo /ruta/al/repo --silla opencode \\
        --titulo "Agregar README" --ordenes "Creá un README.md con una línea."
"""
from django.core.management.base import BaseCommand

from enjambre.models import Participante, Tarea
from enjambre.workspace import diff_stat, ejecutar_tarea


class Command(BaseCommand):
    help = "Crea y ejecuta una Tarea del Enjambre en un worktree aislado."

    def add_arguments(self, parser):
        parser.add_argument('--repo', required=True, help='Repo git destino en disco.')
        parser.add_argument('--silla', required=True, help='key de la Participante que ejecuta.')
        parser.add_argument('--titulo', required=True)
        parser.add_argument('--ordenes', required=True, help='Instrucciones para el agente.')
        parser.add_argument('--base', default='HEAD', help='Ref base (default HEAD).')

    def handle(self, *args, **opts):
        silla = Participante.objects.filter(key=opts['silla'], activo=True).first()
        if not silla:
            self.stderr.write(f"No existe la silla activa '{opts['silla']}'.")
            return

        tarea = Tarea.objects.create(
            titulo=opts['titulo'], ordenes=opts['ordenes'],
            repo_path=opts['repo'], base_ref=opts['base'], participante=silla,
        )
        self.stdout.write(f"▶ Tarea #{tarea.pk} → {silla.nombre} en {opts['repo']}")
        ejecutar_tarea(tarea)
        tarea.refresh_from_db()

        self.stdout.write(f"\nestado: {tarea.estado}")
        ws = getattr(tarea, 'workspace', None)
        if ws and ws.commit_sha:
            self.stdout.write(f"branch: {ws.branch}  commit: {ws.commit_sha[:10]}")
            self.stdout.write("diff:\n" + diff_stat(tarea.repo_path, ws.base_commit))
        self.stdout.write("\n— salida del agente —\n" + (tarea.salida or "(vacía)"))
