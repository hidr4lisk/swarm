"""
providers/gemini.py — Gemini por API key (Google AI Studio), primera clase en la bóveda.

Google expone un endpoint OpenAI-compatible (/v1beta/openai/) que habla el protocolo de
chat/completions incluidas las tools → reusamos openai_compat con base fija, mismo patrón
que pollinations.py. La key es la de AI Studio (AIza…) y viaja como Bearer.

Se eligió este endpoint y NO la API nativa de Gemini (generateContent) a propósito: cero
código nuevo de protocolo, y function-calling ya mapeado — chat_agentic (toolbelt) anda igual
que con OpenAI/OpenRouter. Si algún día hace falta algo exclusivo de la API nativa (caching,
media), ahí se evalúa el cliente propio.
"""
from . import openai_compat

GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta/openai'
DEFAULT_MODEL = 'gemini-3.5-flash'


def chat(model, prompt, api_key, timeout, base_url=''):
    return openai_compat.chat(model or DEFAULT_MODEL, prompt, api_key, timeout,
                              base_url=GEMINI_BASE)


def chat_agentic(model, prompt, api_key, timeout, sesion, participante, system=''):
    return openai_compat.chat_agentic(model or DEFAULT_MODEL, prompt, api_key, timeout,
                                      sesion, participante, base_url=GEMINI_BASE, system=system)
