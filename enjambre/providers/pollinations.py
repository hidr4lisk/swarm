"""
providers/pollinations.py — cliente de Pollinations.AI (proveedor por API key).

Pollinations habla el protocolo de OpenAI, así que reusamos openai_compat con su base_url (el path
anidado /openai/chat/completions). Es un proveedor por token como cualquier otro api-*: el token
sale de enter.pollinations.ai y viaja como Bearer.

⚠️ HISTÓRICO: hasta el 2026-07-20 Pollinations tenía un tier ANÓNIMO sin key (era el escalón 0 de
la escalera, la silla «Chispa»). Ese tier MURIÓ: pasó a créditos «pollen» y todo request sin saldo
devuelve HTTP 402 "budget too low" (con o sin `model`, con o sin auth — no es por IP). Por eso
Chispa se retiró y Pollinations quedó como un proveedor por key más. NO re-proponer el tier
anónimo: verificado muerto en vivo.

Gotcha a favor: Pollinations CACHEA payloads idénticos (misma respuesta, mismo `created`) — el
retry del mismo request tras un fallo transitorio puede volver del cache. Ojo al depurar: un 200
inesperado puede ser cache viejo, no que el request nuevo haya pasado.
"""
from . import openai_compat

POLLINATIONS_BASE = 'https://text.pollinations.ai/openai'
DEFAULT_MODEL = 'openai-fast'

# Atribución (recomendada por Pollinations) + User-Agent propio: el default de urllib
# («Python-urllib/3.x») lo rechaza el filtro anti-bot con HTTP 403 (verificado 2026-07-19).
_ATTR = {'Referer': 'https://github.com/hidr4lisk/swarm', 'User-Agent': 'Swarm'}


def chat(model, prompt, api_key, timeout, base_url=''):
    # throttle_key: espaciar los requests (rate limit de Pollinations) — ver enjambre/ratelimit.py.
    return openai_compat.chat(
        model or DEFAULT_MODEL, prompt, api_key, timeout,
        base_url=POLLINATIONS_BASE, extra_headers=_ATTR, throttle_key='pollinations',
    )


def chat_agentic(model, prompt, api_key, timeout, sesion, participante):
    """Con el toolbelt: Pollinations es un api más (el openai-fast soporta tool-use). Mismo loop de
    function-calling que openai_compat, apuntado a la base_url y headers de Pollinations."""
    from .. import toolbelt
    return openai_compat.chat_agentic(
        model or DEFAULT_MODEL, prompt, api_key, timeout, sesion, participante,
        base_url=POLLINATIONS_BASE, system=toolbelt.system_prompt(), extra_headers=_ATTR,
    )
