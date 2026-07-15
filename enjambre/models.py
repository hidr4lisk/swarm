"""
enjambre/models.py — modelo de datos del núcleo.

Las "sillas" de la mesa son DATOS (Participante), no hardcode: sumar una IA = agregar
una fila. Sesion fija la topología (plana ↔ líder) y los parámetros de corrida. Mensaje
es append-only y hereda el rol de historia.jsonl del prototipo.

Tarea/Workspace cubren la fabricación en worktrees aislados.
"""
from django.db import models


class Rol(models.TextChoices):
    TRABAJADOR = 'trabajador', 'Trabajador'
    LIDER = 'lider', 'Líder'


class Topologia(models.TextChoices):
    PLANA = 'plana', 'Plana (broadcast)'
    LIDER = 'lider', 'Líder'


class Participante(models.Model):
    """Una silla de la mesa. Hoy todas son IA (CLIs); el humano se maneja aparte."""
    key = models.SlugField(max_length=50, unique=True)
    nombre = models.CharField(max_length=100)
    # Comando base del CLI para CHARLAR (sin el prompt). Ej: ["claude", "-p", "--output-format", "text"]
    comando = models.JSONField(default=list)
    # Comando para FABRICAR en un worktree (modo agéntico, edita archivos). Vacío = usa `comando`.
    comando_trabajo = models.JSONField(default=list, blank=True)
    # Silla de MODELO LOCAL vía HTTP (Ollama). Si endpoint_url está seteado, el engine NO
    # hace subprocess: pega a la API {endpoint_url}/api/generate con endpoint_model. Genérico
    # para cualquier box Ollama (en tu máquina o en tu LAN).
    endpoint_url = models.CharField(
        max_length=200, blank=True,
        help_text='URL base de un Ollama (ej: http://localhost:11434). Vacío = silla CLI.',
    )
    endpoint_model = models.CharField(
        max_length=100, blank=True, help_text='Nombre del modelo en el endpoint (ej: qwen2.5:3b).',
    )
    # Rango CONSULTA solo puede sentar/usar sillas con esto en True (protege los tokens caros).
    permitir_consulta = models.BooleanField(
        default=False, help_text='Si el rango consulta puede usar esta silla. False = solo control.',
    )
    color = models.CharField(max_length=20, blank=True, help_text='Código ANSI para el REPL.')
    # Color del chip/borde de la silla en la mesa (hex #rrggbb, seteable a mano). Vacío = negro
    # por defecto. Distinto del `color` de arriba (ANSI del REPL).
    color_ui = models.CharField(max_length=9, blank=True, help_text='Color en la mesa (hex #rrggbb). Vacío = negro.')
    # Avatar 1:1 (retrato estilo StarCraft en los mensajes de la mesa). Se guarda como data-URI
    # (base64) recortado/redimensionado en el navegador → sin MEDIA/Pillow. Vacío = fallback (punto de color).
    avatar = models.TextField(blank=True, help_text='Retrato 1:1 como data-URI. Vacío = usa el punto de color.')
    persona = models.TextField(blank=True, help_text='Encuadre de rol/estilo (variante A, rango control). Va ARRIBA del prompt.')
    persona_consulta = models.TextField(blank=True, help_text='Persona variante B para rango consulta. Vacío = usa la A.')
    recordatorio = models.TextField(blank=True, help_text='Se repite ABAJO del prompt (recencia).')
    # Capacidad CORTA de la silla, para que el LÍDER reparta por capacidad y no a ciegas.
    # Va en el roster que el líder ve al planificar. Vacío = sin pista (reparto como antes).
    especialidad = models.CharField(
        max_length=120, blank=True,
        help_text='Capacidad corta para que el líder reparta mejor '
                  '(ej: "frontend/CSS", "scripts/automatización", "redacción"). Vacío = sin pista.',
    )
    # Rol corto SOLO para la tarjeta del panel de sillas (reconocerlas rápido sin abrirlas).
    # NO se pasa a las sillas ni al contexto — es puramente UI (a diferencia de `especialidad`,
    # que sí va en el roster que el líder ve al planificar).
    rol_tarjeta = models.CharField(
        max_length=40, blank=True, default='',
        help_text='Rol corto para la tarjeta del panel (ej: "Arquitecto", "Red Team"). '
                  'Solo display — NO se pasa a las sillas ni al contexto.',
    )
    rol = models.CharField(max_length=20, choices=Rol.choices, default=Rol.TRABAJADOR)
    activo = models.BooleanField(default=True)
    orden = models.PositiveIntegerField(default=0)
    creado_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['orden', 'key']

    def __str__(self):
        return f"{self.nombre} ({self.key})"

    def cmd_trabajo(self):
        """Invocación para fabricar (edita archivos); cae a `comando` si no hay una propia."""
        return list(self.comando_trabajo) if self.comando_trabajo else list(self.comando)

    @property
    def alias(self):
        """Alias corto tipeable para mencionar a la silla con @ (ej: 'qwen', 'claude').
        Primera palabra del nombre, en minúscula y sin símbolos; cae al key."""
        import re
        base = (self.nombre or self.key).split()[0].lower()
        return re.sub(r'[^a-z0-9]', '', base) or self.key

    @property
    def motor(self):
        """Agente/modelo real detrás de la silla (estable; no cambia al renombrar). Para que
        alguien que no conoce el nombre custom ubique la silla: 'JORGE (Claude Code)'."""
        if self.endpoint_model:
            return self.endpoint_model
        # Si el comando fija un modelo (--model X), mostrarlo — sillas opencode multi-modelo
        # comparten binario pero distinto modelo (ej: 'opencode/big-pickle' → 'big-pickle').
        cmd = self.comando or []
        if '--model' in cmd:
            i = cmd.index('--model')
            if i + 1 < len(cmd):
                return cmd[i + 1].split('/')[-1]
        labels = {'claude': 'Claude Code', 'opencode': 'OpenCode', 'agy': 'Antigravity'}
        return labels.get(self.key) or (cmd[0] if cmd else self.key)

    @property
    def etiqueta(self):
        """Nombre para chips/UI: 'Nombre (motor)'; sin paréntesis si el nombre ya es el motor."""
        return self.nombre if self.nombre == self.motor else f"{self.nombre} ({self.motor})"

    def persona_para(self, es_consulta):
        """Persona según el rango del contexto: B (persona_consulta) si es consulta y existe;
        si no, A (persona)."""
        if es_consulta and self.persona_consulta.strip():
            return self.persona_consulta
        return self.persona


