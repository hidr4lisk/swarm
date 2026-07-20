"""
conexiones_check — detecta qué CLIs tienen credenciales y persiste el resultado.

Lo corre el worker al arrancar (o vos a mano). Solo existencia: nunca lee ni loguea
contenido de credenciales.
"""
from django.core.management.base import BaseCommand

from enjambre.conexiones import CLIS, detectar, guardar_estado, ruta_creds


class Command(BaseCommand):
    help = "Detecta credenciales de los CLIs (existencia, jamás contenido) y guarda conexiones.json."

    def handle(self, *args, **opts):
        estados = detectar()
        guardar_estado(estados)
        for cli, ok in estados.items():
            simbolo = self.style.SUCCESS('✓') if ok else self.style.ERROR('✗')
            self.stdout.write(f"  {simbolo} {CLIS[cli]['nombre']}: {ruta_creds(cli)}")
