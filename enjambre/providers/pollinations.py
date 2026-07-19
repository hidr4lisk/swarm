"""
providers/pollinations.py — cliente de Pollinations.AI (tier anónimo: SIN API key).

Es el ESCALÓN 0 de la escalera de arranque: la silla que charla al primer arranque sin que el
usuario configure nada. Pollinations habla el protocolo de OpenAI, así que reusamos openai_compat
con su base_url (verificado 2026-07-19: el path anidado /openai/chat/completions devuelve 200, y
un `Authorization: Bearer ` vacío no molesta — no hace falta tocar openai_compat).

Tiers (verificados en vivo):
- Anónimo: 1 request / 15 s, UN solo modelo (`openai-fast` = GPT-OSS 20B). Sin cuenta.
- Registrado (token gratis de auth.pollinations.ai, sin tarjeta): 1 request / 5 s. El mismo
  código cubre ambos: si hay token en la bóveda, viaja como Bearer y listo.

Gotcha a favor: Pollinations CACHEA payloads idénticos (misma respuesta, mismo `created`) — el
retry del mismo request tras un fallo transitorio puede volver del cache, gratis y sin latencia.

SEGURIDAD — sin toolbelt: acá NO hay chat_agentic. Un endpoint de terceros no autenticado
dirigiendo ejecución de herramientas en la máquina del usuario es otro perfil de amenaza que una
key que vos controlás; el dispatcher (providers/__init__.py) degrada chat_agentic → chat() para
este proveedor. Si algún día se habilita, tiene que ser un opt-in separado y ruidoso.
"""
from . import openai_compat

POLLINATIONS_BASE = 'https://text.pollinations.ai/openai'
DEFAULT_MODEL = 'openai-fast'

# Atribución (recomendada por Pollinations) + User-Agent propio: el default de urllib
# («Python-urllib/3.x») lo rechaza el filtro anti-bot con HTTP 403 (verificado 2026-07-19).
_ATTR = {'Referer': 'https://github.com/hidr4lisk/swarm', 'User-Agent': 'Swarm'}


def chat(model, prompt, api_key, timeout, base_url=''):
    # api_key = '' en tier anónimo (el Bearer vacío está verificado inocuo); con token, tier seed.
    # throttle_key: espaciar 1 req/15 s (o /5 s con token) — ver enjambre/ratelimit.py.
    return openai_compat.chat(
        model or DEFAULT_MODEL, prompt, api_key, timeout,
        base_url=POLLINATIONS_BASE, extra_headers=_ATTR, throttle_key='pollinations',
    )