class Sesion(models.Model):
    """Una mesa de trabajo: topología + parámetros + hilo de mensajes."""
    nombre = models.CharField(max_length=200, default='Mesa')
    topologia = models.CharField(max_length=20, choices=Topologia.choices, default=Topologia.PLANA)
    # Silla con rol líder cuando topologia == LIDER. Null en plana.
    lider = models.ForeignKey(
        Participante, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sesiones_lideradas',
    )
    rondas = models.PositiveIntegerField(default=2)
    timeout = models.PositiveIntegerField(default=180, help_text='Segundos por call a un CLI.')
    participantes = models.ManyToManyField(
        Participante, blank=True, related_name='sesiones',
        help_text='Sillas que participan en esta mesa. Vacío = todas las activas.',
    )
    activa = models.BooleanField(default=True)
    # Fijada (pin, como en Brain): las mesas fijadas van arriba en el listado.
    fijada = models.BooleanField(default=False)
    # Freno de mano (/alto): la web la prende al instante (sin encolar un mensaje, que quedaría
    # enterrado bajo las respuestas de las sillas y nunca se procesaría). El worker la re-lee de la
    # DB ENTRE silla y silla y aborta lo que queda del turno. Se limpia al arrancar cada turno y
    # al consumirla. NO interrumpe la silla que ya está corriendo (esa termina); corta el resto.
    detener_solicitado = models.BooleanField(default=False)
    # Modo CONTINUO (default conservador): objetivo persistente + flag de modo activo. «/continuo <obj>»
    # lo prende y corre la 1ª iteración; «/seguí» corre la próxima hacia el MISMO objetivo
    # (construye encima de la carpeta persistente); «/alto» lo apaga. SIN --auto: cada iteración la
    # dispara el humano (default conservador, no quema tokens en piloto automático).
    continuo = models.BooleanField(default=False)
    objetivo = models.TextField(blank=True, help_text='Objetivo del modo continuo (lo fija /continuo).')
    # Tope de gasto estimado acumulado de la mesa (USD). 0 = sin tope. Antes de cada iteración
    # continua, si el acumulado lo supera, NO se itera (control de daños).
    costo_tope = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    # Modo --auto: el worker itera SOLO hacia el objetivo (sin que el humano dispare
    # cada /seguí). Opt-in. Frena por: tope de costo, máximo de iteraciones (safety) o /alto.
    # `auto_iter` cuenta las iteraciones de la corrida auto actual (se resetea al arrancar).
    auto = models.BooleanField(default=False)
    auto_iter = models.PositiveIntegerField(default=0)
    # Watermark anti-mensaje-enterrado: id del último Mensaje HUMANO al que el worker ya le
    # disparó un turno. El worker procesa la sesión si el último mensaje humano tiene id > esto
    # (en vez de mirar solo el tail, que se pierde si el humano escribe DURANTE un turno largo:
    # al terminar, el tail es de una silla y su mensaje quedaba enterrado para siempre).
    ultimo_humano_respondido = models.PositiveIntegerField(default=0)
    workspace_dir = models.CharField(
        max_length=500, blank=True,
        help_text='Carpeta de trabajo PERSISTENTE de la mesa (host abs path). La setea el worker '
                  'al primer fabricar en modo scratch. Vacío = todavía no se fabricó ahí.',
    )
    creador = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='enjambre_sesiones',
        help_text='Quién creó la mesa. control ve todas; consulta solo las suyas.',
    )
    creado_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-creado_at']

    def __str__(self):
        return f"{self.nombre} [{self.topologia}] #{self.pk}"


