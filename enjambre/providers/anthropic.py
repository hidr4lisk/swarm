"""
providers/anthropic.py — cliente de la Messages API de Anthropic (Claude) por API key.

Raw HTTP con urllib (ver providers/__init__.py para el porqué de no usar el SDK). Formato de la
Messages API verificado con el skill `claude-api` (2026-07): endpoint `/v1/messages`, header
`anthropic-version: 2023-06-01`, auth por `x-api-key`. El pensamiento va APAGADO por defecto en
Opus 4.8 (no mandamos `thinking`) → respuesta de texto directa, que es lo que la mesa consume.

`model` vacío ('' = default del desplegable) cae a Opus 4.8. `max_tokens` es tope duro por turno
(4096 alcanza para un turno de charla; F3/toolbelt lo revisará si hace falta más).
"""
from . import _http_json

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'
DEFAULT_MODEL = 'claude-opus-4-8'
MAX_TOKENS = 4096


def chat(model, prompt, api_key, timeout, base_url=''):
    headers = {
        'x-api-key': api_key,
        'anthropic-version': API_VERSION,
        'content-type': 'application/json',
    }
    payload = {
        'model': model or DEFAULT_MODEL,
        'max_tokens': MAX_TOKENS,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    ok, data = _http_json(API_URL, payload, headers, timeout)
    if not ok:
        return data  # ya es un marcador (❌ …)
    # El clasificador de seguridad puede rechazar con HTTP 200 + stop_reason:"refusal" (content vacío).
    if data.get('stop_reason') == 'refusal':
        return '(❌ el modelo rechazó el pedido por políticas de seguridad)'
    parts = [b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text']
    return '\n'.join(p for p in parts if p).strip() or '(sin respuesta)'


def chat_agentic(model, prompt, api_key, timeout, sesion, participante, system=''):
    """Loop de tool-use del toolbelt (F3): la silla inspecciona/opera la máquina real vía las
    herramientas, hasta que deja de pedirlas o se agotan las rondas. Devuelve el texto final."""
    from . import _http_json
    from .. import toolbelt
    headers = {'x-api-key': api_key, 'anthropic-version': API_VERSION, 'content-type': 'application/json'}
    tools = toolbelt.tools_anthropic()
    messages = [{'role': 'user', 'content': prompt}]
    for _ in range(toolbelt.MAX_ROUNDS):
        payload = {'model': model or DEFAULT_MODEL, 'max_tokens': MAX_TOKENS,
                   'messages': messages, 'tools': tools}
        if system:
            payload['system'] = system
        ok, data = _http_json(API_URL, payload, headers, timeout)
        if not ok:
            return data
        if data.get('stop_reason') == 'refusal':
            return '(❌ el modelo rechazó el pedido por políticas de seguridad)'
        content = data.get('content', [])
        usos = [b for b in content if b.get('type') == 'tool_use']
        if not usos:  # terminó: sin más herramientas → texto final
            parts = [b.get('text', '') for b in content if b.get('type') == 'text']
            return '\n'.join(p for p in parts if p).strip() or '(sin respuesta)'
        messages.append({'role': 'assistant', 'content': content})  # preservar los tool_use
        resultados = []
        for u in usos:
            res = toolbelt.ejecutar_tool(u.get('name'), u.get('input', {}), sesion, participante)
            resultados.append({'type': 'tool_result', 'tool_use_id': u.get('id'), 'content': res})
        messages.append({'role': 'user', 'content': resultados})
    return '(⏹️ corté tras varias rondas de herramientas sin cerrar — pedile a la silla que resuma)'
