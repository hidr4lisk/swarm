"""
providers/openrouter.py — cliente de OpenRouter (agregador; incluye modelos `:free`).

OpenRouter habla el protocolo de OpenAI, así que reusamos openai_compat con su base_url y los
headers de atribución que OpenRouter recomienda (opcionales pero prolijos). El interés de tener
OpenRouter en Swarm es su catálogo `:free` — sillas gratis por API key, en línea con el candado
"solo gratis" del Enjambre.
"""
from . import openai_compat

OPENROUTER_BASE = 'https://openrouter.ai/api/v1'
DEFAULT_MODEL = 'deepseek/deepseek-chat-v3.1:free'


_ATTR = {'HTTP-Referer': 'https://hidralisk.online', 'X-Title': 'Swarm'}


def chat(model, prompt, api_key, timeout, base_url=''):
    return openai_compat.chat(
        model or DEFAULT_MODEL, prompt, api_key, timeout,
        base_url=OPENROUTER_BASE, extra_headers=_ATTR,
    )


def chat_agentic(model, prompt, api_key, timeout, sesion, participante, base_url='', system=''):
    return openai_compat.chat_agentic(
        model or DEFAULT_MODEL, prompt, api_key, timeout, sesion, participante,
        base_url=OPENROUTER_BASE, system=system, extra_headers=_ATTR,
    )