class Mensaje(models.Model):
    """Append-only. Hereda historia.jsonl: cada turno del hilo compartido.

    'ruido' = límites de sesión, timeouts, errores. Se muestra pero NO entra al
    contexto reinyectado, para no envenenar la ventana de los demás agentes.
    """
    sesion = models.ForeignKey(Sesion, on_delete=models.CASCADE, related_name='mensajes')
    # Snapshot del emisor ("Humano", "Claude Code", ...) — sobrevive aunque se borre la silla.
    emisor = models.CharField(max_length=100)
    participante = models.ForeignKey(
        Participante, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='mensajes',
    )
    texto = models.TextField()
    es_ruido = models.BooleanField(default=False)
    # Mensaje de SISTEMA (confirmación de /deshacer, etc.): se muestra pero el worker NO lo toma
    # como turno del humano (no re-dispara respuestas aunque tenga participante nulo).
    es_sistema = models.BooleanField(default=False)
    # Velocímetro: tokens estimados del turno (prompt+salida, ~len/4) y costo NOTIONAL
    # en USD a precio de lista de la silla. Silla local (HTTP) = 0. Es estimado, no la factura real.
    # Mensajes del humano/sistema/commit quedan en 0.
    tokens = models.PositiveIntegerField(default=0)
    costo = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    creado_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['creado_at', 'pk']

    def __str__(self):
        return f"[{self.emisor}] {self.texto[:50]}"


