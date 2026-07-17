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
import ssl
import urllib.error
import urllib.request


def _ssl_context():
    """Contexto SSL con el bundle de CA de certifi. El Python portátil de Windows NO trae los
    certificados CA del sistema → sin esto, TODO HTTPS a un proveedor falla con
    CERTIFICATE_VERIFY_FAILED ('unable to get local issuer certificate'). En Linux certifi también
    sirve; si por lo que sea no está, cae al default del sistema."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — sin certifi, usamos el store del sistema (Linux)
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _http_json(url, payload, headers, timeout):
    """POST JSON → (ok, data | marcador_error). Nunca lanza: degrada a un marcador `(❌ …)`."""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
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
    except UnicodeEncodeError:
        # Un header (típicamente la API key) tiene un carácter no-latin1 → mensaje claro en vez del
        # críptico "latin-1 codec can't encode". La validación del vault ya lo previene al guardar.
        return False, "(❌ API key inválida: tiene caracteres raros — revisala en Conexiones → API keys)"
    except Exception as e:  # noqa: BLE001 — cualquier fallo del proveedor es ruido
        return False, f"(❌ error: {e})"


def _es_free_id(mid):
    m = (mid or '').lower()
    return m.endswith(':free') or m.endswith('-free')


def _http_get_json(url, headers, timeout=12):
    req = urllib.request.Request(url, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def listar_modelos(provider, api_key='', base_url=''):
    """Modelos REALES del proveedor por API. Devuelve (source, [{'id','free'}], nota).
    source='live' si se pudo traer; 'error' + nota si no (el caller cae a la lista curada).
    OpenRouter no necesita key (endpoint público con pricing → flag free). OpenAI/Anthropic sí."""
    try:
        if provider == 'openrouter':
            data = _http_get_json('https://openrouter.ai/api/v1/models', {'User-Agent': 'Swarm'})
            out = []
            for m in data.get('data', []):
                mid = m.get('id', '')
                p = m.get('pricing', {}) or {}
                free = _es_free_id(mid) or (
                    str(p.get('prompt', '1')) in ('0', '0.0')
                    and str(p.get('completion', '1')) in ('0', '0.0'))
                # `tools` en supported_parameters = el modelo soporta function-calling → sirve para
                # el toolbelt. Si el modelo no lo soporta, la silla ignora las herramientas.
                sp = m.get('supported_parameters') or []
                out.append({'id': mid, 'free': bool(free), 'tools': 'tools' in sp})
            return 'live', out, ''
        if provider == 'openai':
            if not api_key:
                return 'error', [], 'desbloqueá la bóveda para listar los modelos con tu key'
            base = (base_url or 'https://api.openai.com/v1').rstrip('/')
            data = _http_get_json(base + '/models', {'Authorization': 'Bearer ' + api_key})
            # OpenAI /models no informa capacidades → no afirmamos tools (None = desconocido).
            return 'live', [{'id': m.get('id', ''), 'free': _es_free_id(m.get('id', '')), 'tools': None}
                            for m in data.get('data', [])], ''
        if provider == 'anthropic':
            if not api_key:
                return 'error', [], 'desbloqueá la bóveda para listar los modelos con tu key'
            data = _http_get_json('https://api.anthropic.com/v1/models',
                                  {'x-api-key': api_key, 'anthropic-version': '2023-06-01'})
            # Todos los modelos Claude soportan tool-use.
            return 'live', [{'id': m.get('id', ''), 'free': False, 'tools': True}
                            for m in data.get('data', [])], ''
    except Exception as e:  # noqa: BLE001 — red/SSL/parse → el caller muestra la lista curada
        return 'error', [], str(e)
    return 'error', [], 'este proveedor no tiene listado en vivo'


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
