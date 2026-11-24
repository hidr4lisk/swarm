#!/usr/bin/env python3
"""Punto de entrada estándar de Django para Hidr4lisk_Swarm."""
import os
import sys


def main():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swarm.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. ¿Está instalado y activado el entorno virtual? "
            "(pip install -r requirements.txt)"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