class Tarea(models.Model):
    """Un trabajo encajonado: una silla fabrica sobre un repo, SIEMPRE en un worktree
    aislado. El árbol desplegado nunca se toca; el resultado queda como branch a aprobar.
    """
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente'
        EN_CURSO = 'en_curso', 'En curso'
        HECHA = 'hecha', 'Hecha'
        SIN_CAMBIOS = 'sin_cambios', 'Sin cambios'
        ERROR = 'error', 'Error'

    sesion = models.ForeignKey(
        Sesion, on_delete=models.SET_NULL, null=True, blank=True, related_name='tareas',
    )
    titulo = models.CharField(max_length=200)
    ordenes = models.TextField(help_text='Las instrucciones para el agente (el briefing).')
    # En modo persistente (scratch) el repo es la carpeta de la mesa → repo_path queda vacío.
    repo_path = models.CharField(max_length=500, blank=True, help_text='Repo git destino en disco (modo repo). Vacío = modo scratch (carpeta de la mesa).')
    persistente = models.BooleanField(default=False, help_text='True = fabrica IN-PLACE en la carpeta persistente de la mesa (scratch), sin worktree efímero; las tareas se acumulan.')
    base_ref = models.CharField(max_length=200, default='HEAD', help_text='Ref desde donde ramificar (modo repo).')
    participante = models.ForeignKey(
        Participante, on_delete=models.SET_NULL, null=True, blank=True, related_name='tareas',
        help_text='La silla que ejecuta. SET_NULL: si se borra la silla, la Tarea queda HUÉRFANA '
                  '(participante nulo) para que el Líder la reasigne, en vez de bloquear el borrado.',
    )
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    salida = models.TextField(blank=True, help_text='Última salida del agente.')
    creado_at = models.DateTimeField(auto_now_add=True)
    actualizado_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-creado_at']

    def __str__(self):
        return f"Tarea #{self.pk}: {self.titulo} [{self.estado}]"


class Workspace(models.Model):
    """Worktree git aislado de una Tarea. Aislamiento = el principio no negociable:
    el agente trabaja acá, no en el checkout desplegado.
    """
    class Estado(models.TextChoices):
        CREADO = 'creado', 'Creado'
        COMITEADO = 'comiteado', 'Comiteado'
        LIMPIADO = 'limpiado', 'Limpiado'

    tarea = models.OneToOneField(Tarea, on_delete=models.CASCADE, related_name='workspace')
    path = models.CharField(max_length=500, help_text='Ruta del worktree en disco.')
    branch = models.CharField(max_length=200, help_text='Branch aislado (enjambre/...).')
    base_commit = models.CharField(max_length=64, blank=True)
    commit_sha = models.CharField(max_length=64, blank=True, help_text='Commit del resultado, si hubo.')
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.CREADO)
    creado_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"WS {self.branch} ({self.estado})"


class LogMesa(models.Model):
    """Traza de actividad del worker por mesa (5b — visibilidad del flujo).

    A diferencia de Mensaje (la conversación de las sillas), esto es el LOG TÉCNICO: qué hace
    el worker paso a paso (dispatch a cada silla, duración, commits, timeouts, errores). Se
    muestra en un drawer aparte de la mesa, en vivo por SSE; NO entra al contexto de las sillas
    ni a la descarga .txt. CASCADE con la sesión."""
    class Nivel(models.TextChoices):
        INFO = 'info', 'Info'
        PASO = 'paso', 'Paso'      # ▶ arranque de una etapa
        OK = 'ok', 'OK'            # ✓ etapa terminada
        ERROR = 'error', 'Error'   # ✗/⏰ fallo o timeout

    sesion = models.ForeignKey(Sesion, on_delete=models.CASCADE, related_name='logs')
    nivel = models.CharField(max_length=10, choices=Nivel.choices, default=Nivel.INFO)
    texto = models.CharField(max_length=500)
    # Detalle expandible (se ve al hacer hover sobre la línea): p.ej. el diff completo de un
    # commit. Vacío = línea simple sin desplegable.
    detalle = models.TextField(blank=True)
    creado_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"[{self.nivel}] {self.texto[:50]}"


class WorkerRestart(models.Model):
    """Señal web→worker para REINICIAR el worker (recargar código nuevo del repo).

    El worker corre en otro proceso/contenedor que la web. La web (solo CONTROL) encola una
    fila; el worker la consume al inicio de su tick, la borra y se sale (sys.exit) → su
    supervisor (compose `restart: unless-stopped`, o systemd en dev) lo levanta de nuevo
    con el código fresco. Botón en el panel de Sillas."""
    creado_at = models.DateTimeField(auto_now_add=True)
    solicitante = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ['creado_at']

    def __str__(self):
        return f"WorkerRestart @ {self.creado_at:%H:%M:%S} ({self.solicitante})"


