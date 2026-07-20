"""
enjambre/engine.py — motor del Enjambre.

Heredado del prototipo enjambre.py: dispatch de CLIs por subprocess, contexto
compartido y filtro de "ruido". Ahora persistido en el ORM (Sesion/Mensaje/Participante)
en vez de historia.jsonl. La topología (plana ↔ líder) se resuelve en topologia.py.

Nota Docker: con ENJAMBRE_RUNNER seteado los CLIs corren headless en contenedores
descartables (ver runner/); sin él se invocan directo del PATH. El dispatch acá es
el punto único que usa el worker.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

from django.conf import settings

from .models import Mensaje, Participante

MAX_CONTEXTO = 30  # últimos N mensajes (no-ruido) reinyectados como contexto
# Topes del contexto reinyectado (palanca de costo): sin esto, el líder re-lee toda la mesa en
# cada llamada y los tokens crecen sin techo a medida que la conversación se alarga.
CONTEXTO_MSG_MAX = 1500     # máx caracteres por mensaje (el completo sigue en el chat)
CONTEXTO_TOTAL_MAX = 12000  # presupuesto total de caracteres del contexto (~3k tokens)
AUTO_MAX_ITER = 15  # tope DURO de iteraciones del modo --auto (safety: corta aunque no haya tope $)

# Piso de timeout para turnos de FABRICAR (editar archivos de verdad). Fabricar tarda MUCHO más
# que charlar: el techo de charla (sesion.timeout, ~180s) mataba el subprocess a mitad y se perdía
# el trabajo ya escrito en disco (verificado en mesa 105: Saul y Jesse matados a 180s). Para
# fabricar usamos max(sesion.timeout, este piso). 10 min cubre tareas reales sin colgar el worker.
FABRICAR_TIMEOUT_MIN = 600

# Marcadores que SIEMPRE son ruido: los generamos NOSOTROS al degradar (ejecutar_cli/http),
# siempre como mensaje COMPLETO que ARRANCA así: «(❌ …)», «(⏰ timeout …)», «(sin respuesta)».
# Por eso cuentan solo como PREFIJO: una respuesta legítima puede traer ❌/⏰ en el medio —
# en la mesa 131 Jesse describió un scoreboard «✅❌📊» y su integración quedó marcada error.
RUIDO_PROPIO = ("(❌", "(⏰", "(sin respuesta)")
# Marcadores de error de PROVEEDOR (límites de sesión, JSON de error de opencode/Zen). Estos SÍ
# pueden aparecer como substring dentro de una respuesta legítima y larga — p.ej. una mesa de
# pentesting explicando "el endpoint tiene rate limiting". Por eso solo cuentan como ruido si la
# salida es CORTA (un error real es breve; un párrafo que los menciona, no). Ver mesa 127.
ERROR_MARKERS = ("session limit", "hit your", "unknownerror", "unexpected server error",
                 "internal server error", "rate limit", "overloaded", "bad request")
# Tope de largo para que un marker de proveedor cuente como ruido (errores reales son breves).
RUIDO_MAX_LEN = 300

# ── Limpieza de la salida de los CLIs ──────────────────────────────────────────
# Cada turno corre en un contenedor FRESCO (seed-copy efímero), así que algunos CLIs
# re-inicializan su estado y lo cuentan por stdout (opencode re-migra su DB interna y
# imprime su banner «> build · modelo»), con códigos ANSI incluidos. Nada de eso es la
# respuesta: se limpia ANTES de persistir. Visto en el primer beta real (mesa Test).
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
PREAMBULOS_CLI = (
    'performing one time database migration',
    'sqlite-migration:',
    'database migration complete',
    '> build ·', '> plan ·',
)
# El runner puede fallar ANTES de llegar al CLI (p.ej. silla activada sin tener el CLI
# instalado: el --mount corta con este error del daemon). Crudo asusta; se traduce a un
# marcador de ruido amigable que apunta a Conexiones.
DOCKER_MOUNT_ERROR = 'bind source path does not exist'


def limpiar_salida(texto):
    """Saca los códigos ANSI y el preámbulo de arranque del CLI (líneas conocidas, solo
    AL INICIO de la salida). El cuerpo de la respuesta no se toca."""
    t = ANSI_RE.sub('', texto or '')
    lineas = t.splitlines()
    i = 0
    while i < len(lineas):
        ln = lineas[i].strip().lower()
        if not ln or any(ln.startswith(p) for p in PREAMBULOS_CLI):
            i += 1
            continue
        break
    return '\n'.join(lineas[i:]).strip()


def es_ruido(texto):
    low = (texto or "").strip().lower()
    if low.startswith(RUIDO_PROPIO):
        return True
    if len(low) <= RUIDO_MAX_LEN and any(m in low for m in ERROR_MARKERS):
        return True
    return False


# ── Comandos de mesa (se tipean en el mensaje, sin botones) ──────────────────────
# Solo CONTROL los dispara; para CONSULTA se ignora el verbo y queda como charla.
TRIGGERS_BUILD = ('/armar', '/build', '🔨')  # fabricar/editar en la carpeta de la mesa (agéntico)
TRIGGER_UNDO = '/deshacer'                   # revertir el último turno de /armar (git reset)
TRIGGER_VOLVER = '/volver'                   # rebobinar la carpeta a un commit dado (rollback granular)
TRIGGER_DEBATE = '/debate'                   # N rondas donde las sillas se refutan (solo texto)
TRIGGER_ALTO = '/alto'                       # kill-switch: este turno NO dispatcha sillas
TRIGGER_CONTINUO = '/continuo'               # arranca el modo continuo con un objetivo
TRIGGERS_SEGUI = ('/seguí', '/segui')        # corre la próxima iteración hacia el objetivo
TRIGGER_AUTO = '/auto'                        # pasa un continuo en curso a automático
TRIGGER_CERRAR = '/cerrar'                     # cierra la mesa con un resumen (qué se hizo + costo)


def parse_comando(texto):
    """Devuelve (comando, texto_limpio). comando ∈ {'build','undo','debate','alto','continuo',
    'segui',None}. El verbo va al inicio del texto (ya sin @mención)."""
    t = (texto or '').lstrip()
    low = t.lower()
    for trig in TRIGGERS_BUILD:
        if low.startswith(trig):
            return 'build', t[len(trig):].lstrip()
    if low.startswith(TRIGGER_VOLVER):
        return 'volver', t[len(TRIGGER_VOLVER):].lstrip()
    if low.startswith(TRIGGER_UNDO):
        return 'undo', t[len(TRIGGER_UNDO):].lstrip()
    if low.startswith(TRIGGER_DEBATE):
        return 'debate', t[len(TRIGGER_DEBATE):].lstrip()
    if low.startswith(TRIGGER_ALTO):
        return 'alto', t[len(TRIGGER_ALTO):].lstrip()
    if low.startswith(TRIGGER_CONTINUO):
        return 'continuo', t[len(TRIGGER_CONTINUO):].lstrip()
    if low.startswith(TRIGGER_AUTO):
        return 'auto', t[len(TRIGGER_AUTO):].lstrip()
    if low.startswith(TRIGGER_CERRAR):
        return 'cerrar', t[len(TRIGGER_CERRAR):].lstrip()
    for trig in TRIGGERS_SEGUI:
        if low.startswith(trig):
            return 'segui', t[len(trig):].lstrip()
    return None, texto


# ── Prompts del modo LÍDER ────────────────────────────────────────────────────
def _prompt_plan(pedido, workers, editar):
    """Prompt con el que el líder descompone el pedido en subtareas dirigidas. El formato
    «@alias: subtarea» es el contrato que consume _parse_asignaciones."""
    roster = "\n".join(
        f"  @{w.alias} — {w.etiqueta}" + (f" — {w.especialidad}" if w.especialidad else "")
        for w in workers
    )
    if editar:
        modo = ("Los trabajadores van a EDITAR ARCHIVOS de verdad en la carpeta de la mesa, "
                "así que cada subtarea tiene que ser una acción de construcción concreta "
                "(qué archivo, qué cambio).")
    else:
        modo = ("Esto es trabajo de ANÁLISIS/CHARLA (no se editan archivos): cada subtarea es "
                "algo para pensar, investigar o responder.")
    return (
        f"Sos el LÍDER de esta mesa de trabajo. El humano pidió:\n\n«{pedido}»\n\n"
        f"Tu equipo (usá EXACTAMENTE estos alias):\n{roster}\n\n"
        f"Descomponé el pedido en subtareas y asigná cada una a UN miembro del equipo, "
        f"UNA POR LÍNEA, con el formato exacto:\n@alias: subtarea concreta\n\n"
        f"{modo} Podés darle varias subtareas a la misma silla (una por línea). "
        f"Arrancá con una frase breve de plan y después las líneas «@alias:». "
        f"NO ejecutes vos las subtareas: solo planificá y repartí."
    )


def _prompt_integracion(pedido, resumen):
    """Prompt de cierre: el líder integra lo que entregó el equipo en una respuesta final."""
    return (
        f"Sos el LÍDER de la mesa. El humano había pedido:\n\n«{pedido}»\n\n"
        f"Tu equipo entregó:\n\n{resumen}\n\n"
        f"Cerrá vos: integrá los aportes en una respuesta final para el humano — qué quedó "
        f"hecho, qué falta y cualquier conflicto entre los trabajadores. Conciso y claro."
    )


def _runner_prefix():
    """Wrapper de dispatch (runner headless), si está configurado.

    El runner (`enjambre-run.sh <agente> ...`) es config del WORKER, no dato de la
    silla: con ENJAMBRE_RUNNER seteado el motor antepone el wrapper y los CLIs corren
    en contenedores descartables (volumen de creds). Vacío = llamada directa al CLI
    en PATH (dev). comando[0] de cada silla ya es el nombre del agente que espera el
    runner (claude|opencode|agy), así que basta anteponer la ruta del script.
    """
    runner = getattr(settings, 'ENJAMBRE_RUNNER', '') or os.environ.get('ENJAMBRE_RUNNER', '')
    return [runner] if runner else []


def ejecutar_http(participante, prompt, timeout):
    """Silla de MODELO LOCAL: POST a la API Ollama del endpoint. Devuelve (salida, ruido).

    No usa subprocess/runner: el worker llega directo por HTTP al box Ollama.
    Si el box está apagado/inalcanzable, devuelve un marcador de ruido → la silla queda
    muda y la mesa sigue (degradación)."""
    url = participante.endpoint_url.rstrip('/') + '/api/generate'
    body = json.dumps({
        'model': participante.endpoint_model,
        'prompt': prompt,
        'stream': False,
        # Refuerzo anti-loop: un modelo chico tiende a copiar sus propias respuestas del
        # contexto de la mesa. repeat_penalty + ventana amplia + algo de temperatura lo evitan.
        'options': {'temperature': 0.7, 'repeat_penalty': 1.3, 'repeat_last_n': 256},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        salida = (data.get('response') or '').strip() or '(sin respuesta)'
    except urllib.error.URLError as e:
        # apagado / fuera de red / timeout del socket → degradar, no romper la mesa
        salida = f"(❌ {participante.nombre} no responde: {getattr(e, 'reason', e)})"
    except Exception as e:  # noqa: BLE001
        salida = f"(❌ error: {e})"
    return salida, es_ruido(salida)


def ejecutar_api(participante, provider, prompt, timeout, sesion=None):
    """Silla por API KEY: llama la API HTTP del proveedor (anthropic/openai/openrouter) con la key
    del vault. Devuelve (salida, ruido). No usa subprocess ni runner → es la ruta PORTABLE (sin
    binarios).

    Si el TOOLBELT está habilitado y hay `sesion`, corre con el loop de tool-use sobre el sistema
    real (F3: inspect/read_file/system_report auto, apply_fix con aprobación). Si no, charla plana (F2).

    La key sale del vault DESBLOQUEADO (runtime 0600). Si la bóveda está bloqueada o no hay key,
    el proveedor devuelve un marcador (❌ …) → la silla queda muda y la mesa sigue (degradación)."""
    from . import providers, vault, toolbelt
    from .clientes import modelo_de
    # base_url configurable solo para el proveedor openai-compat (Groq/DeepSeek/…); anthropic y
    # openrouter tienen endpoint fijo.
    base = getattr(settings, 'SWARM_OPENAI_BASE_URL', '') if provider == 'openai' else ''
    key, modelo = vault.get_key(provider), modelo_de(participante)
    if sesion is not None and toolbelt.habilitado():
        salida = providers.chat_agentic(provider, modelo, prompt, key, timeout,
                                        sesion, participante, base_url=base)
    else:
        salida = providers.chat(provider, modelo, prompt, key, timeout, base_url=base)
    return salida, es_ruido(salida)


def ejecutar_cli(participante, prompt, timeout, workdir=None, comando=None, workdir_ro=None,
                 sesion=None, cwd_maquina=None):
    """Corre la silla con el prompt dado. Devuelve (salida, ruido).

    Sillas de modelo local (endpoint_url seteado) van por HTTP (ejecutar_http); las sillas por API
    key (api-*) van por HTTP al proveedor (ejecutar_api); el resto son CLIs por subprocess. Si
    `workdir` está dado (worktree aislado), el CLI trabaja ahí: vía runner se exporta
    ENJAMBRE_WORKDIR (el wrapper lo monta en /work); sin runner, es el cwd. `comando` permite
    override (ej: el de fabricación vs el de charla). `workdir_ro` (excluyente con `workdir`):
    monta esa carpeta en /work SOLO-LECTURA — para que el líder lea/grepee el código real sin
    poder editar (exporta ENJAMBRE_WORKDIR_RO).

    `cwd_maquina` (modo máquina, toolbelt ON): corre sobre el equipo real desde esa carpeta. NO
    exporta ENJAMBRE_WORKDIR a propósito — con el runner, el wrapper montaría solo /work y la
    silla quedaría encerrada en el contenedor, que es lo contrario de operar la máquina.
    """
    if participante.endpoint_url and not comando:
        return ejecutar_http(participante, prompt, timeout)
    from .clientes import api_de
    prov = api_de(participante)
    if prov:  # silla por API key → HTTP directo al proveedor (+ toolbelt si está habilitado)
        return ejecutar_api(participante, prov, prompt, timeout, sesion=sesion)
    env = os.environ.copy()
    cwd = None
    if cwd_maquina:
        cwd = str(cwd_maquina)
    elif workdir:
        env['ENJAMBRE_WORKDIR'] = str(workdir)
        cwd = str(workdir)
    elif workdir_ro:
        env['ENJAMBRE_WORKDIR_RO'] = str(workdir_ro)
        cwd = str(workdir_ro)
    argv = list(comando or participante.comando)
    # Modo máquina: SIN runner. El wrapper mete al CLI en un contenedor descartable que solo ve
    # /work — justo lo contrario de operar el equipo real. Acá se invoca directo en el host.
    pref = [] if cwd_maquina else _runner_prefix()
    if not pref and argv:
        # Sin runner el CLI se invoca directo: si el binario no está en el PATH del
        # proceso (doble-clic del pendrive), resolver_bin lo busca en los dirs típicos.
        from .conexiones import resolver_bin
        ruta = resolver_bin(argv[0])
        if ruta:
            argv[0] = ruta
    try:
        result = subprocess.run(
            pref + argv + [prompt],
            capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd,
        )
        salida = (limpiar_salida(result.stdout) or limpiar_salida(result.stderr)
                  or "(sin respuesta)")
        if DOCKER_MOUNT_ERROR in (result.stderr or '').lower():
            # el runner no llegó ni a arrancar el CLI: falta su credencial/binario en el host
            salida = (f"(❌ {participante.nombre}: el host no tiene su credencial o binario "
                      f"— ¿el CLI está instalado y logueado? Revisá Conexiones)")
    except subprocess.TimeoutExpired:
        salida = f"(⏰ timeout tras {timeout}s)"
    except FileNotFoundError:
        salida = f"(❌ {participante.nombre} no instalado)"
    except Exception as e:  # noqa: BLE001 — cualquier fallo del CLI es ruido, no rompe la mesa
        salida = f"(❌ error: {e})"
    return salida, es_ruido(salida)


class Enjambre:
    """Mesa de trabajo multi-agente sobre una Sesion. (antes 'Concilio')"""

    def __init__(self, sesion):
        self.sesion = sesion

    # ── Persistencia ──────────────────────────────────────────────────────────
    def sillas(self):
        """Sillas de ESTA mesa (∩ globalmente activas). Vacío = fallback a todas las
        activas, así una mesa vieja o sin selección nunca queda muda."""
        sel = list(self.sesion.participantes.filter(activo=True).order_by('orden', 'key'))
        return sel or list(Participante.objects.filter(activo=True).order_by('orden', 'key'))

    def guardar(self, emisor, texto, participante=None, ruido=False, sistema=False,
                tokens=0, costo=0):
        return Mensaje.objects.create(
            sesion=self.sesion, emisor=emisor, participante=participante,
            texto=texto, es_ruido=ruido, es_sistema=sistema,
            tokens=tokens, costo=costo,
        )

    def log(self, texto, nivel='info', detalle=''):
        """Escribe una línea en el LOG DE ACTIVIDAD de la mesa (drawer en vivo, SSE aparte).

        Es feature de CONTROL: en mesas de CONSULTA no se traza (el flujo técnico es del dueño).
        Best-effort: un fallo de log NUNCA debe tumbar un turno. `detalle` = texto expandible al
        hacer hover (p.ej. el diff completo de un commit)."""
        if self._es_consulta():
            return
        try:
            from .models import LogMesa
            LogMesa.objects.create(sesion=self.sesion, texto=str(texto)[:500],
                                   nivel=nivel, detalle=detalle or '')
        except Exception:  # noqa: BLE001 — el log es accesorio
            pass

    def contexto(self):
        """Contexto reinyectado a las sillas: la CONVERSACIÓN reciente (sin ruido ni mensajes de
        sistema/UI), cada mensaje recortado y con tope TOTAL de caracteres. Acota los tokens de
        entrada (palanca de costo): el líder re-lee esto en CADA llamada y, sin tope, crece con
        la mesa. El mensaje completo sigue en el chat; sólo se recorta lo que se reinyecta."""
        qs = (self.sesion.mensajes.filter(es_ruido=False, es_sistema=False)
              .order_by('-creado_at', '-pk')[:MAX_CONTEXTO])
        msgs = list(reversed(qs))

        def fmt(m):
            t = m.texto or ''
            if len(t) > CONTEXTO_MSG_MAX:
                t = t[:CONTEXTO_MSG_MAX].rstrip() + ' …[recortado]'
            return f"[{m.emisor}]: {t}"

        lineas = [fmt(m) for m in msgs]
        # Tope total: descartar los MÁS VIEJOS hasta entrar en el presupuesto (preserva lo reciente).
        total = sum(len(x) + 1 for x in lineas)
        while len(lineas) > 1 and total > CONTEXTO_TOTAL_MAX:
            total -= len(lineas.pop(0)) + 1
        return "\n".join(lineas)

    def _nombre_humano(self):
        """Cómo se llama el humano en esta mesa = emisor del último mensaje humano (participante
        nulo, no-sistema). Sirve para que las sillas no lo confundan con una silla más."""
        m = (self.sesion.mensajes.filter(participante__isnull=True, es_sistema=False)
             .order_by('-id').first())
        return m.emisor if m else 'el humano'

    # ── Dispatch a una silla ────────────────────────────────────────────────────
    def _es_consulta(self):
        """Rango del CONTEXTO de la mesa = rango del creador (el dispatch corre en el worker
        sin request). Decide la variante de persona (A control / B consulta).
        Resolver pluggable: settings.ENJAMBRE_ROLE_RESOLVER = callable(creador) -> rango;
        None (el default de Swarm, single-user) = todo es `control`."""
        resolver = getattr(settings, 'ENJAMBRE_ROLE_RESOLVER', None)
        rol = resolver(self.sesion.creador) if callable(resolver) else 'control'
        return rol == 'consulta'

    def construir_prompt(self, participante, texto, editar=False, leer=False, maquina=False):
        ctx = self.contexto()
        sillas = self.sillas()
        roster = ", ".join(s.nombre for s in sillas)
        otras = ", ".join(s.nombre for s in sillas if s.key != participante.key) or "ninguna otra"
        humano = self._nombre_humano()
        if editar:
            encuadre = (
                "IMPORTANTE: tu DIRECTORIO ACTUAL es la CARPETA DE TRABAJO de esta mesa — un sandbox "
                "git aislado (el árbol desplegado NUNCA se toca). Podés leer, crear y editar archivos "
                "ahí. Hay un `NOTAS.md` que es la MEMORIA COMPARTIDA de la mesa: leélo antes de "
                "trabajar y dejá ahí decisiones/TODOs/contexto para los próximos turnos. Hacé lo que "
                "la mesa acordó y al final contá CONCRETAMENTE qué archivos tocaste. No afirmes "
                "cambios que no hiciste."
            )
        elif leer:
            encuadre = (
                "IMPORTANTE: como LÍDER tenés la CARPETA DE TRABAJO de esta mesa montada en tu "
                "DIRECTORIO ACTUAL en modo SOLO-LECTURA. LEÉ/GREPEÁ los archivos REALES (incluido "
                "`NOTAS.md`, la memoria compartida) ANTES de repartir o integrar: NO planifiques a "
                "ciegas ni adivines el contenido — abrí el código y citá `archivo:línea` cuando "
                "corresponda. NO podés editar ni crear archivos (eso es de las sillas de trabajo); si "
                "algo hay que cambiar, decílo en la subtarea apuntando al archivo y la línea."
            )
        elif maquina:
            from . import toolbelt
            encuadre = toolbelt.encuadre_cli()
        else:
            encuadre = (
                "IMPORTANTE: esto es CHARLA (respondés con texto). La mesa TIENE una carpeta de "
                "trabajo propia; cuando el dueño pide construir/editar algo con el comando «/armar», "
                "las sillas de trabajo la editan de verdad. En este turno NO estás editando archivos: "
                "no inventes que creaste o corriste algo."
            )
        return (
            f"{participante.persona_para(self._es_consulta())}\n\n"
            f"Estás en una MESA DE TRABAJO multi-agente del Enjambre. En la mesa hay "
            f"{len(sillas)} silla(s): {roster}. VOS sos {participante.nombre}; las otras son {otras}. "
            f"El humano se llama {humano} y NO es una silla: hace pedidos, no vota ni cuenta como un "
            f"agente más (no digas «los tres coincidimos» sumándolo). "
            f"Lo de abajo es la conversación de la mesa EN VIVO — incluye lo que YA dijeron los "
            f"demás agentes en este mismo turno. Leélos: podés responderles, coincidir, refutar o "
            f"construir sobre lo que dijeron, como en una charla real entre colegas.\n\n"
            f"--- Conversación de la mesa ---\n{ctx}\n\n"
            f"--- Último mensaje a responder ---\n{texto}\n\n"
            f"{encuadre}\n\n"
            f"Respondé como {participante.nombre}, dirigiéndote a la mesa (podés nombrar a los otros). "
            f"RECORDÁ TU ESTILO: {participante.recordatorio}"
        )

    def enviar(self, participante, texto, editar=False, leer=False):
        """Dispatch a una silla y persiste su respuesta. Devuelve el texto crudo.

        Si `editar` y la silla puede (control + CLI), corre en modo AGÉNTICO con la carpeta de la
        mesa como cwd: lee/crea/edita archivos ahí; después se commitea y se postea el diff.
        Si `leer` (y la silla es CLI, no fabrica en este turno), monta la carpeta de la mesa
        SOLO-LECTURA: el líder lee/grepea el código real para planificar/integrar sin editar."""
        from .clientes import edita_archivos, opera_maquina
        from . import toolbelt
        puede = editar and not self._es_consulta() and edita_archivos(participante)
        # MODO MÁQUINA: con el toolbelt ENCENDIDO, una silla CLI opera el equipo real en un turno
        # normal — igual que las sillas por API key, que ya lo hacían. No pisa a `/armar`: ese
        # sigue fabricando en la carpeta de la mesa (es el taller de entregables, otra cosa).
        # Sin gate por comando (no hay dónde interceptar un CLI); el candado es el switch y el
        # registro es la Bitácora. Ver el bloque «Sillas CLI operando la máquina» en toolbelt.py.
        maquina = (not puede and not leer and not self._es_consulta()
                   and opera_maquina(participante) and toolbelt.habilitado())
        workdir = comando = base = workdir_ro = None
        cwd_maq = None
        timeout = self.sesion.timeout
        if maquina:
            cwd_maq = toolbelt.cwd_maquina()
            comando = participante.cmd_trabajo()   # el agéntico: puede leer, editar y ejecutar
            timeout = max(self.sesion.timeout, FABRICAR_TIMEOUT_MIN)
        elif puede:
            from .workspace import mesa_workspace, _git
            workdir = str(mesa_workspace(self.sesion))
            comando = participante.cmd_trabajo()
            base = _git(workdir, 'rev-parse', 'HEAD', check=False)
            # Fabricar tarda más que charlar: subir el techo para no matar el turno a mitad.
            timeout = max(self.sesion.timeout, FABRICAR_TIMEOUT_MIN)
        elif leer and not participante.endpoint_url:
            # Líder en planeo/integración: carpeta de la mesa montada SOLO-LECTURA (lee/grepea el
            # código real, no a ciegas) sin editar ni commitear. Las sillas HTTP no montan FS.
            from .workspace import mesa_workspace
            workdir_ro = str(mesa_workspace(self.sesion))
        prompt = self.construir_prompt(participante, texto, editar=puede,
                                       leer=bool(workdir_ro), maquina=maquina)
        modo = ('opera la máquina' if maquina else
                'fabrica' if puede else ('lee (ro)' if workdir_ro else 'charla'))
        self.log(f"▶ {participante.nombre} · {modo} (timeout {timeout}s)", nivel='paso')
        t0 = time.monotonic()
        salida, ruido = ejecutar_cli(participante, prompt, timeout,
                                     workdir=workdir, comando=comando, workdir_ro=workdir_ro,
                                     sesion=self.sesion, cwd_maquina=cwd_maq)
        dt = time.monotonic() - t0
        # Velocímetro: estimación uniforme tokens ≈ len/4; costo notional por tarifa de la
        # silla (local = $0). No es la factura real, es referencia para ver y topear el gasto.
        from .clientes import precio_silla
        p_in, p_out = precio_silla(participante)
        tok_in, tok_out = len(prompt) // 4, len(salida) // 4
        tokens = tok_in + tok_out
        costo = round(tok_in / 1e6 * p_in + tok_out / 1e6 * p_out, 6)
        self.guardar(participante.nombre, salida, participante=participante, ruido=ruido,
                     tokens=tokens, costo=costo)
        gasto = '$0 (gratis)' if costo == 0 else f"${costo:.4f}"
        if ruido:
            self.log(f"✗ {participante.nombre} sin respuesta / error ({dt:.1f}s · ~{tokens} tok · {gasto})",
                     nivel='error', detalle=salida)
        else:
            self.log(f"✓ {participante.nombre} respondió ({dt:.1f}s · ~{tokens} tok · {gasto})",
                     nivel='ok')
        # Rescate: en modo fabricar commiteamos SIEMPRE lo que haya quedado en disco, aunque el
        # turno haya dado timeout/error — el agente pudo escribir archivos antes de que lo mataran.
        # comitear() no hace nada si no hubo cambios, así que es seguro llamarlo igual.
        if puede:
            self._comitear_y_postear(participante, workdir, base, parcial=ruido)
        # Modo máquina: el turno queda en la Bitácora — es donde el equipo ve trabajar a la silla
        # (qué CLI corrió, en qué carpeta y qué contó que hizo). Nunca rompe el turno.
        if maquina:
            try:
                toolbelt.log_cli(self.sesion, participante,
                                 comando or participante.comando, cwd_maq, salida)
            except Exception as e:  # noqa: BLE001 — la bitácora no puede tumbar la mesa
                self.log(f"no se pudo registrar el turno en la bitácora: {e}", nivel='error')
        return salida

    def _comitear_y_postear(self, participante, workdir, base, parcial=False):
        """Tras un turno agéntico: commitea la carpeta de la mesa. En el CHAT postea solo la
        línea corta + el stat (para no ensuciarlo); el diff completo va al LOG DE ACTIVIDAD,
        desplegable al hacer hover sobre el commit. Si no hubo cambios, no postea nada.

        `parcial`: el turno se cortó (timeout/error) pero quedó trabajo en disco — se rescata y se
        marca como parcial para que se sepa que NO es una entrega completa."""
        from .workspace import comitear, diff_stat, _git
        marca = ' (PARCIAL · turno cortado)' if parcial else ''
        try:
            sha = comitear(workdir, f"Enjambre · mesa {self.sesion.id}: {participante.nombre}{marca}")
        except Exception:  # noqa: BLE001 — un fallo de git no debe tumbar el turno
            return
        if not sha:
            return
        stat = diff_stat(workdir, base) if base else ''
        diff = _git(workdir, 'diff', f'{base}..HEAD', check=False) if base else ''
        # Chat: corto (commit + stat), sin volcar el diff entero.
        if parcial:
            cuerpo = f"⏰ Rescatado (commit {sha[:7]}) — el turno se cortó por timeout; se guardó lo que alcanzó a escribir"
        else:
            cuerpo = f"📝 Guardado (commit {sha[:7]})"
        if stat:
            cuerpo += f":\n{stat}"
        self.guardar(participante.nombre, cuerpo, participante=participante)
        # Flujo: entrada de commit con el diff COMPLETO en el detalle (hover lo despliega).
        detalle = "\n\n".join(p for p in (stat, diff) if p).strip()
        etiqueta = '⏰ commit parcial' if parcial else '✓ commit'
        self.log(f"{etiqueta} {sha[:7]} · {participante.nombre}", nivel='ok', detalle=detalle)

    def deshacer(self):
        """Revierte el último turno de /armar: git reset --hard al commit anterior de la carpeta
        de la mesa. Guard: nunca pasa el commit init (se conserva al menos 1 commit)."""
        from .workspace import mesa_workspace, _git
        dest = str(mesa_workspace(self.sesion))
        n = _git(dest, 'rev-list', '--count', 'HEAD', check=False)
        if not n.isdigit() or int(n) < 2:
            self.guardar("Enjambre", "↩️ No hay nada que deshacer todavía.", sistema=True)
            return
        actual = _git(dest, 'log', '-1', '--format=%s', check=False)
        _git(dest, 'reset', '--hard', 'HEAD~1', check=False)
        nuevo = _git(dest, 'rev-parse', '--short', 'HEAD', check=False)
        nuevo_msg = _git(dest, 'log', '-1', '--format=%s', check=False)
        self.guardar("Enjambre",
                     f"↩️ Deshecho «{actual}». La carpeta volvió a {nuevo} «{nuevo_msg}».",
                     sistema=True)

    def volver(self, arg=''):
        """Rebobina la carpeta de la mesa a un commit anterior (git reset --hard <sha>), SIN tocar
        el historial de mensajes. Granular (a diferencia de /deshacer, que vuelve solo 1). Sin sha:
        lista los commits recientes para que el humano elija. El sha debe ser ancestro de HEAD."""
        from .workspace import mesa_workspace, _git
        dest = str(mesa_workspace(self.sesion))
        sha = (arg or '').strip().split()[0] if (arg or '').strip() else ''
        if not sha:
            log = _git(dest, 'log', '--oneline', '--no-decorate', '-20', check=False)
            if not log.strip():
                self.guardar("Enjambre", "🕰️ Todavía no hay commits en la carpeta de la mesa.",
                             sistema=True)
            else:
                self.guardar("Enjambre", "🕰️ Commits de la mesa (el de arriba es el más nuevo). "
                             "Rebobiná con «/volver <sha>»:\n" + log, sistema=True)
            return
        # Validar: el sha tiene que resolver a un commit Y ser ancestro de HEAD (no "volver" adelante).
        full = _git(dest, 'rev-parse', '--verify', f'{sha}^{{commit}}', check=False)
        ancestros = _git(dest, 'rev-list', 'HEAD', check=False).split()
        if not full or full not in ancestros:
            self.guardar("Enjambre", f"⚠️ No encuentro el commit «{sha}» en el historial de la mesa. "
                         "Usá «/volver» (sin nada) para ver la lista.", sistema=True)
            return
        actual = _git(dest, 'rev-parse', '--short', 'HEAD', check=False)
        _git(dest, 'reset', '--hard', full, check=False)
        nuevo = _git(dest, 'rev-parse', '--short', 'HEAD', check=False)
        nuevo_msg = _git(dest, 'log', '-1', '--format=%s', check=False)
        self.log(f"↩️ /volver {actual} → {nuevo}", nivel='paso')
        self.guardar("Enjambre", f"↩️ Rebobiné la carpeta a {nuevo} «{nuevo_msg}» (estaba en {actual}). "
                     "El historial de mensajes de la mesa queda intacto.", sistema=True)

    def detener(self):
        """Kill-switch de la mesa (/alto): este turno NO se dispatcha a ninguna silla. Es el
        primitivo de control de daños que el modo continuo honra para frenar la iteración. Hoy, sin continuo, su efecto es confirmar y NO despertar a las sillas.
        OJO: un turno YA en curso NO se corta a mitad — el worker es sincrónico y recién ve el
        /alto cuando termina el dispatch actual."""
        self.log("🛑 /alto — la mesa no dispatcha este turno", nivel='paso')
        self.guardar("Enjambre", "🛑 Alto. La mesa quedó detenida: no se llamó a ninguna silla. "
                     "(Si había un turno en curso, ese no se corta a mitad.)", sistema=True)

    # ── Freno de mano (/alto cooperativo) ───────────────────────────────────────
    def _hay_alto(self):
        """Re-lee de la DB la señal de freno. La web la prende mientras el worker corre un turno;
        el engine la chequea ENTRE sillas para abortar lo que falta. Re-query (no cache) a propósito:
        el flag lo setea OTRO proceso (web) después de que este turno arrancó."""
        from .models import Sesion
        return Sesion.objects.filter(pk=self.sesion.pk, detener_solicitado=True).exists()

    def limpiar_alto(self):
        """Apaga la señal. Se llama al arrancar cada turno (no arrastrar un /alto viejo) y al abortar."""
        from .models import Sesion
        Sesion.objects.filter(pk=self.sesion.pk).update(detener_solicitado=False)

    def _abortar_alto(self, pendientes=''):
        """Consume la señal, apaga el modo continuo y avisa qué quedó sin hacer. /alto = stop duro."""
        from .models import Sesion
        Sesion.objects.filter(pk=self.sesion.pk).update(detener_solicitado=False, continuo=False, auto=False)
        self.sesion.continuo = self.sesion.auto = False
        extra = f" Quedó sin hacer: {pendientes}." if pendientes else ""
        self.log("🛑 /alto — corté el turno a pedido", nivel='error')
        self.guardar("Enjambre", f"🛑 Corté la mesa por /alto.{extra} "
                     "(La silla que estaba corriendo terminó; el resto se salteó.) Modo continuo apagado.",
                     sistema=True)
        return True

    # ── Modo continuo (default conservador) ──────────────────────────────────────────
    def _costo_acumulado(self):
        from django.db.models import Sum
        return self.sesion.mensajes.aggregate(c=Sum('costo'))['c'] or 0

    def _tope_excedido(self):
        tope = self.sesion.costo_tope or 0
        return tope > 0 and self._costo_acumulado() >= tope

    def _iterar_objetivo(self, on_respuesta=None, ajuste=''):
        """Corre UNA iteración de fabricación hacia self.sesion.objetivo, reutilizando todo el flujo
        (líder/plana, filtro de locales, freno /alto). Inyecta el ESTADO REAL de la carpeta (archivos
        + NOTAS.md) para que el líder NO planifique a ciegas ni alucine «arranquemos de cero». `ajuste`
        = corrección puntual del humano para esta iteración (lo que escribe junto a /seguí). No chequea
        tope ni postea pausa: eso es del caller (manual /seguí o el loop --auto)."""
        from .models import Topologia
        obj = (self.sesion.objetivo or '').strip()
        extra = (f"\n\n⚠️ AJUSTE que pide el humano para ESTA iteración (priorizalo): {ajuste.strip()}"
                 if ajuste.strip() else "")
        framing = (
            f"/armar {obj}\n\n"
            f"ESTADO ACTUAL DE LA CARPETA — esto YA está construido y PERSISTE en disco; NO empieces "
            f"de cero, construí ENCIMA de lo que existe:\n{self._estado_carpeta()}\n\n"
            f"[ITERACIÓN DE TRABAJO CONTINUO hacia ese objetivo] Hacé el PRÓXIMO incremento concreto "
            f"sobre lo ya hecho (no lo rehagas) y dejá en NOTAS.md qué quedó y qué falta.{extra}")
        self.log(f"🔁 iteración continua → {obj[:60]}" + (f" · ajuste: {ajuste.strip()[:40]}" if ajuste.strip() else ""),
                 nivel='paso')
        if self.sesion.topologia == Topologia.LIDER and self.sesion.lider_id:
            return self.liderar(framing, on_respuesta=on_respuesta)
        return self.responder(framing, on_respuesta=on_respuesta)

    def _continuo_iteracion(self, comando, arg, on_respuesta=None):
        """«/continuo [--auto] <obj>» fija el objetivo y arranca; «/seguí» corre la próxima hacia el
        MISMO objetivo; «/auto» pasa un continuo en curso a automático. Manual = corre 1 iteración y
        PAUSA esperando /seguí. --auto/auto = lo configura y lo maneja el worker (ver auto_paso)."""
        from .models import Sesion
        if self._es_consulta():
            return self.responder(arg or (self.sesion.objetivo or ''), on_respuesta=on_respuesta)
        ajuste = ''  # corrección puntual del humano (texto que va junto a /seguí)
        if comando == 'continuo':
            obj = (arg or '').strip()
            auto = False
            if obj.startswith('--auto'):
                auto, obj = True, obj[len('--auto'):].strip()
            if not obj:
                self.guardar("Enjambre", "ℹ️ Usá «/continuo <objetivo>» (o «/continuo --auto <objetivo>»).",
                             sistema=True)
                return {}
            Sesion.objects.filter(pk=self.sesion.pk).update(
                continuo=True, objetivo=obj, auto=auto, auto_iter=0)
            self.sesion.continuo, self.sesion.objetivo, self.sesion.auto = True, obj, auto
            if auto:
                self.guardar("Enjambre", f"🤖 Modo AUTO ON. Objetivo: «{obj}». Itero solo hasta el tope "
                             f"de costo, {AUTO_MAX_ITER} iteraciones, o que tires /alto. Arranco…",
                             sistema=True)
                return {}  # el worker maneja las iteraciones (auto_paso)
            self.guardar("Enjambre", f"🔁 Modo continuo ON. Objetivo: «{obj}». Corro una iteración y "
                         "freno para que revises; seguí con /seguí o cerrá con /alto.", sistema=True)
        elif comando == 'auto':
            self.sesion.refresh_from_db(fields=['continuo', 'objetivo'])
            if not self.sesion.continuo or not (self.sesion.objetivo or '').strip():
                self.guardar("Enjambre", "ℹ️ No hay objetivo continuo activo. Arrancá con "
                             "«/continuo --auto <objetivo>».", sistema=True)
                return {}
            Sesion.objects.filter(pk=self.sesion.pk).update(auto=True, auto_iter=0)
            self.guardar("Enjambre", f"🤖 Paso a AUTO: sigo solo hasta el tope, {AUTO_MAX_ITER} "
                         "iteraciones, o /alto.", sistema=True)
            return {}  # el worker toma desde acá
        else:  # segui
            self.sesion.refresh_from_db(fields=['continuo', 'objetivo', 'costo_tope'])
            obj = (self.sesion.objetivo or '').strip()
            if not self.sesion.continuo or not obj:
                self.guardar("Enjambre", "ℹ️ No hay objetivo continuo activo. Arrancá con "
                             "«/continuo <objetivo>».", sistema=True)
                return {}
            ajuste = (arg or '').strip()  # «/seguí <instrucción>» = corrección puntual de esta iteración
            if ajuste:
                self.guardar("Enjambre", f"↪️ Ajuste para esta iteración: {ajuste}", sistema=True)
        # Tope de costo: antes de gastar.
        if self._tope_excedido():
            self.guardar("Enjambre", f"💸 Tope de ${self.sesion.costo_tope} alcanzado "
                         f"(acumulado ${self._costo_acumulado():.4f}). No gasté más: subí el tope (⚙) "
                         "o cerrá con /alto.", sistema=True)
            return {}
        res = self._iterar_objetivo(on_respuesta=on_respuesta, ajuste=ajuste)
        # Pausa conservadora (salvo que /alto haya cerrado el modo en el medio).
        self.sesion.refresh_from_db(fields=['continuo'])
        if self.sesion.continuo and not self._hay_alto():
            self.guardar("Enjambre", f"⏸️ Iteración hecha (acumulado ${self._costo_acumulado():.4f}). "
                         "Revisá y seguí con /seguí, o cerrá con /alto.", sistema=True)
        return res

    # ── Modo --auto: el worker itera solo ──────────────────────────
    def auto_paso(self):
        """Una iteración del modo --auto, disparada por el WORKER (no el humano). Chequea límites
        (tope de costo, máx de iteraciones), corre la iteración y decide seguir o frenar. Devuelve
        True si iteró. /alto la corta entre sillas (apaga continuo+auto vía _abortar_alto)."""
        from .models import Sesion
        self.sesion.refresh_from_db()
        if not (self.sesion.continuo and self.sesion.auto):
            return False
        if self._tope_excedido():
            self._auto_stop(f"tope de ${self.sesion.costo_tope} alcanzado "
                            f"(acumulado ${self._costo_acumulado():.4f})")
            return False
        if self.sesion.auto_iter >= AUTO_MAX_ITER:
            self._auto_stop(f"llegué al máximo de {AUTO_MAX_ITER} iteraciones automáticas")
            return False
        self.limpiar_alto()  # turno fresco
        Sesion.objects.filter(pk=self.sesion.pk).update(auto_iter=self.sesion.auto_iter + 1)
        self.sesion.auto_iter += 1
        self.log(f"🤖 auto-iteración {self.sesion.auto_iter}/{AUTO_MAX_ITER}", nivel='paso')
        self._iterar_objetivo()
        # Tras la iteración: si /alto la cortó, _abortar_alto ya apagó continuo+auto.
        self.sesion.refresh_from_db(fields=['continuo', 'auto'])
        if not (self.sesion.continuo and self.sesion.auto):
            return True
        # ¿El objetivo ya está cumplido? Si sí, el modo se cierra solo (auto-detección de fin).
        cumplido, razon = self._objetivo_cumplido()
        if cumplido:
            self._auto_fin(razon)
            return True
        self.guardar("Enjambre", f"🤖 Auto-iteración {self.sesion.auto_iter} hecha "
                     f"(acumulado ${self._costo_acumulado():.4f}). Sigo… (/alto para frenar)",
                     sistema=True)
        return True

    def _auto_stop(self, motivo):
        """Apaga el modo automático (deja continuo ON para seguir a mano con /seguí)."""
        from .models import Sesion
        Sesion.objects.filter(pk=self.sesion.pk).update(auto=False)
        self.sesion.auto = False
        self.log(f"⏸️ auto detenido: {motivo}", nivel='paso')
        self.guardar("Enjambre", f"⏸️ Pausé el modo automático: {motivo}. El objetivo sigue activo: "
                     "subí el tope y /auto para reanudar solo, seguí a mano con /seguí, o cerrá con /alto.",
                     sistema=True)

    def _auto_fin(self, razon):
        """El evaluador dictaminó objetivo CUMPLIDO: cierra auto + continuo y postea el resumen (F4)."""
        from .models import Sesion
        n = self.sesion.auto_iter
        Sesion.objects.filter(pk=self.sesion.pk).update(auto=False, continuo=False)
        self.sesion.auto = self.sesion.continuo = False
        self.log("✅ objetivo cumplido — modo continuo cerrado", nivel='ok')
        self.resumen_cierre(f"✅ objetivo cumplido tras {n} iteración(es) — {razon}")

    # ── Cierre y promoción (F4) ─────────────────────────────────────────────────
    def cerrar(self, motivo=''):
        """«/cerrar»: termina la mesa con un resumen. Apaga el modo continuo/auto si estaba activo.
        No destruye nada: la carpeta persiste y se baja con 📁 (zip) o se rebobina con /volver."""
        from .models import Sesion
        Sesion.objects.filter(pk=self.sesion.pk).update(continuo=False, auto=False)
        self.sesion.continuo = self.sesion.auto = False
        self.resumen_cierre(motivo.strip() or 'cierre a pedido')

    def resumen_cierre(self, motivo=''):
        """Resumen de cierre (F4.1): objetivo, commits, archivos y COSTO TOTAL real (+ por silla).
        Reusa git y el velocímetro. Apunta al 📁 para bajar el resultado. Best-effort."""
        from django.db.models import Sum
        from .workspace import mesa_workspace, _git
        try:
            dest = str(mesa_workspace(self.sesion))
            files = _git(dest, 'ls-files', check=False) or ''
            n_files = len([x for x in files.splitlines() if x.strip()])
            n_commits = _git(dest, 'rev-list', '--count', 'HEAD', check=False) or '0'
            commits = _git(dest, 'log', '--oneline', '--no-decorate', '-8', check=False) or '(sin commits)'
        except Exception:  # noqa: BLE001
            files, n_files, n_commits, commits = '', 0, '0', '(carpeta no disponible)'
        agg = self.sesion.mensajes.aggregate(c=Sum('costo'), t=Sum('tokens'))
        costo, toks = agg['c'] or 0, agg['t'] or 0
        porsilla = (self.sesion.mensajes.filter(tokens__gt=0).values('emisor')
                    .annotate(c=Sum('costo'), t=Sum('tokens')).order_by('-c', '-t'))
        lineas = "\n".join(
            f"  · {g['emisor']}: {g['t']} tok · {('$%.4f' % g['c']) if g['c'] else '$0'}"
            for g in porsilla)
        obj = (self.sesion.objetivo or '').strip()
        cuerpo = (
            f"🏁 CIERRE DE LA MESA{(' — ' + motivo) if motivo else ''}\n"
            + (f"Objetivo: «{obj}»\n" if obj else "")
            + f"Commits: {n_commits} · Archivos: {n_files} · Costo total estimado: ${costo:.4f} · {toks} tok\n"
            + (f"Gasto por silla:\n{lineas}\n" if lineas else "")
            + (f"\nArchivos:\n{files}\n" if files else "")
            + f"\nÚltimos commits:\n{commits}\n\n"
            "📥 Bajá todo con 📁 → «⬇ Descargar todo (.zip)». Nada se borra: la carpeta persiste "
            "(podés rebobinar con /volver o seguir con /armar)."
        )
        self.guardar("Enjambre", cuerpo, sistema=True)
        self.log("🏁 cierre — resumen posteado", nivel='ok')

    # ── Auto-detección de "objetivo cumplido" ───────────────────────
    def _juez(self):
        """Silla que evalúa si el objetivo está cumplido. Prefiere una GRATIS (local o modelo free)
        para no encarecer la evaluación; si no, el líder; si no, la primera silla."""
        from .clientes import precio_silla
        sillas = self.sillas()
        for s in sillas:
            if precio_silla(s) == (0.0, 0.0):
                return s
        return self.sesion.lider or (sillas[0] if sillas else None)

    def _estado_carpeta(self):
        """Resumen del estado de la carpeta de la mesa para el evaluador: archivos EN DISCO +
        NOTAS.md (la memoria compartida donde las sillas anotan qué falta).

        Lista el disco (no `git ls-files`) a propósito: así incluye también los archivos que el
        humano sube a mitad de trabajo y todavía no están commiteados — el worker los commitea
        sola al cerrar el turno, pero las sillas tienen que VERLOS desde el turno en que aparecen."""
        from .workspace import mesa_workspace
        dest = str(mesa_workspace(self.sesion))
        listado = []
        for root, dirs, files in os.walk(dest):
            # Excluir .git, runtime de las CLIs (dot-dirs) y __pycache__.
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            for f in files:
                if f.startswith('.'):
                    continue
                rel = os.path.relpath(os.path.join(root, f), dest)
                listado.append(rel)
                if len(listado) >= 2000:
                    break
            if len(listado) >= 2000:
                break
        listado.sort(key=str.lower)
        files_txt = '\n'.join(listado) if listado else '(sin archivos)'
        notas = ''
        try:
            with open(os.path.join(dest, 'NOTAS.md'), encoding='utf-8') as f:
                notas = f.read()[:2000]
        except Exception:  # noqa: BLE001
            pass
        return f"Archivos en la carpeta:\n{files_txt}\n\nNOTAS.md (memoria de la mesa):\n{notas or '(vacío)'}"

    def _objetivo_cumplido(self):
        """Le pregunta a una silla gratis si el objetivo ya está cumplido (mirando archivos+NOTAS).
        Devuelve (cumplido: bool, razon: str). Best-effort: ante ruido/duda devuelve False (seguir
        iterando, acotado por el máximo, es más seguro que cortar de más)."""
        obj = (self.sesion.objetivo or '').strip()
        juez = self._juez()
        if not obj or juez is None:
            return False, ''
        prompt = (
            f"Sos un EVALUADOR estricto pero realista. El objetivo de trabajo es:\n«{obj}»\n\n"
            f"{self._estado_carpeta()}\n\n"
            "¿El objetivo está COMPLETAMENTE cumplido con lo que hay (sin exigir extras no pedidos)? "
            "Respondé EXACTAMENTE en este formato:\n"
            "Línea 1: CUMPLIDO o PENDIENTE\n"
            "Línea 2: una frase corta justificando."
        )
        try:
            salida, ruido = ejecutar_cli(juez, prompt, min(self.sesion.timeout, 90))
        except Exception:  # noqa: BLE001
            return False, ''
        if ruido or not (salida or '').strip():
            return False, ''
        lineas = salida.strip().splitlines()
        veredicto = lineas[0].strip().upper()
        cumplido = veredicto.startswith('CUMPLIDO') and 'PENDIENTE' not in veredicto
        razon = (lineas[1].strip() if len(lineas) > 1 else salida.strip())[:200]
        self.log(f"🔎 evaluación de objetivo: {'CUMPLIDO' if cumplido else 'PENDIENTE'}",
                 nivel='info', detalle=salida.strip()[:500])
        return cumplido, razon

    def debatir(self, texto, on_respuesta=None):
        """Como debate() pero SIN re-guardar el turno del humano (ya persistido por la vista).
        N rondas (sesion.rondas): en cada ronda cada silla ve lo que dijeron las otras. Solo texto."""
        sillas = self.sillas()
        nombres = ", ".join(s.nombre for s in sillas)
        self.guardar("Enjambre",
                     f"💬 Debate entre {len(sillas)} silla(s) ({nombres}) · {self.sesion.rondas} rondas.",
                     sistema=True)
        respuestas = {}
        for ronda in range(1, self.sesion.rondas + 1):
            for silla in sillas:
                if self._hay_alto():
                    self._abortar_alto(f"resto del debate (ronda {ronda}+)")
                    return respuestas
                if ronda == 1:
                    prompt = texto
                else:
                    otras = "\n".join(
                        f"- {s.nombre}: {respuestas[s.key]}"
                        for s in sillas if s.key != silla.key and s.key in respuestas
                    )
                    prompt = (
                        f"{texto}\n\nRespuestas de otros agentes:\n{otras}\n\n"
                        f"Teniendo en cuenta lo que dijeron los otros, ¿qué aportás o refutás?"
                    )
                resp = self.enviar(silla, prompt)
                respuestas[silla.key] = resp
                if on_respuesta:
                    on_respuesta(silla, resp)
        return respuestas

    # ── Topología LÍDER ───────────────────────────────────────────────────────
    def _parse_asignaciones(self, plan, workers):
        """Extrae las líneas «@alias: subtarea» del plan del líder. Devuelve [(silla, subtarea)]
        en orden de aparición; junta varias subtareas de la misma silla. Solo matchea workers."""
        by_alias = {}
        for w in workers:
            by_alias.setdefault(w.alias, w)
            by_alias.setdefault(w.key.lower(), w)
        orden, agrupado = [], {}
        for linea in (plan or '').splitlines():
            m = re.match(r'\s*[-*\d.)\]]*\s*@(\S+?)\s*[:：\-–]\s*(.+)', linea)
            if not m:
                continue
            alias = m.group(1).lower().rstrip('.,:;!?')
            silla = by_alias.get(alias)
            if not silla:
                continue
            if silla.key not in agrupado:
                agrupado[silla.key] = (silla, [])
                orden.append(silla.key)
            agrupado[silla.key][1].append(m.group(2).strip())
        return [(agrupado[k][0], "\n".join(agrupado[k][1])) for k in orden]

    def liderar(self, texto, on_respuesta=None):
        """Modo LÍDER (NO guarda el turno del humano: ya persistido por la web/REPL).

        El líder descompone el pedido en subtareas «@silla: …», cada silla ejecuta la suya
        (agéntica en la carpeta de la mesa si es /armar y puede), y el líder integra y reporta.
        Secuencial a propósito: cada silla ve en su contexto lo que dejaron las anteriores.

        Degrada solo: sin líder válido → plana; @mención → solo esa silla; /deshacer y /debate
        los maneja el líder como en plana; consulta nunca fabrica (el verbo queda como charla)."""
        lider = self.sesion.lider
        # Líder caído (desactivado): la mesa está marcada como LÍDER pero corre plana. Antes era
        # 100% silencioso — avisamos UNA vez (mensaje de sistema) para que no parezca que el líder
        # trabajó. La @mención no avisa: es una desviación deliberada de un turno puntual, no una
        # degradación de la topología.
        if lider is not None and not lider.activo and self.mencion(texto) is None:
            self.log(f"⚠️ líder {lider.nombre} inactivo — la mesa corre PLANA", nivel='error')
            # Dedupe: si el último mensaje de sistema YA es este aviso, no lo repetimos en cada
            # turno (sería spam). Vuelve a aparecer solo si algún otro sistema lo "tapó" en el medio.
            ult_sis = (self.sesion.mensajes.filter(es_sistema=True).order_by('-id')
                       .values_list('texto', flat=True).first() or '')
            if 'modo PLANO' not in ult_sis:
                self.guardar("Enjambre", f"⚠️ El líder de esta mesa ({lider.nombre}) está desactivado: "
                             "este turno corre en modo PLANO (todas las sillas responden, sin reparto). "
                             "Reactivá esa silla o asigná otro líder en ⚙ para recuperar el modo líder.",
                             sistema=True)
        if lider is None or not lider.activo or self.mencion(texto) is not None:
            return self.responder(texto, on_respuesta=on_respuesta)

        comando, limpio = parse_comando(texto)
        if comando and self._es_consulta():
            comando, limpio = None, texto  # consulta: el verbo no fabrica, es charla
        if comando == 'undo':
            self.deshacer()
            return {}
        if comando == 'volver':
            self.volver(limpio)
            return {}
        if comando == 'alto':
            self.detener()
            return {}
        if comando == 'cerrar':
            self.cerrar(limpio)
            return {}
        if comando in ('continuo', 'segui', 'auto'):
            return self._continuo_iteracion(comando, limpio, on_respuesta=on_respuesta)
        if comando == 'debate':
            return self.debatir(limpio or texto, on_respuesta=on_respuesta)
        editar = (comando == 'build')
        pedido = limpio if comando else texto

        workers = [s for s in self.sillas() if s.key != lider.key]
        if editar:
            # fabricar de verdad: solo sillas que editan archivos (CLI). Las de modelo local (HTTP)
            # y las api:* (charla en F2) quedan fuera del reparto.
            from .clientes import edita_archivos
            workers = [w for w in workers if edita_archivos(w)]

        modo_txt = 'fabricar' if editar else 'charla'
        self.log(f"👑 Modo líder ({lider.nombre}) · {modo_txt} · {len(workers)} trabajador(es)",
                 nivel='paso')

        # 1) El líder planifica y reparte (si está solo, responde/fabrica el pedido directo).
        #    `leer=True`: con workers, el líder NO fabrica pero ve la carpeta read-only para no
        #    planificar a ciegas. Si está solo y es /armar, `puede` gana y fabrica (leer se ignora).
        plan = self.enviar(lider, _prompt_plan(pedido, workers, editar) if workers else pedido,
                           editar=(editar and not workers), leer=bool(workers))
        if on_respuesta:
            on_respuesta(lider, plan)
        resultados = {lider.key: plan}
        if not workers:
            return resultados

        # 2) Parsear las asignaciones del plan.
        asignaciones = self._parse_asignaciones(plan, workers)
        if not asignaciones:
            self.log("⚠️ El líder no repartió subtareas (formato «@silla: …»)", nivel='error')
            self.guardar("Enjambre", "ℹ️ El líder no repartió subtareas con el formato «@silla: …»; "
                         "su mensaje queda como respuesta de la mesa.", sistema=True)
            return resultados
        reparto = ", ".join(f"{s.alias}×{len(sub.splitlines()) or 1}" for s, sub in asignaciones)
        self.log(f"✓ plan repartido · {len(asignaciones)} silla(s): {reparto}", nivel='ok',
                 detalle=plan)

        # 3) Cada silla ejecuta su subtarea (secuencial: ve lo anterior en el contexto).
        for i, (silla, subtarea) in enumerate(asignaciones):
            if self._hay_alto():
                rest = ", ".join(s.nombre for s, _ in asignaciones[i:])
                self._abortar_alto(f"{rest} + integración del líder" if rest else "integración del líder")
                return resultados
            encargo = f"[Encargo de {lider.nombre} (líder)]: {subtarea}"
            try:
                resp = self.enviar(silla, encargo, editar=editar)
            except Exception as e:  # noqa: BLE001
                resp = f"(❌ error: {e})"
            resultados[silla.key] = resp
            if on_respuesta:
                on_respuesta(silla, resp)

        # 4) El líder integra y reporta (salvo que pidieran /alto recién terminada la última silla).
        if self._hay_alto():
            self._abortar_alto("integración del líder")
            return resultados
        self.log(f"👑 {lider.nombre} integrando los aportes…", nivel='paso')
        resumen = "\n\n".join(
            f"@{s.alias} ({s.nombre}):\n{resultados.get(s.key, '(sin respuesta)')}"
            for s, _ in asignaciones
        )
        cierre = self.enviar(lider, _prompt_integracion(pedido, resumen), leer=True)
        if on_respuesta:
            on_respuesta(lider, cierre)
        resultados[lider.key] = cierre
        return resultados

    def mencion(self, texto):
        """Si el mensaje arranca con @alias de una silla de la mesa, devuelve esa silla
        (para dirigirle SOLO a ella). Si no hay @ o no matchea, devuelve None."""
        t = (texto or '').lstrip()
        if not t.startswith('@'):
            return None
        token = t[1:].split(None, 1)[0].lower().rstrip('.,:;!?') if len(t) > 1 else ''
        if not token:
            return None
        for silla in self.sillas():
            if silla.alias == token or silla.key == token:
                return silla
        return None

    @staticmethod
    def _sin_mencion(texto):
        """Saca el '@alias' del arranque para que la silla reciba la pregunta limpia."""
        t = (texto or '').lstrip()
        if t.startswith('@'):
            partes = t[1:].split(None, 1)
            return partes[1] if len(partes) > 1 else ''
        return texto

    # ── Topología PLANA ─────────────────────────────────────────────────────────
    def responder(self, texto, on_respuesta=None):
        """Dispatch SECUENCIAL a las sillas activas, SIN guardar el mensaje del humano
        (ya persistido). Lo usa el worker para contestar una pregunta de la web.

        Secuencial a propósito: cada silla persiste su respuesta antes de la siguiente, así
        la próxima la ve en su contexto y la mesa "se lee" entre sí dentro del mismo turno.
        El líder (si está) va primero por orden de sillas().

        Si el mensaje menciona a una silla con @alias, SOLO esa responde (la pregunta le
        llega sin el @). Los comandos (/armar, /deshacer, /debate) solo los honra CONTROL;
        para consulta se ignora el verbo y todo cae a charla."""
        objetivo = self.mencion(texto)
        if objetivo is not None:
            destinatarias = [objetivo]
            texto = self._sin_mencion(texto)
        else:
            destinatarias = self.sillas()
        # Comando de mesa (tras sacar la @mención). Gate de rango: consulta = solo charla.
        comando, limpio = parse_comando(texto)
        editar = False
        if comando:
            if self._es_consulta():
                texto = limpio  # consulta: ignorar el verbo, tratar como charla
            elif comando == 'undo':
                self.deshacer()
                return {}
            elif comando == 'volver':
                self.volver(limpio)
                return {}
            elif comando == 'alto':
                self.detener()
                return {}
            elif comando == 'cerrar':
                self.cerrar(limpio)
                return {}
            elif comando in ('continuo', 'segui', 'auto'):
                return self._continuo_iteracion(comando, limpio, on_respuesta=on_respuesta)
            elif comando == 'debate':
                return self.debatir(limpio or texto, on_respuesta=on_respuesta)
            elif comando == 'build':
                texto, editar = limpio, True
                # /armar = trabajo real: solo las sillas que PUEDEN editar (CLI). Las de modelo
                # local (HTTP) y las api:* (charla en F2) no tienen acceso a archivos → se saltean
                # para que no "actúen" que fabricaron (un modelo tiende a alucinar que escribió).
                from .clientes import edita_archivos
                capaces = [s for s in destinatarias if edita_archivos(s)]
                saltadas = [s for s in destinatarias if not edita_archivos(s)]
                if not capaces:
                    self.guardar("Enjambre", "⚠️ Ninguna silla de la mesa puede fabricar: las de "
                                 "modelo local (HTTP) y las de API key no editan archivos. Sentá una "
                                 "silla CLI (claude/opencode) para usar /armar.", sistema=True)
                    return {}
                if saltadas:
                    nombres = ", ".join(s.nombre for s in saltadas)
                    self.guardar("Enjambre", f"ℹ️ {nombres} no edita archivos (modelo local o API key); "
                                 "queda fuera de este /armar (fabrican las sillas CLI).", sistema=True)
                destinatarias = capaces
        resultados = {}
        for i, silla in enumerate(destinatarias):
            if self._hay_alto():
                self._abortar_alto(", ".join(s.nombre for s in destinatarias[i:]))
                break
            try:
                resp = self.enviar(silla, texto, editar=editar)
            except Exception as e:  # noqa: BLE001
                resp = f"(❌ error: {e})"
            resultados[silla.key] = resp
            if on_respuesta:
                on_respuesta(silla, resp)
        return resultados

    def broadcast(self, texto, on_respuesta=None):
        """Plana: guarda el turno del humano y dispatcha a todas las sillas."""
        self.guardar("Humano", texto)
        return self.responder(texto, on_respuesta=on_respuesta)

    def debate(self, texto, on_respuesta=None):
        """Plana multi-ronda: en cada ronda cada silla ve lo que dijeron las otras."""
        self.guardar("Humano", texto)
        sillas = self.sillas()
        respuestas = {}
        for ronda in range(1, self.sesion.rondas + 1):
            for silla in sillas:
                if self._hay_alto():
                    self._abortar_alto(f"resto del debate (ronda {ronda}+)")
                    return respuestas
                if ronda == 1:
                    prompt = texto
                else:
                    otras = "\n".join(
                        f"- {s.nombre}: {respuestas[s.key]}"
                        for s in sillas if s.key != silla.key and s.key in respuestas
                    )
                    prompt = (
                        f"{texto}\n\nRespuestas de otros agentes:\n{otras}\n\n"
                        f"Teniendo en cuenta lo que dijeron los otros, ¿qué aportás o refutás?"
                    )
                resp = self.enviar(silla, prompt)
                respuestas[silla.key] = resp
                if on_respuesta:
                    on_respuesta(silla, resp, ronda)
        return respuestas
