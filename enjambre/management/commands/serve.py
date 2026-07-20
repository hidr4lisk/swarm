"""
enjambre/management/commands/serve.py — arranque ÚNICO de Swarm (modo nativo / portátil).

`python manage.py serve` levanta TODO Swarm en un solo proceso, sin Docker: migra la DB, arranca
el worker del Enjambre en un hilo, abre el navegador y sirve la web (runserver threaded — cada SSE
vive en su hilo, sin gevent). Es lo que corre el launcher del pendrive (enjambre.sh / Enjambre.bat).

No hay caja ni cápsula: las sillas por API key hablan directo con el proveedor y, con el toolbelt
encendido, tanto ellas como las de CLI operan la máquina real. Sin API keys tampoco te quedás
afuera: la silla gratis (Chispa) charla sin configurar nada.
"""
import threading
import webbrowser

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Arranca Swarm entero en un proceso (migra + worker + web + navegador)."

    def add_arguments(self, parser):
        parser.add_argument('--host', default='127.0.0.1', help='Interfaz (default: 127.0.0.1).')
        parser.add_argument('--port', default='8799', help='Puerto (default: 8799).')
        parser.add_argument('--no-browser', action='store_true', help='No abrir el navegador.')
        parser.add_argument('--no-worker', action='store_true',
                            help='No arrancar el worker (solo la web).')

    def handle(self, *args, **o):
        host, port = o['host'], o['port']
        url = f'http://{host}:{port}/'

        # 1) DB al día (crea db.sqlite3 la primera vez).
        self.stdout.write('· Migrando la base…')
        call_command('migrate', interactive=False, verbosity=0)

        # 2) Worker del Enjambre en un hilo daemon (muere con el proceso). Es quien despacha las
        #    sillas; sin él la mesa encola pero nadie responde.
        if not o['no_worker']:
            def _worker():
                while True:
                    try:
                        call_command('enjambre_worker')
                        return
                    except SystemExit:
                        # El botón "reiniciar worker" hace sys.exit; en un hilo solo corta el hilo
                        # (no hay supervisor en modo portátil). Relanzamos SIEMPRE — antes se
                        # relanzaba una sola vez y al segundo reinicio la mesa quedaba muda.
                        self.stderr.write('· Worker pidió reinicio; relanzando…')
                    except Exception as e:  # noqa: BLE001
                        self.stderr.write(f'· Worker cayó: {e}')
                        return
            threading.Thread(target=_worker, daemon=True, name='enjambre-worker').start()
            self.stdout.write('· Worker del Enjambre arrancado (hilo).')

        # 3) Abrir el navegador cuando el server ya esté por levantar.
        if not o['no_browser']:
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()

        self.stdout.write(self.style.SUCCESS(f'\n  Swarm en {url}   (Ctrl-C para salir)\n'))
        # 4) Web (threaded, sin reloader: el reloader forkea y duplicaría el worker/navegador).
        call_command('runserver', f'{host}:{port}', use_reloader=False)
