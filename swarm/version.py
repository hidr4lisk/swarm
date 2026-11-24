"""
swarm/version.py — la versión que muestra la app (footer).

El valor lo sella el dueño con `scripts/actualizar_version.py <tag>` y lo commitea ANTES de
crear el tag; el workflow de release se niega a publicar un tag sin sellar. Así la app, el zip y
la landing (Pages sale de main) dicen la misma versión.

No se edita a mano.
"""
__version__ = 'v1.2.2'
