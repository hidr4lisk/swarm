"""
enjambre/providers/ — clientes HTTP de proveedores por API KEY (ruta portable de Swarm).

Las sillas `api:*` no usan binarios: el motor llama la API HTTP del proveedor con la key del
vault. Se implementan con **urllib de la stdlib** a propósito — no el SDK de Anthropic ni
`requests`: el bundle portátil (pendrive) tiene que ser liviano y de dependencias mínimas; la
única wheel nativa que arrastramos es `cryptography` (Fernet del vault). urllib cubre un POST
JSON de sobra. (Trade-off documentado en el threat model / README del empaquetado.)

F2 = charla plana (un mensaje `user` con el prompt ya armado por el engine, que incluye persona +
contexto + encuadre). El **loop de tool-use** sobre el sistema real llega en F3 (toolbelt).

`chat(provider, model, prompt, api_key, timeout, base_url='')` → texto de respuesta, o un
marcador `(❌ …)` que el engine trata como ruido y degrada (la silla queda muda, la mesa sigue).
Nunca lanza: cualquier fallo del proveedor es ruido, no rompe la mesa.
"""
import json
import urllib.error
import urllib.request


def _http_json(url, payload, headers, timeout):
    """POST JSON → (ok, data | marcador_error). Nunca lanza: degrada a un marcador `(❌ …)`."""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            err = json.loads(e.read()).get('error', '')
            detail = err.get('message', '') if isinstance(err, dict) else str(err)
        except Exception:  # noqa: BLE001 — el cuerpo del error puede no ser JSON
            pass
        return False, f"(❌ HTTP {e.code}{': ' + detail if detail else ''})"
    except urllib.error.URLError as e:
        return False, f"(❌ sin conexión: {getattr(e, 'reason', e)})"
    except Exception as e:  # noqa: BLE001 — cualquier fallo del proveedor es ruido
        return False, f"(❌ error: {e})"


def chat(provider, model, prompt, api_key, timeout, base_url=''):
    """Dispatcher: llama al cliente del proveedor. Devuelve el texto o un marcador de ruido."""
    if not (api_key or '').strip():
        return '(❌ sin API key para este proveedor — cargala en Conexiones → API keys, o desbloqueá la bóveda)'
    if provider == 'anthropic':
        from . import anthropic as p
        return p.chat(model, prompt, api_key, timeout)
    if provider == 'openai':
        from . import openai_compat as p
        return p.chat(model, prompt, api_key, timeout, base_url=base_url)
    if provider == 'openrouter':
        from . import openrouter as p
        return p.chat(model, prompt, api_key, timeout)
    return f"(❌ proveedor API desconocido: {provider})"


def chat_agentic(provider, model, prompt, api_key, timeout, sesion, participante, base_url=''):
    """Como chat() pero con el LOOP DE TOOL-USE del toolbelt (F3): la silla puede inspeccionar y
    operar la máquina real. El `system` OS-aware con las reglas lo arma toolbelt.system_prompt()."""
    if not (api_key or '').strip():
        return '(❌ sin API key para este proveedor — cargala en Conexiones → API keys, o desbloqueá la bóveda)'
    from .. import toolbelt
    system = toolbelt.system_prompt()
    if provider == 'anthropic':
        from . import anthropic as p
        return p.chat_agentic(model, prompt, api_key, timeout, sesion, participante, system=system)
    if provider == 'openai':
        from . import openai_compat as p
        return p.chat_agentic(model, prompt, api_key, timeout, sesion, participante,
                              base_url=base_url, system=system)
    if provider == 'openrouter':
        from . import openrouter as p
        return p.chat_agentic(model, prompt, api_key, timeout, sesion, participante, system=system)
    return f"(❌ proveedor API desconocido: {provider})"
