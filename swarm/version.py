"""
swarm/version.py — la versión que muestra la app (footer).

El valor lo escribe el workflow de release (`scripts/actualizar_version.py <tag>`) ANTES de
buildear el bundle, así el zip que se descarga sabe qué versión es. En un clone del repo o
corriendo desde el código queda en `dev`, que es la verdad: no salió de un release.

No se edita a mano.
"""
__version__ = 'v0.6.5'
