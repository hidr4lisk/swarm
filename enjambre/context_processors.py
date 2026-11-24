"""
enjambre/context_processors.py — datos que toda plantilla necesita.

Por ahora uno solo: la versión, que el footer de `base_swarm.html` muestra al lado del nombre.
Va como context processor y no como variable de cada vista porque el footer vive en la base:
si dependiera de que cada vista la pase, la primera que se olvide muestra el footer sin versión.
"""
from swarm.version import __version__


def version(request):
    return {'swarm_version': __version__}
