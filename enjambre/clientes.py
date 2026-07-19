"""
enjambre/clientes.py — registro de CLIENTES (CLIs + modelo local) del Enjambre.

Fuente única para el Panel de Sillas: cada cliente define la plantilla de `comando` (charla) y
`comando_trabajo` (fabricar/agéntico), si soporta `--model`, y una lista sugerida de modelos. La
vista deriva `Participante.comando` de (cliente + modelo) para que el usuario NO edite JSON a mano.

- claude/opencode soportan `--model`; **agy NO** (es setting interno de antigravity-cli).
- `ollama` no es un CLI: usa `endpoint_url` + `endpoint_model` (silla HTTP, ej. el Lab).

Los modelos sugeridos de opencode salieron de `opencode models` (verificados con PONG, 2026-06-03).
El campo de modelo en la UI es un datalist: sugiere, pero acepta cualquier ID (modelos futuros sin
tocar código).
"""

CLIENTES = {
    'claude': {
        'label': 'Claude (tu plan OAuth)',
        'comando': ['claude', '-p', '--output-format', 'text'],
        # Fabricar: acceptEdits auto-acepta ediciones; --allowedTools Bash habilita EJECUTAR (correr/
        # testear lo que arma) sin prompt y sin bypass (que claude prohíbe como root). OJO el orden:
        # --allowedTools es variádico → va ANTES de --permission-mode para no comerse el prompt.
        'comando_trabajo': ['claude', '-p', '--allowedTools', 'Bash', '--permission-mode', 'acceptEdits'],
        'model_flag': '--model',
        'modelos': ['', 'opus', 'sonnet', 'haiku'],  # '' = default del plan
    },
    'opencode': {
        'label': 'OpenCode (Zen)',
        'comando': ['opencode', 'run'],
        'comando_trabajo': ['opencode', 'run'],
        'model_flag': '--model',
        'modelos': [
            '',
            # Free (verificados)
            'opencode/big-pickle',
            'opencode/deepseek-v4-flash-free',
            'opencode/nemotron-3-super-free',
            'opencode/mimo-v2.5-free',
            'opencode/grok-build-0.1',
            # Populares (pagos por la key de Zen)
            'opencode/claude-sonnet-4-6',
            'opencode/claude-opus-4-8',
            'opencode/gpt-5.2',
            'opencode/gemini-3-flash',
            'opencode/kimi-k2.6',
            'opencode/glm-5',
            'opencode/qwen3.6-plus',
            'opencode/minimax-m2.7',
        ],
    },
    'agy': {
        'label': 'Antigravity (Gemini / Claude / GPT-OSS)',
        'comando': ['agy', '--sandbox', '-p'],
        'comando_trabajo': ['agy', '--sandbox', '-p'],
        # agy SÍ soporta --model (verificado 2026-06-09 con `agy models`). El valor es el nombre
        # tal cual lo lista `agy models`, entre comillas. OJO: en agy el prompt va INMEDIATAMENTE
        # después de -p, así que --model debe insertarse ANTES del -p (ver build_comando).
        'model_flag': '--model',
        'modelos': [
            '',  # default de antigravity (hoy Gemini)
            'Claude Sonnet 4.6 (Thinking)',
            'Claude Opus 4.6 (Thinking)',
            'GPT-OSS 120B (Medium)',
            'Gemini 3.5 Flash (Medium)',
            'Gemini 3.5 Flash (High)',
            'Gemini 3.1 Pro (Low)',
            'Gemini 3.1 Pro (High)',
        ],
    },
    # ── Proveedores por API KEY (sin binarios, ruta portable) ──────────────────
    # No son CLIs: el motor llama la API HTTP del proveedor con la key del vault (ver
    # providers/ + vault.py). `api` = nombre del proveedor para el dispatcher. El modelo se
    # guarda igual que en los CLIs (--model en `comando`), así cliente_de/modelo_de no cambian.
    'api-anthropic': {
        'label': 'Anthropic API (Claude)',
        'api': 'anthropic',
        'comando': ['api-anthropic'],
        'model_flag': '--model',
        'modelos': ['', 'claude-opus-4-8', 'claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-fable-5'],
    },
    'api-openai': {
        'label': 'OpenAI-compatible (API key)',
        'api': 'openai',
        'comando': ['api-openai'],
        'model_flag': '--model',
        'modelos': ['', 'gpt-5.2', 'gpt-5-mini', 'o4', 'deepseek-chat', 'llama-3.3-70b'],
    },
    'api-openrouter': {
        'label': 'OpenRouter (API key · incluye :free)',
        'api': 'openrouter',
        'comando': ['api-openrouter'],
        'model_flag': '--model',
        'modelos': [
            '',
            'deepseek/deepseek-chat-v3.1:free',
            'meta-llama/llama-3.3-70b-instruct:free',
            'qwen/qwen3-coder:free',
            'anthropic/claude-sonnet-5',
            'openai/gpt-5.2',
            'google/gemini-3-flash',
        ],
    },
    'api-gemini': {
        'label': 'Gemini (API key de Google AI Studio)',
        'api': 'gemini',
        'comando': ['api-gemini'],
        'model_flag': '--model',
        # Curada de respaldo; el modal trae la lista real en vivo con la key de la bóveda.
        'modelos': ['', 'gemini-3.5-flash', 'gemini-3.1-pro', 'gemini-3-flash'],
    },
    'api-pollinations': {
        'label': 'Pollinations (gratis, sin cuenta)',
        'api': 'pollinations',
        'sin_key': True,  # tier anónimo: funciona sin credencial (el token gratis acelera 3×)
        'comando': ['api-pollinations'],
        'model_flag': '--model',
        # Tier anónimo verificado 2026-07-19: UN modelo (openai-fast = GPT-OSS 20B), 1 req/15 s.
        'modelos': ['', 'openai-fast'],
    },
    'ollama': {
        'label': 'Modelo local (Ollama/HTTP)',
        'http': True,  # usa endpoint_url + endpoint_model, no comando
        'modelos': ['', 'qwen2.5:3b', 'qwen2.5:7b'],
    },
}