class Accion(models.Model):
    """Bitácora del TOOLBELT: cada vez que una silla api:* toca la máquina real (fuera de la
    cápsula de la mesa), queda registrado acá. Es la red de seguridad del nuevo paradigma «salir
    del cascarón»: no hay sandbox, así que TODO lo que una silla hace sobre el sistema se audita.

    Dos clases:
      · LECTURA (inspect/read_file/list_dir/system_report): read-only → se ejecuta SOLA y se
        registra `ejecutada` con su salida. `es_mutacion=False`.
      · MUTACIÓN (apply_fix): cambia el sistema → NUNCA se ejecuta sola. Nace `pendiente`; el
        técnico la aprueba (→ se ejecuta, `ejecutada`/`error`) o la rechaza (`rechazada`).
    """
    class Estado(models.TextChoices):
        PENDIENTE = 'pendiente', 'Pendiente de aprobación'
        EJECUTADA = 'ejecutada', 'Ejecutada'
        RECHAZADA = 'rechazada', 'Rechazada'
        ERROR = 'error', 'Error'

    sesion = models.ForeignKey(Sesion, on_delete=models.CASCADE, related_name='acciones')
    participante = models.ForeignKey(
        Participante, on_delete=models.SET_NULL, null=True, blank=True, related_name='acciones',
    )
    emisor = models.CharField(max_length=100, blank=True, help_text='Nombre de la silla (snapshot).')
    herramienta = models.CharField(max_length=30, help_text='inspect|read_file|list_dir|system_report|apply_fix')
    # True = cambia el sistema (apply_fix) → siempre pasa por aprobación. False = read-only (auto).
    es_mutacion = models.BooleanField(default=False)
    comando = models.TextField(help_text='Comando/ruta que la silla quiso correr.')
    motivo = models.TextField(blank=True, help_text='Por qué (lo explica la silla en apply_fix).')
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.EJECUTADA)
    salida = models.TextField(blank=True, help_text='Salida (stdout/stderr) o razón del rechazo.')
    aprobada_por = models.CharField(max_length=150, blank=True)
    creado_at = models.DateTimeField(auto_now_add=True)
    resuelto_at = models.DateTimeField(null=True, blank=True, help_text='Cuándo se aprobó/rechazó/ejecutó.')

    class Meta:
        ordering = ['-creado_at', '-id']

    def __str__(self):
        return f"Acción #{self.pk}: {self.herramienta} [{self.estado}]"


class AvataresEnjambre(models.Model):
    """Singleton (pk=1) con los retratos de los participantes que NO son sillas:
    el **humano** (los turnos de la gente) y el **Enjambre** (mensajes de sistema —
    `es_sistema`: estado, /deshacer, avisos). Se conserva también un slot **ultron**
    por compatibilidad/futuro (una voz orquestadora), hoy sin uso en Swarm.
    Mismo formato que Participante.avatar: data-URI 1:1 recortado en el navegador. Vacío = fallback."""
    ultron = models.TextField(blank=True, help_text='Retrato 1:1 de la voz orquestadora (reservado), data-URI.')
    enjambre = models.TextField(blank=True, help_text='Retrato 1:1 del Enjambre (mensajes de sistema), data-URI.')
    humano = models.TextField(blank=True, help_text='Retrato 1:1 del humano, data-URI.')
    # Color del chip/borde de cada voz en la mesa (hex #rrggbb, seteable a mano). Vacío = negro.
    color_ultron = models.CharField(max_length=9, blank=True, help_text='Color de la voz orquestadora (hex). Vacío = negro.')
    color_enjambre = models.CharField(max_length=9, blank=True, help_text='Color del Enjambre (hex). Vacío = negro.')
    color_humano = models.CharField(max_length=9, blank=True, help_text='Color del humano (hex). Vacío = negro.')

    def __str__(self):
        return "Avatares del Enjambre (humano + sistema)"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
