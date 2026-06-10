"""
enjambre/topologia.py — seam de coordinación: PLANA ↔ LÍDER.

Router fino entre las dos topologías. La orquestación real vive en el motor
(`Enjambre.broadcast` para plana, `Enjambre.liderar` para líder: el líder descompone,
reparte a las sillas, integra y reporta). Acá solo se decide a cuál ir.

`despachar` es el entry del REPL (`manage.py enjambre`), que NO persiste el turno del
humano aparte → estas funciones lo guardan. El worker NO usa despachar: la web ya guardó
el mensaje, así que el worker llama directo a `enj.liderar`/`enj.responder` (sin re-guardar).
"""
from .models import Topologia


def despachar(enjambre, texto, on_respuesta=None):
    """Enruta un mensaje del REPL según la topología. Guarda el turno del humano y delega
    al motor. Devuelve dict {key: respuesta}."""
    if enjambre.sesion.topologia == Topologia.LIDER and enjambre.sesion.lider_id:
        enjambre.guardar("Humano", texto)
        return enjambre.liderar(texto, on_respuesta=on_respuesta)
    return enjambre.broadcast(texto, on_respuesta=on_respuesta)