def es_api(cliente):
    """True si el cliente es un proveedor por API key (api-*)."""
    c = CLIENTES.get(cliente)
    return bool(c and c.get('api'))


def api_de(participante):
    """Nombre del proveedor API de la silla ('anthropic'|'openai'|'openrouter'), o '' si no es
    una silla por API key. Las sillas Ollama (endpoint_url) nunca son api."""
    if participante.endpoint_url:
        return ''
    return (CLIENTES.get(cliente_de(participante)) or {}).get('api', '')


def edita_archivos(participante):
    """True si la silla puede FABRICAR/editar archivos por CLI (subprocess con acceso al FS).
    Las sillas HTTP (Ollama) y las api:* NO editan: las primeras no tienen filesystem; las api:*
    en F2 son solo charla (el tool-use sobre el sistema real llega con el toolbelt en F3). El
    engine usa esto para no repartirles subtareas de /armar."""
    return not participante.endpoint_url and not api_de(participante)


def build_comando(cliente, modelo):
    """Devuelve (comando, comando_trabajo) para un cliente CLI + modelo opcional.
    Para clientes HTTP (ollama) devuelve ([], []) — esos van por endpoint_url/endpoint_model."""
    c = CLIENTES.get(cliente)
    if not c or c.get('http'):
        return [], []
    cmd = list(c['comando'])
    # Los proveedores API no fabrican por CLI → si no traen comando_trabajo, cae a `comando`.
    cmdt = list(c.get('comando_trabajo') or c['comando'])
    if c.get('model_flag') and modelo:
        flag = [c['model_flag'], modelo]
        if cliente == 'agy':
            # agy: el prompt se agrega después del -p final, así que el --model va ANTES del -p
            # (insertarlo después rompería: «-p --model X <prompt>» comería el flag como prompt).
            cmd = cmd[:-1] + flag + cmd[-1:]
            cmdt = cmdt[:-1] + flag + cmdt[-1:]
        else:
            cmd = cmd + flag
            cmdt = cmdt + flag
    return cmd, cmdt


