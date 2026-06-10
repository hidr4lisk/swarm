"""
enjambre/management/commands/enjambre.py — REPL del Enjambre (antes el script enjambre.py).

Driver local del núcleo, ya sobre el ORM. Crea/retoma una Sesion y deja operar la mesa
sin navegador (la capa web hace lo mismo vía el worker).

    python manage.py enjambre              # nueva sesión plana
    python manage.py enjambre --sesion 3   # retomar la sesión #3
"""
from django.core.management.base import BaseCommand

from enjambre.engine import Enjambre
from enjambre.models import Participante, Sesion, Topologia
from enjambre.topologia import despachar

COLOR_RESET = "\033[0m"


def _pintar(silla, texto, ronda=None):
    color = silla.color or ""
    cab = f"▌{silla.nombre}" + (f" (ronda {ronda})" if ronda else "") + ":"
    print(f"\n{color}{cab}{COLOR_RESET}\n{texto}")


class Command(BaseCommand):
    help = "REPL del Enjambre: mesa de trabajo multi-agente sobre el ORM."

    def add_arguments(self, parser):
        parser.add_argument('--sesion', type=int, default=None,
                            help='ID de una Sesion existente a retomar.')

    def handle(self, *args, **opts):
        if opts['sesion']:
            sesion = Sesion.objects.filter(pk=opts['sesion']).first()
            if not sesion:
                self.stderr.write(f"No existe la sesión #{opts['sesion']}.")
                return
        else:
            sesion = Sesion.objects.create(nombre='Mesa REPL')

        enjambre = Enjambre(sesion)
        sillas = {s.key: s for s in enjambre.sillas()}
        if not sillas:
            self.stderr.write("No hay sillas activas (Participante). Sembrá las migraciones primero.")
            return

        self._banner(sesion, sillas)
        while True:
            try:
                raw = input("\n👤 > ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n👋 El enjambre se repliega.")
                break
            if not raw:
                continue
            if raw == '/exit':
                print("\n👋 El enjambre se repliega.")
                break
            if raw == '/sillas':
                self._listar_sillas(sillas, sesion)
                continue
            if raw.startswith('/topologia '):
                self._set_topologia(enjambre, raw[len('/topologia '):].strip())
                continue
            if raw.startswith('/debate '):
                enjambre.debate(raw[len('/debate '):], on_respuesta=_pintar)
                continue
            if raw.startswith('/all '):
                despachar(enjambre, raw[len('/all '):], on_respuesta=_pintar)
                continue
            if raw.startswith('@'):
                key, _, text = raw[1:].partition(' ')
                silla = sillas.get(key)
                if not silla:
                    self.stderr.write(f"Sillas: {', '.join(sillas)}")
                    continue
                if not text.strip():
                    self.stderr.write("Usá: @silla <mensaje>")
                    continue
                enjambre.guardar("Humano", text)
                resp = enjambre.enviar(silla, text)
                _pintar(silla, resp)
                continue
            # Texto suelto → según topología (plana = broadcast).
            despachar(enjambre, raw, on_respuesta=_pintar)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _banner(self, sesion, sillas):
        print("=" * 55)
        print("  ENJAMBRE — Mesa de Trabajo")
        print(f"  Sesión #{sesion.pk} · topología: {sesion.topologia}")
        print(f"  Sillas: Tú | {' | '.join(s.nombre for s in sillas.values())}")
        print("=" * 55)
        print("  @<silla> <msg>      → mensaje directo")
        print("  /all <msg>          → según topología (plana = broadcast)")
        print("  /debate <msg>       → debate multi-ronda (plana)")
        print("  /topologia plana|lider [silla]  → cambiar coordinación")
        print("  /sillas             → listar sillas")
        print("  /exit               → salir")
        print("=" * 55)

    def _listar_sillas(self, sillas, sesion):
        for s in sillas.values():
            marca = "  ← líder" if sesion.lider_id == s.pk else ""
            print(f"  · {s.key:<10} {s.nombre} [{s.rol}]{marca}")

    def _set_topologia(self, enjambre, arg):
        partes = arg.split()
        modo = partes[0] if partes else ''
        if modo not in Topologia.values:
            self.stderr.write(f"Topologías: {', '.join(Topologia.values)}")
            return
        sesion = enjambre.sesion
        sesion.topologia = modo
        if modo == Topologia.LIDER:
            key = partes[1] if len(partes) > 1 else None
            lider = Participante.objects.filter(key=key, activo=True).first() if key else None
            if not lider:
                self.stderr.write("Modo líder requiere una silla válida: /topologia lider <silla>")
                return
            sesion.lider = lider
            print(f"  Topología → líder ({lider.nombre})")
        else:
            sesion.lider = None
            print("  Topología → plana")
        sesion.save()
