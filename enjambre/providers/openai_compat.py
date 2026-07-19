"""
providers/openai_compat.py — cliente de cualquier API compatible con OpenAI (Chat Completions).

Cubre OpenAI y todo lo que hable su protocolo: Groq, DeepSeek, Together, LM Studio, etc. El
`base_url` es configurable (default OpenAI); en Swarm se fija con `settings.SWARM_OPENAI_BASE_URL`
si querés apuntar a otro proveedor compatible. Auth por `Authorization: Bearer`.

Raw HTTP con urllib (ver providers/__init__.py). `extra_headers` lo usa OpenRouter para sus
headers de atribución. Respuesta = `choices[0].message.content`.
"""
import json

from . import _http_json

DEFAULT_BASE = 'https://api.openai.com/v1'
DEFAULT_MODEL = 'gpt-5.2'
MAX_TOKENS = 4096


def _param_tokens(base_url):
    """Nombre del parámetro de tope de tokens. OpenAI real (sin base_url custom) rechaza
    `max_tokens` en los modelos actuales (o-*, gpt-5.*) con HTTP 400 y exige
    `max_completion_tokens`; los compatibles (Groq/DeepSeek/OpenRouter/LM Studio) siguen
    hablando `max_tokens`."""
    return 'max_completion_tokens' if not base_url else 'max_tokens'


def chat(model, prompt, api_key, timeout, base_url='', extra_headers=None, throttle_key=''):
    base = (base_url or DEFAULT_BASE).rstrip('/')
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        'model': model or DEFAULT_MODEL,
        _param_tokens(base_url): MAX_TOKENS,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    ok, data = _http_json(base + '/chat/completions', payload, headers, timeout,
                          throttle_key=throttle_key)
    if not ok:
        return data
    try:
        return (data['choices'][0]['message']['content'] or '').strip() or '(sin respuesta)'
    except (KeyError, IndexError, TypeError):
        return '(❌ respuesta inesperada del proveedor)'


def chat_agentic(model, prompt, api_key, timeout, sesion, participante,
                 base_url='', system='', extra_headers=None):
    """Loop de tool-use (function calling de OpenAI) del toolbelt (F3). Devuelve el texto final."""
    from .. import toolbelt
    base = (base_url or DEFAULT_BASE).rstrip('/')
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    tools = toolbelt.tools_openai()
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    for _ in range(toolbelt.MAX_ROUNDS):
        payload = {'model': model or DEFAULT_MODEL, _param_tokens(base_url): MAX_TOKENS,
                   'messages': messages, 'tools': tools}
        ok, data = _http_json(base + '/chat/completions', payload, headers, timeout)
        if not ok:
            return data
        try:
            msg = data['choices'][0]['message']
        except (KeyError, IndexError, TypeError):
            return '(❌ respuesta inesperada del proveedor)'
        llamadas = msg.get('tool_calls') or []
        if not llamadas:
            return (msg.get('content') or '').strip() or '(sin respuesta)'
        messages.append(msg)  # assistant con tool_calls
        for tc in llamadas:
            fn = tc.get('function', {})
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except (ValueError, TypeError):
                args = {}
            res = toolbelt.ejecutar_tool(fn.get('name'), args, sesion, participante)
            messages.append({'role': 'tool', 'tool_call_id': tc.get('id'), 'content': res})
    return '(⏹️ corté tras varias rondas de herramientas sin cerrar — pedile a la silla que resuma)'