# ── Tarifas para el VELOCÍMETRO ────────────────────────────────────
# USD por MILLÓN de tokens (in, out). Son NOTIONALES (precio de lista ~2026), NO la factura real:
# claude va por plan OAuth de tarifa plana y opencode por la key de Zen. Sirven de referencia para
# tener un velocímetro y poder ponerle un tope. Silla local (HTTP/Ollama) = $0 explícito.
PRECIOS_DEFAULT = (3.0, 15.0)  # ~clase Sonnet; fallback para clientes/modelos no tabulados
PRECIOS = {
    'claude': {
        '': (3.0, 15.0),        # default del plan ≈ Sonnet
        'opus': (15.0, 75.0),
        'sonnet': (3.0, 15.0),
        'haiku': (0.80, 4.0),
    },
}
# Precios por SUBSTRING del id de modelo, comunes a clientes proxy (opencode/Zen, agy): el id trae
# el nombre del modelo subyacente (ej 'opencode/claude-opus-4-8' → opus). Se evalúan en ORDEN, así
# que van de más específico a más genérico. Antes opencode no estaba tabulado y TODO caía al default
# (3/15): un opus-vía-opencode se contaba 5× barato.
PRECIOS_POR_MODELO = (
    ('opus',     (15.0, 75.0)),
    ('sonnet',   (3.0, 15.0)),
    ('haiku',    (0.80, 4.0)),
    ('gpt-5',    (1.25, 10.0)),
    ('gpt',      (1.25, 10.0)),
    ('gemini',   (1.25, 10.0)),   # ~clase Pro; las Flash son más baratas pero acotamos por arriba
    ('glm',      (0.60, 2.20)),
    ('kimi',     (0.60, 2.50)),
    ('qwen',     (0.40, 1.20)),
    ('minimax',  (0.30, 1.20)),
    ('deepseek', (0.30, 1.20)),
    ('nemotron', (0.30, 1.20)),
    ('grok',     (3.0, 15.0)),
)
# Modelos GRATIS de opencode/Zen (substrings en el id): no suman costo.
FREE_MARKERS = ('-free', 'big-pickle', 'grok-build')


def precio_silla(participante):
    """(precio_in, precio_out) en USD/millón de tokens para estimar el costo de un turno.
    Silla local (endpoint_url) y modelos free de opencode = (0, 0).

    NOTA: el costo del Enjambre es NOTIONAL y subestima en modo fabricar — los CLIs hacen rondas
    internas de tool-calls (leer/editar/correr) que no se ven en len(prompt+salida)/4. Tomalo como
    piso de referencia, no como factura. (Mejora futura: leer el usage real del JSON del CLI.)"""
    if participante.endpoint_url:
        return (0.0, 0.0)
    cli = cliente_de(participante)
    mod = (modelo_de(participante) or '').lower()
    if cli == 'opencode' and any(f in mod for f in FREE_MARKERS):
        return (0.0, 0.0)
    # Proveedores sin_key (Pollinations): gratis en ambos tiers (el token solo acelera). Sin esta
    # regla, 'openai-fast' no matchea ninguna tarifa y caería al default (3/15) — costo falso en
    # el velocímetro para la silla del escalón 0.
    if (CLIENTES.get(cli) or {}).get('sin_key'):
        return (0.0, 0.0)
    tabla = PRECIOS.get(cli)
    if tabla and mod in tabla:
        return tabla[mod]
    # Match por substring del id de modelo (cubre opencode/agy y claude --model con id largo).
    for marcador, precio in PRECIOS_POR_MODELO:
        if marcador in mod:
            return precio
    if tabla:  # cliente tabulado (claude) sin modelo reconocido → su default
        return tabla.get('', PRECIOS_DEFAULT)
    return PRECIOS_DEFAULT


def cliente_de(participante):
    """Cliente actual de una silla (para preseleccionar el desplegable). HTTP → 'ollama';
    si no, el binario en comando[0] si está en CLIENTES."""
    if participante.endpoint_url:
        return 'ollama'
    head = (participante.comando or [None])[0]
    return head if head in CLIENTES else (head or 'opencode')


def modelo_de(participante):
    """Modelo actual de una silla (para preseleccionar). HTTP → endpoint_model; si no, el valor
    que sigue a --model en comando (vacío si no hay)."""
    if participante.endpoint_url:
        return participante.endpoint_model
    cmd = participante.comando or []
    if '--model' in cmd:
        i = cmd.index('--model')
        if i + 1 < len(cmd):
            return cmd[i + 1]
    return ''
