"""
enjambre/views.py — capa web. FBV, single-user: sin login humano, todo es rango `control`.

La versión original tenía dos niveles (consulta/control) sobre el perfil del usuario; en
Swarm los helpers de permisos devuelven siempre control y el decorador es un passthrough.
Ambos quedan como punto único donde enchufar auth real si algún día esto se expone
fuera de tu máquina.

El dispatch real de los CLIs lo hace el worker: acá solo se ENCOLA (se persiste el
mensaje del humano / la Tarea pendiente) y se streamea la mesa por SSE.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.models import Max
from django.http import (
    FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify

from .clientes import CLIENTES, build_comando, cliente_de, modelo_de
from .models import (
    LogMesa, Mensaje, Participante, Sesion, Tarea, Topologia, WorkerRestart,
)
from .workspace import mesas_dir, chown_host
from .personas import persona_default

logger = logging.getLogger('enjambre')


# ── Single-user (lo que en el origen era auth + auditoría) ─────────────────────
def requiere_acceso(view):
    """Passthrough: Swarm es single-user en tu máquina, sin login humano. Si algún día
    exponés esto afuera, acá va el auth real (p. ej. login_required de Django)."""
    return view


def log_event(user, action, module, details=None, request=None):
    """La auditoría del portal de origen, reducida a logging estándar (misma firma para no
    tocar las vistas). Acá jamás se loguea contenido de credenciales ni tokens."""
    logger.info("%s | %s | %s", action, module, details or {})


# ── Permisos ──────────────────────────────────────────────────────────────────
# Single-user: hay un solo humano y es el dueño. Los helpers quedan como puntos de
# corte por si alguien repone un rango consulta (las vistas ya los consultan a todos).
def _settings(request):
    return None  # sin perfil de usuario: los getattr sobre esto caen a su default


def _can_access(request):
    return True


def _es_control(request):
    return True


def _puede_controlar(request):
    return True


def _puede_ver_sesion(request, sesion):
    """control ve cualquier mesa; consulta solo las que creó (scope por usuario)."""
    return _es_control(request) or sesion.creador_id == request.user.id


# Cómo se dirigen los agentes al humano (queda en Mensaje.emisor, así las sillas lo ven en
# el contexto y responden tratándolo así). Configurable por env: ENJAMBRE_TITULO_HUMANO.

# Colores de jugador estilo StarCraft 1, asignados POR ORDEN DE SILLA (posicional).
# Silla 1 = el humano (siempre Azul). Las sillas IA agarran las siguientes por orden de id.
PALETA_SC1 = [
    '#4a6cff',  # 1 Azul   — el jugador (humano)
    '#f4433a',  # 2 Rojo   — claude
    '#18b8c4',  # 3 Teal
    '#b14ae0',  # 4 Púrpura
    '#ff8c1a',  # 5 Naranja
    '#b97a56',  # 6 Marrón
    '#6b7280',  # 7 Gris (oscurecido: el blanco #e8e8e8 se lavaba/no se leía)
    '#ffe23a',  # 8 Amarillo
]
COLOR_HUMANO = PALETA_SC1[0]
# Paleta de 16 swatches para el selector de color por silla/voz en el panel (color_ui).
PALETA_16 = [
    '#4a6cff', '#7b5cff', '#b14ae0', '#ff5db1', '#f4433a', '#ff7043', '#ff8c1a', '#ffc61a',
    '#ffe23a', '#a0e020', '#18b8c4', '#00d977', '#b97a56', '#9aa0a6', '#6b7280', '#e8e8e8',
]
COLOR_DEFAULT = PALETA_SC1[0]


def _color_limpio(val):
    """Valida un color del form: hex #rrggbb. Si no, '' (→ fallback)."""
    val = (val or '').strip()
    return val if re.fullmatch(r'#[0-9a-fA-F]{6}', val) else ''


def _avatar_limpio(val):
    """Valida un avatar del form: data-URI de imagen y no gigante (el front recorta a 128px →
    ~10-30 KB; el tope es red de seguridad para no inflar la DB). Si no, ''."""
    val = (val or '').strip()
    if val and val.startswith('data:image/') and len(val) <= 200_000:
        return val
    return ''


def _colores_por_silla():
    """Devuelve (sillas_con_color, mapa_id→color). El color lo fija el humano por silla
    (`color_ui`); si está vacío, cae al color posicional estilo StarCraft (por orden de id,
    desplazado porque el asiento 1 es el humano). Estable: no depende de activo."""
    todas = list(Participante.objects.order_by('id'))
    mapa = {}
    for i, p in enumerate(todas):
        p.color = p.color_ui or PALETA_SC1[(i + 1) % len(PALETA_SC1)]
        mapa[p.id] = p.color
    return todas, mapa


def _colores_globales():
    """Colores (hex) de las voces que NO son sillas: Enjambre (sistema) y humano. Del singleton
    AvataresEnjambre; vacío = fallback. Espejo de los avatares globales."""
    from .models import AvataresEnjambre
    esp = AvataresEnjambre.get()
    return (esp.color_enjambre or COLOR_DEFAULT, esp.color_humano or COLOR_HUMANO)


def _color_de(m, color_map, c_enjambre, c_humano):
    """Color de UN mensaje, en paralelo a `_avatar_de`: silla → su color; sistema → Enjambre;
    resto → humano."""
    if m.participante_id:
        return color_map.get(m.participante_id, c_humano)
    if m.es_sistema:
        return c_enjambre
    return c_humano


def _avatares():
    """Retratos (data-URI) para los mensajes: mapa id_silla→avatar + los dos globales
    (Enjambre para los mensajes de sistema, humano para los turnos de la gente)."""
    from .models import AvataresEnjambre
    mapa = {p.id: p.avatar for p in Participante.objects.exclude(avatar='').only('id', 'avatar')}
    esp = AvataresEnjambre.get()
    return mapa, esp.enjambre, esp.humano


def _avatar_de(m, avatar_map, av_enjambre, av_humano):
    """Avatar de UN mensaje: silla → su avatar; sistema (es_sistema) → Enjambre; resto → humano.
    Cadena vacía = sin avatar → la UI cae al cuadrado de color."""
    if m.participante_id:
        return avatar_map.get(m.participante_id, '')
    if m.es_sistema:
        return av_enjambre
    return av_humano


def _es_humano(m):
    """¿El mensaje es de una persona? Solo entonces va anclado a la derecha (estilo WhatsApp).
    Los avisos de sistema (es_sistema, 'Enjambre') NO son del humano aunque no tengan silla."""
    return not m.participante_id and not m.es_sistema


def _modelo_corto(m):
    """Modelo/motor de la silla para la etiqueta de la UI, recortado. Solo display. '' para
    humano/sistema."""
    if not m.participante_id:
        return ''
    return (m.participante.motor or '').split('/')[-1].split(':')[0]


def _titulo_humano(request):
    titulo = (getattr(settings, 'ENJAMBRE_TITULO_HUMANO', '') or '').strip()
    return titulo or getattr(request.user, 'username', '') or 'Humano'


# ── Serialización ───────────────────────────────────────────────────────────────
def _mensaje_dict(m, color_map, colores, avatares=None):
    d = {
        'id': m.id,
        'emisor': m.emisor,
        'texto': m.texto,
        'es_ruido': m.es_ruido,
        'es_humano': _es_humano(m),
        'participante': m.participante.key if m.participante_id else None,
        'color': _color_de(m, color_map, *colores),
        'modelo': _modelo_corto(m),
        'creado_at': m.creado_at.isoformat(),
    }
    if avatares is not None:
        d['avatar'] = _avatar_de(m, *avatares)
    return d


def _fetch_mensajes_since(sesion_id, last_id, color_map, colores, avatares=None):
    qs = (Mensaje.objects.filter(sesion_id=sesion_id, id__gt=last_id)
          .select_related('participante').order_by('id'))
    data = [_mensaje_dict(m, color_map, colores, avatares) for m in qs]
    if data:
        last_id = data[-1]['id']
    return data, last_id


# ── Vistas ──────────────────────────────────────────────────────────────────────
@requiere_acceso
def home(request):
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesiones = (Sesion.objects.all() if _es_control(request)
                else Sesion.objects.filter(creador=request.user))
    # Fijadas (pin) arriba, después por fecha. Como Brain.
    sesiones = sesiones.order_by('-fijada', '-creado_at')
    sesiones = list(sesiones.prefetch_related('participantes')[:50])
    for s in sesiones:
        s.sel_ids = {p.id for p in s.participantes.all()}
    # Sillas que el usuario puede sentar: control = todas las activas; consulta = solo las
    # permitidas (protege los tokens caros).
    activas = Participante.objects.filter(activo=True).order_by('orden', 'key')
    if not _es_control(request):
        activas = activas.filter(permitir_consulta=True)
    todas = list(Participante.objects.order_by('orden', 'key'))  # para el menú de gestión
    for p in todas:
        p.persona_default = persona_default(p)  # default de fábrica para el botón Reset
    from . import onboarding
    # El banner de la escalera se muestra hasta completar el escalón 2 (keys en la bóveda).
    escalera = None if onboarding.completa() else onboarding.escalones()
    return render(request, 'enjambre/home.html', {
        'sesiones': sesiones,
        'sillas_activas': activas,
        'todas_sillas': todas,
        'puede_controlar': _puede_controlar(request),
        'escalera': escalera,
        'escalera_listos': onboarding.listos(escalera) if escalera else 0,
    })


@requiere_acceso
def crear_sesion(request):
    # Cualquier usuario con acceso crea sus propias mesas (consulta incluida); el scope por
    # creador las mantiene separadas. Fabricar Tareas y borrar siguen siendo solo control.
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()
        # Single-user sin login: no hay usuario autenticado que registrar como creador.
        creador = request.user if request.user.is_authenticated else None
        sesion = Sesion.objects.create(nombre=nombre or 'Mesa', creador=creador)
        # Sin nombre (botón «+ Nueva») → nombre default numerado por id, distinguible en la lista
        # y renombrable con el lápiz. El buscador de arriba filtra; acá solo se crea.
        if not nombre:
            sesion.nombre = f'Mesa {sesion.pk}'
            sesion.save(update_fields=['nombre'])
        # Arranca con las sillas activas que el usuario puede usar (consulta = solo permitidas,
        # nunca vacío para que el fallback "todas las activas" del engine no cuele una paga).
        iniciales = Participante.objects.filter(activo=True)
        if not _es_control(request):
            iniciales = iniciales.filter(permitir_consulta=True)
        sesion.participantes.set(iniciales)
        # Crear la carpeta de la mesa YA (no perezosamente). Si no existe, subir un archivo
        # antes de que una silla fabrique lo guardaba como archivo suelto `mesa-<id>` →
        # rompía el worker (mkdir exist_ok igual revienta si el path es un archivo). El
        # git init + workspace_dir (ruta host) los sigue poniendo el worker vía mesa_workspace.
        try:
            d = _mesa_dir_container(sesion.id)
            d.mkdir(parents=True, exist_ok=True)
            chown_host(d)  # el worker (host) tiene que poder git-init acá si el web corre como root
        except OSError:
            pass
        log_event(request.user, 'ENJAMBRE_SESION_CREATE', 'enjambre',
                  {'pk': sesion.pk, 'nombre': nombre}, request)
    # La mesa aparece en la lista; no se entra directo.
    return redirect('enjambre:home')


@requiere_acceso
def borrar_sesion(request, pk):
    """Borra una mesa (y en cascada sus mensajes/tareas). Solo control."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control puede borrar mesas.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if request.method == 'POST':
        nombre = sesion.nombre
        sesion.delete()
        log_event(request.user, 'ENJAMBRE_SESION_DELETE', 'enjambre',
                  {'pk': pk, 'nombre': nombre}, request)
        return redirect('enjambre:home')
    return redirect('enjambre:mesa', pk=pk)


@requiere_acceso
def guardar_config(request, pk):
    """Guarda qué sillas participan en la mesa (tuerquita → modal de checkboxes).
    Puede configurar quien ve la mesa (creador o control) y no es read-only."""
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion) or getattr(_settings(request), 'is_read_only', False):
        return HttpResponseForbidden("No podés configurar esta mesa.")
    if request.method == 'POST':
        keys = request.POST.getlist('sillas')
        sillas = Participante.objects.filter(key__in=keys)
        # consulta no puede sentar sillas no permitidas, ni forzándolas por POST.
        if not _es_control(request):
            sillas = sillas.filter(permitir_consulta=True)
        # Mesa de trabajo (Líder): las sillas de modelo local (HTTP) no pueden fabricar → ni se
        # sientan, para que no ocupen lugar sin poder actuar. Vale para la topología que va a quedar
        # (la del POST si viene del modal, o la actual si el seat viene del listado, que no manda topo).
        topo_post = request.POST.get('topologia')
        sera_lider = (topo_post == Topologia.LIDER) if topo_post in (Topologia.PLANA, Topologia.LIDER) \
            else (sesion.topologia == Topologia.LIDER)
        if sera_lider:
            sillas = sillas.filter(endpoint_url='')
        sesion.participantes.set(sillas)
        keys_ok = list(sillas.values_list('key', flat=True))
        # Topología + líder: SOLO si el POST los trae (el modal de la mesa). El auto-save de sillas
        # del listado no manda 'topologia' → no debe pisar/borrar el líder ya configurado.
        if 'topologia' in request.POST:
            # El líder debe ser una silla SENTADA en la mesa (o, si no hay selección explícita =
            # mesa con todas las activas, cualquier silla activa permitida).
            topo = request.POST.get('topologia')
            if topo in (Topologia.PLANA, Topologia.LIDER):
                sesion.topologia = topo
            lider_key = request.POST.get('lider', '')
            lider = None
            if sesion.topologia == Topologia.LIDER and lider_key:
                cand = Participante.objects.filter(key=lider_key, activo=True)
                if not _es_control(request):
                    cand = cand.filter(permitir_consulta=True)
                cand = cand.first()
                if cand and (not keys_ok or cand.key in keys_ok):
                    lider = cand
            sesion.lider = lider
            if sesion.topologia == Topologia.LIDER and lider is None:
                sesion.topologia = Topologia.PLANA  # líder sin silla válida → no dejar la mesa muda
            sesion.save(update_fields=['topologia', 'lider'])
        # Tope de costo del modo continuo. Solo si el POST lo trae (modal de la mesa).
        if 'costo_tope' in request.POST:
            from decimal import Decimal, InvalidOperation
            try:
                tope = Decimal(str(request.POST.get('costo_tope') or '0').replace(',', '.'))
            except (InvalidOperation, ValueError):
                tope = Decimal('0')
            sesion.costo_tope = tope if tope > 0 else Decimal('0')
            sesion.save(update_fields=['costo_tope'])
        log_event(request.user, 'ENJAMBRE_SESION_CONFIG', 'enjambre',
                  {'pk': sesion.pk, 'sillas': keys_ok,
                   'topologia': sesion.topologia, 'lider': sesion.lider.key if sesion.lider else None},
                  request)
    # Auto-save inline (fetch): sin redirect, 204 y listo.
    if request.headers.get('X-Requested-With') == 'fetch':
        return HttpResponse(status=204)
    # Si la config se editó desde la mesa abierta, volvemos a la mesa (sillas en vivo).
    if request.POST.get('origen') == 'mesa':
        return redirect('enjambre:mesa', pk=sesion.pk)
    return redirect('enjambre:home')


@requiere_acceso
def renombrar_sesion(request, pk):
    """Renombra una mesa (lápiz ✎ inline en el listado). Mismo permiso que guardar_config:
    el creador (incl. consulta en sus mesas) o control; nunca read-only."""
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion) or getattr(_settings(request), 'is_read_only', False):
        return HttpResponseForbidden("No podés renombrar esta mesa.")
    if request.method == 'POST':
        nombre = request.POST.get('nombre', '').strip()[:200]
        if nombre:
            sesion.nombre = nombre
            sesion.save(update_fields=['nombre'])
            log_event(request.user, 'ENJAMBRE_SESION_RENAME', 'enjambre',
                      {'pk': sesion.pk, 'nombre': nombre}, request)
    if request.headers.get('X-Requested-With') == 'fetch':
        return JsonResponse({'ok': True, 'nombre': sesion.nombre})
    return redirect('enjambre:home')


@requiere_acceso
def fijar_sesion(request, pk):
    """Toggle del PIN de la mesa (como en Brain): las fijadas van arriba en el listado. Mismo
    permiso que renombrar: el creador (incl. consulta en sus mesas) o control; nunca read-only."""
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion) or getattr(_settings(request), 'is_read_only', False):
        return HttpResponseForbidden("No podés fijar esta mesa.")
    if request.method == 'POST':
        sesion.fijada = not sesion.fijada
        sesion.save(update_fields=['fijada'])
        log_event(request.user, 'ENJAMBRE_SESION_PIN' if sesion.fijada else 'ENJAMBRE_SESION_UNPIN',
                  'enjambre', {'pk': sesion.pk}, request)
        return JsonResponse({'id': sesion.pk, 'fijada': sesion.fijada})
    return JsonResponse({'ok': False}, status=405)


@requiere_acceso
def mesa(request, pk):
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    todas, color_map = _colores_por_silla()
    colores = _colores_globales()
    avatar_map, av_enjambre, av_humano = _avatares()
    mensajes = list(sesion.mensajes.select_related('participante').order_by('id'))
    for m in mensajes:
        m.color = _color_de(m, color_map, *colores)
        m.avatar = _avatar_de(m, avatar_map, av_enjambre, av_humano)
        m.es_humano = _es_humano(m)
        m.modelo = _modelo_corto(m)
    # Sillas de la mesa (∩ activas); vacío = todas las activas (espejo del engine).
    sel_ids = set(sesion.participantes.values_list('id', flat=True))
    mesa_sillas = [p for p in todas if p.activo and (not sel_ids or p.id in sel_ids)]
    # Para el modal de sillas en vivo: las que el usuario puede sentar (consulta = permitidas).
    seleccionables = Participante.objects.filter(activo=True).order_by('orden', 'key')
    if not _es_control(request):
        seleccionables = seleccionables.filter(permitir_consulta=True)
    # Flujo de trabajo (log técnico): SOLO CONTROL (sea la mesa plana o líder). Últimos 200.
    puede_log = _es_control(request)
    logs = []
    costo_total = tokens_total = 0
    gasto_por_silla = []
    if puede_log:
        logs = list(sesion.logs.order_by('-id')[:200])
        logs.reverse()
        # Velocímetro: gasto acumulado de la mesa. Solo CONTROL (es feature de costos).
        from django.db.models import Sum
        agg = sesion.mensajes.aggregate(c=Sum('costo'), t=Sum('tokens'))
        costo_total = agg['c'] or 0
        tokens_total = agg['t'] or 0
        # Desglose por silla (hover del badge): quién gastó cuánto. Agrupa por emisor (snapshot que
        # sobrevive al borrado de la silla); ignora humano/sistema (sin tokens).
        gasto_por_silla = list(
            sesion.mensajes.filter(tokens__gt=0).values('emisor')
            .annotate(tok=Sum('tokens'), usd=Sum('costo')).order_by('-usd', '-tok'))
    return render(request, 'enjambre/mesa.html', {
        'sesion': sesion,
        'mensajes': mensajes,
        'sillas': mesa_sillas,
        'sillas_activas': seleccionables,
        'sel_ids': sel_ids,
        'puede_controlar': _puede_controlar(request),
        'puede_log': puede_log,
        'logs': logs,
        'costo_total': costo_total,
        'tokens_total': tokens_total,
        'gasto_por_silla': gasto_por_silla,
        'color_humano': COLOR_HUMANO,
    })


@requiere_acceso
def preguntar(request, pk):
    """Encola una pregunta del humano (chat). Permitido a consulta y control."""
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    if request.method == 'POST':
        texto = request.POST.get('texto', '').strip()
        from .engine import parse_comando
        comando, limpio = parse_comando(texto) if texto else (None, texto)
        # Enforcement de rango: CONSULTA solo charla. Si tipea un comando (/armar, /deshacer,
        # /debate, /alto) se descarta el verbo y queda como charla — nunca corta ni edita.
        if texto and not _puede_controlar(request):
            if comando:
                texto, comando = limpio, None
        # /alto (CONTROL): freno de mano. NO se encola como mensaje (quedaría enterrado bajo las
        # respuestas de las sillas y el worker nunca lo vería). Prende la señal que el engine lee
        # ENTRE sillas → corta el resto del turno en curso. El ack va como mensaje de sistema.
        if comando == 'alto' and _puede_controlar(request):
            Sesion.objects.filter(pk=sesion.pk).update(detener_solicitado=True, continuo=False)
            Mensaje.objects.create(
                sesion=sesion, emisor='Enjambre', es_sistema=True,
                texto='🛑 Alto pedido. Corto la mesa apenas termine la silla en curso '
                      '(si no hay nada corriendo, no hay nada que cortar).')
            log_event(request.user, 'ENJAMBRE_ALTO', 'enjambre', {'sesion': sesion.pk}, request)
            return redirect('enjambre:mesa', pk=sesion.pk)
        if texto:
            Mensaje.objects.create(sesion=sesion, emisor=_titulo_humano(request), texto=texto)
            log_event(request.user, 'ENJAMBRE_PREGUNTA', 'enjambre',
                      {'sesion': sesion.pk}, request)
    return redirect('enjambre:mesa', pk=sesion.pk)


@requiere_acceso
def crear_tarea(request, pk):
    """DORMIDA: el botón de fabricar se dio de baja; fabricar ahora es el comando «/armar» en la mesa
    (chat-en-carpeta, ver engine.responder). Esta vista + el modo-repo (worktree→branch) quedan
    parqueados para la fase Líder/multi-silla; ningún template la invoca. NO borrar: vuelve con la fase Líder/multi-silla.

    Encola una Tarea de fabricación. Solo control."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control puede mandar a fabricar.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if request.method == 'POST':
        silla = Participante.objects.filter(key=request.POST.get('silla'), activo=True).first()
        titulo = request.POST.get('titulo', '').strip()
        ordenes = request.POST.get('ordenes', '').strip()
        # modo scratch (default) = carpeta persistente de la mesa; modo repo = repo existente.
        modo = request.POST.get('modo', 'scratch')
        repo_path = request.POST.get('repo_path', '').strip()
        persistente = (modo != 'repo')
        # scratch no necesita repo_path; repo sí.
        ok = silla and titulo and ordenes and (persistente or repo_path)
        if ok:
            tarea = Tarea.objects.create(
                sesion=sesion, titulo=titulo, ordenes=ordenes,
                persistente=persistente,
                repo_path='' if persistente else repo_path,
                base_ref=request.POST.get('base_ref', '').strip() or 'HEAD',
                participante=silla,
            )
            log_event(request.user, 'ENJAMBRE_TAREA_CREATE', 'enjambre',
                      {'tarea': tarea.pk, 'sesion': sesion.pk, 'silla': silla.key,
                       'modo': 'scratch' if persistente else 'repo', 'repo': repo_path}, request)
    return redirect('enjambre:mesa', pk=sesion.pk)


@requiere_acceso
def stream(request, pk):
    """SSE de mensajes nuevos de la sesión (mismo patrón que chat/aventuras)."""
    if not _can_access(request):
        return HttpResponseForbidden("Sin acceso al Enjambre.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    _, color_map = _colores_por_silla()
    colores = _colores_globales()
    avatares = _avatares()
    try:
        since_id = int(request.headers.get('Last-Event-ID') or request.GET.get('since', 0))
    except (ValueError, TypeError):
        since_id = 0

    def event_stream(last_id):
        yield ': ok\n\n'  # abre el stream de inmediato
        idle = 0
        while True:
            try:
                data, last_id = _fetch_mensajes_since(sesion.id, last_id, color_map, colores, avatares)
            except Exception:
                connection.close()
                break
            finally:
                connection.close()  # no retener la conexión PG durante el sleep
            if data:
                yield f'id: {last_id}\ndata: {json.dumps({"messages": data})}\n\n'
                idle = 0
            else:
                idle += 1
                if idle % 15 == 0:  # heartbeat ~cada 30s
                    yield ': ping\n\n'
            # time.sleep (no gevent): bajo el runserver threaded que usa Swarm cada SSE vive en su
            # propio hilo → bloquear ese hilo 2s es correcto y no necesita gevent (dep nativa que
            # sacamos del bundle portátil). Bajo un worker gevent monkey-patcheado sería cooperativo igual.
            time.sleep(2)

    resp = StreamingHttpResponse(event_stream(since_id), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp


@requiere_acceso
def log_stream(request, pk):
    """SSE del LOG DE ACTIVIDAD de la mesa (drawer de flujo). SOLO CONTROL — el flujo técnico es
    del dueño, no de consulta. Mismo patrón que stream(), pero sobre LogMesa."""
    if not _es_control(request):
        return HttpResponseForbidden("El flujo de trabajo es solo para control.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    try:
        since_id = int(request.headers.get('Last-Event-ID') or request.GET.get('since', 0))
    except (ValueError, TypeError):
        since_id = 0

    def event_stream(last_id):
        yield ': ok\n\n'
        idle = 0
        while True:
            try:
                qs = (LogMesa.objects.filter(sesion_id=sesion.id, id__gt=last_id)
                      .order_by('id'))
                data = [{'id': lg.id, 'nivel': lg.nivel, 'texto': lg.texto,
                         'detalle': lg.detalle, 'creado_at': lg.creado_at.isoformat()}
                        for lg in qs]
            except Exception:
                connection.close()
                break
            finally:
                connection.close()
            if data:
                last_id = data[-1]['id']
                yield f'id: {last_id}\ndata: {json.dumps({"logs": data})}\n\n'
                idle = 0
            else:
                idle += 1
                if idle % 15 == 0:
                    yield ': ping\n\n'
            time.sleep(2)  # ver nota en stream(): sin gevent, correcto bajo runserver threaded

    resp = StreamingHttpResponse(event_stream(since_id), content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'
    return resp


# ── Carpeta de la mesa: explorar / descargar / copiar ruta (solo CONTROL) ─────────
def _mesa_dir_container(pk):
    """Ruta de la carpeta de la mesa para LEER/SERVIR archivos desde la web. En el compose,
    ~/.enjambre se monta con la MISMA ruta en host y contenedor (paridad), así que mesas_dir()
    resuelve igual acá que en el worker. La ruta del HOST sale de sesion.workspace_dir."""
    return Path(mesas_dir()) / f"mesa-{pk}"


@requiere_acceso
def mesa_archivos(request, pk):
    """Lista (JSON) los archivos de la carpeta de la mesa. Solo CONTROL. Excluye `.git`."""
    if not _es_control(request):
        return HttpResponseForbidden("La carpeta de la mesa es solo para control.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    base = _mesa_dir_container(pk)
    # workspace_dir lo setea el worker recién al primer fabricar; mientras tanto, si la carpeta ya
    # existe (se crea eager al crear la mesa), mostramos su ruta. Hay paridad host/contenedor
    # (~/.enjambre montado igual en ambos), así que la ruta del contenedor ES la del host.
    host = sesion.workspace_dir or (str(base) if base.exists() else '')
    if not base.exists():
        return JsonResponse({'existe': False, 'host_path': host, 'archivos': []})
    archivos = []
    for root, dirs, files in os.walk(base):
        # Excluir dot-dirs (.git, .antigravitycli y runtime de las CLIs) y __pycache__ del listado.
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for f in files:
            if f.startswith('.'):
                continue  # ocultar dotfiles (.gitignore, .env, runtime de CLIs, etc.)
            full = Path(root) / f
            try:
                st = full.stat()
            except OSError:
                continue  # symlink roto / archivo transitorio: no listarlo
            archivos.append({'path': str(full.relative_to(base)),
                             'size': st.st_size, 'mtime': int(st.st_mtime)})
            if len(archivos) >= 2000:  # tope defensivo
                break
        if len(archivos) >= 2000:
            break
    archivos.sort(key=lambda a: a['path'].lower())
    return JsonResponse({'existe': True, 'host_path': host, 'archivos': archivos})


@requiere_acceso
def mesa_archivo(request, pk):
    """Descarga UN archivo de la carpeta de la mesa (?path=relativo). Solo CONTROL.
    Anti path-traversal: el destino resuelto debe quedar dentro de la carpeta de la mesa."""
    if not _es_control(request):
        return HttpResponseForbidden("La carpeta de la mesa es solo para control.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    rel = (request.GET.get('path') or '').strip()
    if not rel:
        raise Http404("Falta path.")
    base = _mesa_dir_container(pk).resolve()
    target = (base / rel).resolve()
    # Debe quedar DENTRO de la carpeta de la mesa y no ser parte del repo git interno.
    if base != target and base not in target.parents:
        return HttpResponseForbidden("Ruta fuera de la carpeta de la mesa.")
    if '.git' in target.relative_to(base).parts:
        return HttpResponseForbidden("No.")
    if not target.is_file():
        raise Http404("No es un archivo.")
    log_event(request.user, 'ENJAMBRE_ARCHIVO_GET', 'enjambre',
              {'sesion': pk, 'path': rel}, request)
    return FileResponse(open(target, 'rb'), as_attachment=True, filename=target.name)


@requiere_acceso
def mesa_zip(request, pk):
    """Descarga TODA la carpeta de la mesa en un solo .zip (excluye `.git`). Solo CONTROL.
    Evita bajar archivo por archivo: arma el zip en memoria y lo manda como adjunto."""
    if not _es_control(request):
        return HttpResponseForbidden("La carpeta de la mesa es solo para control.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    import io
    import zipfile
    base = _mesa_dir_container(pk)
    if not base.exists():
        raise Http404("La carpeta de la mesa todavía no existe.")
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(base):
            # Excluir dot-dirs (.git, .antigravitycli y demás runtime de las CLIs) y __pycache__:
            # no son el resultado y suelen tener symlinks rotos/archivos transitorios.
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            for f in files:
                if f.startswith('.'):
                    continue  # no incluir dotfiles en la descarga (coincide con el listado)
                full = Path(root) / f
                try:
                    if full.is_file() and not full.is_symlink():
                        z.write(full, full.relative_to(base))
                        n += 1
                except (OSError, ValueError):
                    continue  # archivo transitorio / roto / desaparecido: saltear, no romper el zip
                if n >= 2000:  # tope defensivo (igual que el listado)
                    break
            if n >= 2000:
                break
    buf.seek(0)
    log_event(request.user, 'ENJAMBRE_ZIP_GET', 'enjambre', {'sesion': pk, 'archivos': n}, request)
    resp = HttpResponse(buf.getvalue(), content_type='application/zip')
    resp['Content-Disposition'] = f'attachment; filename="mesa-{pk}.zip"'
    return resp


@requiere_acceso
def mesa_subir(request, pk):
    """Sube archivos a la carpeta de la mesa (solo CONTROL). Sirve para sembrar la mesa con input
    antes de /armar y para sumar archivos a mitad de trabajo. NO usa git (escribe al volumen
    compartido; el worker commitea sola al próximo turno) y el motor los inyecta a las sillas
    listando el disco (ver engine._estado_carpeta). Anti path-traversal: solo el basename, dentro
    de la carpeta de la mesa."""
    if not _es_control(request):
        return HttpResponseForbidden("La carpeta de la mesa es solo para control.")
    sesion = get_object_or_404(Sesion, pk=pk)
    if not _puede_ver_sesion(request, sesion):
        return HttpResponseForbidden("Esta mesa no es tuya.")
    if request.method != 'POST':
        return HttpResponseForbidden("Método no permitido.")

    files = request.FILES.getlist('archivos')
    if not files:
        return JsonResponse({'ok': False, 'error': 'No se recibieron archivos.'}, status=400)

    dest = _mesa_dir_container(pk)
    dest.mkdir(parents=True, exist_ok=True)
    dest_real = str(dest.resolve())
    guardados, rechazados = [], []
    for f in files:
        name = os.path.basename((f.name or '').strip())
        # Sin nombre, dotfiles ocultos ni rutas que escapen de la carpeta de la mesa.
        if not name or name.startswith('.'):
            rechazados.append(f.name or '(sin nombre)')
            continue
        target = (dest / name).resolve()
        if not str(target).startswith(dest_real + os.sep):
            rechazados.append(name)
            continue
        try:
            with open(target, 'wb') as out:
                for chunk in f.chunks():
                    out.write(chunk)
        except OSError as e:
            rechazados.append(f"{name} ({e})")
            continue
        guardados.append(name)

    if not guardados:
        return JsonResponse({'ok': False, 'error': 'Ningún archivo válido.', 'rechazados': rechazados}, status=400)

    chown_host(dest)  # si el web corre como root, dejar los archivos al worker del host

    quien = getattr(request.user, 'username', '') or 'el humano'
    # Dejar la subida en el flujo de la mesa para que quede en el historial (y se vea en el chat).
    try:
        from .models import Mensaje
        Mensaje.objects.create(
            sesion=sesion, emisor='Enjambre', es_sistema=True,
            texto=(f"📎 {quien} subió {len(guardados)} archivo(s) a la carpeta: "
                   + ", ".join(guardados[:15]) + ("…" if len(guardados) > 15 else "")
                   + ". Ya están disponibles para las sillas en el próximo turno."),
        )
    except Exception:  # noqa: BLE001
        pass

    log_event(request.user, 'ENJAMBRE_SUBIR', 'enjambre', {'pk': pk, 'archivos': guardados}, request)
    return JsonResponse({'ok': True, 'guardados': guardados, 'rechazados': rechazados})


def _key_unica(nombre):
    """Slug único para una silla nueva/clonada (clave estable de la silla)."""
    base = slugify(nombre)[:40] or 'silla'
    key = base
    i = 2
    while Participante.objects.filter(key=key).exists():
        key = f"{base}-{i}"
        i += 1
    return key


def _aplicar_form_silla(silla, post):
    """Aplica el form COMPLETO de una card del panel a una silla. El AJAX manda toda la card en
    cada guardado, así un campo faltante no blanquea otro. cliente+modelo → comando/comando_trabajo
    (o endpoint_url/endpoint_model si es ollama)."""
    silla.nombre = (post.get('nombre') or silla.nombre or 'Silla').strip()[:100]
    cliente = post.get('cliente', '')
    modelo = (post.get('modelo') or '').strip()
    if cliente == 'ollama':
        silla.endpoint_url = (post.get('endpoint_url') or '').strip()
        silla.endpoint_model = modelo
        silla.comando = []
        silla.comando_trabajo = []
    elif cliente in CLIENTES:
        silla.comando, silla.comando_trabajo = build_comando(cliente, modelo)
        silla.endpoint_url = ''
        silla.endpoint_model = ''
    silla.persona = (post.get('persona_a') or '').strip()
    # persona_consulta (variante B) y permitir_consulta son del modelo multiusuario del origen:
    # en Swarm no hay rango consulta, el form ya no los manda y acá no se tocan (ver docstring
    # del módulo). Se aplican solo si algún día un form los repone.
    if 'persona_b' in post:
        silla.persona_consulta = (post.get('persona_b') or '').strip()
    silla.recordatorio = (post.get('recordatorio') or '').strip()
    silla.especialidad = (post.get('especialidad') or '').strip()[:120]
    silla.rol_tarjeta = (post.get('rol_tarjeta') or '').strip()[:40]
    # Color/avatar de la silla en la mesa (guardados solo si el form los trae, para no blanquear
    # el avatar pesado en un save parcial).
    if 'color_ui' in post:
        silla.color_ui = _color_limpio(post.get('color_ui'))
    if 'avatar' in post:
        silla.avatar = _avatar_limpio(post.get('avatar'))
    if 'rango' in post:
        silla.permitir_consulta = (post.get('rango') == 'consulta')
    silla.activo = bool(post.get('activo'))
    try:
        silla.orden = int(post.get('orden'))
    except (TypeError, ValueError):
        pass
    silla.save()


@requiere_acceso
def gestionar_sillas(request):
    """Panel de Sillas (vista completa): CRUD — nombre, cliente+modelo, prompt, recordatorio,
    especialidad, avatar/color, activo, orden. Guardado AJAX por card (ver enjambre/sillas.html)."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control puede gestionar las sillas.")
    sillas = list(Participante.objects.order_by('orden', 'key'))
    for p in sillas:
        p.persona_default = persona_default(p)
        p.cliente_sel = cliente_de(p)
        p.modelo_sel = modelo_de(p)
    # Metadata por cliente para el front (poblar el <select> de modelo y mostrar/ocultar campos).
    clientes_meta = {k: {'modelos': c.get('modelos', []),
                         'http': bool(c.get('http')),
                         'model': bool(c.get('model_flag')),
                         'sin_key': bool(c.get('sin_key'))}
                     for k, c in CLIENTES.items()}
    from .models import AvataresEnjambre
    esp = AvataresEnjambre.get()
    avatares_voces = [
        {'key': 'humano', 'label': 'Humano (vos)', 'avatar': esp.humano,
         'color': esp.color_humano or COLOR_HUMANO},
        {'key': 'enjambre', 'label': 'Enjambre (sistema)', 'avatar': esp.enjambre,
         'color': esp.color_enjambre or COLOR_DEFAULT},
    ]
    return render(request, 'enjambre/sillas.html',
                  {'sillas': sillas, 'clientes': CLIENTES, 'clientes_meta': clientes_meta,
                   'avatares_voces': avatares_voces, 'paleta16': PALETA_16})


@requiere_acceso
def guardar_avatares(request):
    """Guarda los retratos del Enjambre (mensajes de sistema) y del humano (singleton
    AvataresEnjambre). Solo control. AJAX."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method == 'POST':
        from .models import AvataresEnjambre
        esp = AvataresEnjambre.get()
        if 'enjambre' in request.POST:
            esp.enjambre = _avatar_limpio(request.POST.get('enjambre'))
        if 'humano' in request.POST:
            esp.humano = _avatar_limpio(request.POST.get('humano'))
        if 'color_enjambre' in request.POST:
            esp.color_enjambre = _color_limpio(request.POST.get('color_enjambre'))
        if 'color_humano' in request.POST:
            esp.color_humano = _color_limpio(request.POST.get('color_humano'))
        esp.save()
        log_event(request.user, 'ENJAMBRE_AVATARES_SAVE', 'enjambre', {}, request)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True})
    return redirect('enjambre:gestionar_sillas')


@requiere_acceso
def guardar_silla(request, key):
    """Guarda una silla (AJAX por card). JSON si es XHR; redirect al panel como fallback."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    silla = get_object_or_404(Participante, key=key)
    if request.method == 'POST':
        _aplicar_form_silla(silla, request.POST)
        log_event(request.user, 'ENJAMBRE_SILLA_SAVE', 'enjambre', {'silla': key}, request)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True})
    return redirect('enjambre:gestionar_sillas')


@requiere_acceso
def crear_silla(request):
    """Crea una silla nueva (default opencode sin modelo); se edita inline después."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method == 'POST':
        nombre_in = (request.POST.get('nombre') or '').strip()[:100]
        nombre = nombre_in or 'Silla'
        cmd, cmdt = build_comando('opencode', '')
        orden = (Participante.objects.aggregate(m=Max('orden'))['m'] or 0) + 1
        silla = Participante.objects.create(
            key=_key_unica(nombre), nombre=nombre, comando=cmd, comando_trabajo=cmdt,
            activo=True, permitir_consulta=False, orden=orden,
        )
        # Sin nombre (botón «+ Nueva») → nombre default numerado por id (se renombra inline).
        if not nombre_in:
            silla.nombre = f'Silla {silla.pk}'
            silla.save(update_fields=['nombre'])
        log_event(request.user, 'ENJAMBRE_SILLA_CREATE', 'enjambre', {'silla': silla.key}, request)
    return redirect('enjambre:gestionar_sillas')


@requiere_acceso
def clonar_silla(request, key):
    """Duplica una silla (misma config, key nueva): repetir con otro modelo/prompt."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method == 'POST':
        src = get_object_or_404(Participante, key=key)
        nombre = f"{src.nombre} (copia)"[:100]
        orden = (Participante.objects.aggregate(m=Max('orden'))['m'] or 0) + 1
        nueva = Participante.objects.create(
            key=_key_unica(nombre), nombre=nombre,
            comando=list(src.comando), comando_trabajo=list(src.comando_trabajo),
            endpoint_url=src.endpoint_url, endpoint_model=src.endpoint_model,
            permitir_consulta=src.permitir_consulta, color=src.color,
            color_ui=src.color_ui, avatar=src.avatar, rol_tarjeta=src.rol_tarjeta,
            persona=src.persona, persona_consulta=src.persona_consulta,
            recordatorio=src.recordatorio, especialidad=src.especialidad,
            rol=src.rol, activo=src.activo, orden=orden,
        )
        log_event(request.user, 'ENJAMBRE_SILLA_CLONE', 'enjambre',
                  {'src': key, 'nueva': nueva.key}, request)
    return redirect('enjambre:gestionar_sillas')


@requiere_acceso
def borrar_silla(request, key):
    """Borra una silla. Tarea.participante es SET_NULL → las tareas quedan huérfanas, no bloquean."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method == 'POST':
        silla = get_object_or_404(Participante, key=key)
        nombre = silla.nombre
        silla.delete()
        log_event(request.user, 'ENJAMBRE_SILLA_DELETE', 'enjambre',
                  {'silla': key, 'nombre': nombre}, request)
    return redirect('enjambre:gestionar_sillas')


@requiere_acceso
def guardar_persona(request, key):
    """Edita la persona (prompt) de UNA silla. Se lee fresca en cada mensaje, así que aplica en
    el próximo turno sin reiniciar."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control puede editar personas.")
    silla = get_object_or_404(Participante, key=key)
    if request.method == 'POST':
        silla.persona = request.POST.get('persona_a', '').strip()
        silla.save(update_fields=['persona'])
        log_event(request.user, 'ENJAMBRE_PERSONA_EDIT', 'enjambre', {'silla': key}, request)
    return redirect('enjambre:home')


@requiere_acceso
def worker_restart(request):
    """Encola un reinicio del worker (el worker del host se sale y systemd lo relanza con el
    código nuevo). Solo CONTROL. Idempotente: si ya hay un pedido encolado, no apila otro."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method == 'POST':
        if not WorkerRestart.objects.exists():
            WorkerRestart.objects.create(solicitante=_titulo_humano(request))
        log_event(request.user, 'ENJAMBRE_WORKER_RESTART', 'enjambre', {}, request)
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False}, status=405)


@requiere_acceso
def conexiones(request):
    """Pantalla Conexiones: qué CLIs tienen login detectado en el host. Solo EXISTENCIA
    por archivo (jamás se lee/loguea contenido). En compose lee lo que persistió el
    worker (conexiones.json, chequeado al arrancar); en dev chequea en vivo."""
    from .conexiones import CLIS, detectar, leer_estado, ruta_corta, ruta_creds
    estado = leer_estado()
    if estado:
        detectado = estado.get('detectado', {})
        chequeado_at = estado.get('chequeado_at', '')
    else:
        detectado, chequeado_at = detectar(), ''
    filas = [
        {'key': k, 'nombre': c['nombre'], 'login': c['login'],
         'ruta': ruta_corta(ruta_creds(k)), 'ok': detectado.get(k, False)}
        for k, c in CLIS.items()
    ]
    from . import vault
    prov_labels = {'anthropic': 'Anthropic (Claude)',
                   'openai': 'OpenAI-compatible (OpenAI / Groq / DeepSeek…)',
                   'openrouter': 'OpenRouter (incluye :free)',
                   'gemini': 'Gemini (key de Google AI Studio)',
                   'pollinations': 'Pollinations (anda sin key; el token gratis acelera 3×)'}
    configurados = vault.configured_providers()
    keys = [{'id': p, 'label': prov_labels.get(p, p), 'ok': p in configurados,
             'opcional': p in vault.PROVIDERS_OPCIONALES}
            for p in vault.TODOS]
    desbloqueada, existe = vault.is_unlocked(), vault.has_vault()
    vault_state = 'abierta' if desbloqueada else ('cerrada' if existe else 'nueva')
    from . import onboarding, toolbelt
    escalera = onboarding.escalones()
    return render(request, 'enjambre/conexiones.html', {
        'escalera': escalera,
        'escalera_listos': onboarding.listos(escalera),
        'filas': filas,
        'chequeado_at': chequeado_at,
        'puede_controlar': _puede_controlar(request),
        'keys': keys,
        'vault_desbloqueada': desbloqueada,
        'vault_existe': existe,
        'vault_state': vault_state,
        'vault_min_pass': vault.MIN_PASSPHRASE,
        'vault_nconf': len(configurados),
        'toolbelt_on': toolbelt.habilitado(),
        'toolbelt_forzado': toolbelt.forzado_por_env(),
    })


@requiere_acceso
def toolbelt_toggle(request):
    """Prende/apaga el toolbelt (que las sillas API operen la máquina real) desde la interfaz, sin
    editar el launcher. Persiste como flag en el data dir; se lee en vivo. Solo control. AJAX."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method != 'POST':
        return redirect('enjambre:conexiones')
    from . import toolbelt
    quiere = request.POST.get('on') in ('1', 'true', 'on')
    if toolbelt.forzado_por_env():
        estado = True  # forzado desde el entorno → no se apaga desde la UI
    else:
        estado = toolbelt.set_habilitado(quiere)
    log_event(request.user, 'ENJAMBRE_TOOLBELT', 'enjambre', {'on': estado}, request)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'on': estado, 'forzado': toolbelt.forzado_por_env()})
    return redirect('enjambre:conexiones')


@requiere_acceso
def vault_keys(request):
    """Gestión de la bóveda de API keys (cifrada por passphrase). POST con `accion`:
    set / remove / unlock / lock. Single-user, solo control. La passphrase NUNCA se loguea ni
    se devuelve. AJAX (JSON) con fallback a redirect a Conexiones."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    if request.method != 'POST':
        return redirect('enjambre:conexiones')
    from . import vault
    accion = request.POST.get('accion', '')
    passphrase = request.POST.get('passphrase', '')
    ok, error = True, ''
    if accion == 'set':
        ok, error = vault.set_key(passphrase, request.POST.get('provider', ''),
                                  request.POST.get('token', ''))
    elif accion == 'remove':
        ok, error = vault.remove_key(passphrase, request.POST.get('provider', ''))
    elif accion == 'unlock':
        ok = vault.unlock(passphrase)
        if ok:
            error = ''
        elif not vault.has_vault():
            error = 'todavía no hay bóveda — guardá tu primera key para crearla'
        else:
            error = 'passphrase incorrecta'
    elif accion == 'lock':
        vault.lock()
    else:
        ok, error = False, 'acción desconocida'
    # Se loguea SOLO la acción y el proveedor (nombre), jamás la passphrase ni el token.
    log_event(request.user, 'ENJAMBRE_VAULT', 'enjambre',
              {'accion': accion, 'provider': request.POST.get('provider', '')}, request)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': ok, 'error': error,
                             'desbloqueada': vault.is_unlocked(),
                             'configurados': vault.configured_providers()})
    return redirect('enjambre:conexiones')


@requiere_acceso
def modelos_disponibles(request):
    """Lista los modelos REALES del proveedor de un cliente, para el modal de la silla. Trae en
    vivo lo que se puede (OpenRouter público; OpenAI/Anthropic con la key del vault; opencode por
    CLI si está instalado) y flaggea los free. Si no se puede, cae a la lista curada de
    clientes.py con una nota. Solo control."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    from django.conf import settings as dj_settings
    from .clientes import CLIENTES, es_api
    from . import providers, vault, toolbelt
    ckey = request.GET.get('cliente', '')
    c = CLIENTES.get(ckey) or {}
    # tools=None (desconocido) en la lista curada y en opencode: no informan capacidad de tool-use.
    curada = [{'id': m, 'free': providers._es_free_id(m), 'tools': None}
              for m in c.get('modelos', []) if m]
    source, models, nota = 'curated', curada, ''

    if es_api(ckey):
        provider = c.get('api')
        key = vault.get_key(provider) if vault.is_unlocked() else ''
        base = getattr(dj_settings, 'SWARM_OPENAI_BASE_URL', '') if provider == 'openai' else ''
        src, live, note = providers.listar_modelos(provider, api_key=key, base_url=base)
        if src == 'live' and live:
            source, models, nota = 'live', live, ''
        else:
            nota = f'{note} — te muestro las sugeridas' if note else 'te muestro las sugeridas'
    elif ckey == 'opencode':
        import subprocess

        from .conexiones import resolver_bin
        try:
            oc = resolver_bin('opencode')
            if oc:
                r = subprocess.run([oc, 'models'], capture_output=True, text=True, timeout=15)
                ids = [ln.strip() for ln in (r.stdout or '').splitlines() if ln.strip()]
                if ids:
                    source = 'live'
                    models = [{'id': i, 'free': providers._es_free_id(i), 'tools': None} for i in ids]
                else:
                    nota = 'opencode no devolvió modelos — te muestro las sugeridas'
            else:
                nota = 'opencode no está instalado en esta máquina — te muestro las sugeridas'
        except Exception:  # noqa: BLE001
            nota = 'no se pudo consultar opencode — te muestro las sugeridas'

    seen, uniq = set(), []
    for m in models:
        mid = m.get('id', '')
        if mid and mid not in seen:
            seen.add(mid)
            uniq.append(m)
    # Con el toolbelt encendido, los que soportan tools van primero (son los que operan la máquina);
    # si no, ordenamos por free primero. En ambos casos, alfabético como desempate.
    belt_on = toolbelt.habilitado()
    if belt_on:
        uniq.sort(key=lambda m: (m.get('tools') is not True, not m.get('free'), m['id'].lower()))
    else:
        uniq.sort(key=lambda m: (not m.get('free'), m['id'].lower()))
    return JsonResponse({'source': source, 'nota': nota, 'models': uniq,
                         'total': len(uniq), 'free': sum(1 for m in uniq if m.get('free')),
                         'tools': sum(1 for m in uniq if m.get('tools') is True),
                         'belt_on': belt_on})


@requiere_acceso
def bitacora(request, pk):
    """Bitácora del toolbelt de una mesa: todas las Acciones (lecturas auto + mutaciones), con
    aprobar/rechazar para las pendientes. La red de seguridad del paradigma «fuera del cascarón»."""
    sesion = get_object_or_404(Sesion, pk=pk)
    from . import toolbelt
    acciones = list(sesion.acciones.select_related('participante'))
    pendientes = sum(1 for a in acciones if a.estado == 'pendiente')
    return render(request, 'enjambre/bitacora.html', {
        'sesion': sesion,
        'acciones': acciones,
        'pendientes': pendientes,
        'toolbelt_on': toolbelt.habilitado(),
        'puede_controlar': _puede_controlar(request),
    })


@requiere_acceso
def accion_resolver(request, accion_id):
    """Aprueba (ejecuta en el host) o rechaza una Acción pendiente del toolbelt. Solo control.
    POST accion=aprobar|rechazar. AJAX (JSON) con fallback a redirect a la bitácora."""
    if not _puede_controlar(request):
        return HttpResponseForbidden("Solo control.")
    from .models import Accion
    from . import toolbelt
    acc = get_object_or_404(Accion, pk=accion_id)
    if request.method == 'POST' and acc.estado == 'pendiente':
        quien = getattr(request.user, 'username', '') or 'el técnico'
        if request.POST.get('accion') == 'aprobar':
            toolbelt.ejecutar_pendiente(acc, quien)
            log_event(request.user, 'ENJAMBRE_ACCION_APROBAR', 'enjambre', {'accion': acc.pk}, request)
        elif request.POST.get('accion') == 'rechazar':
            toolbelt.rechazar_pendiente(acc, quien, request.POST.get('motivo', ''))
            log_event(request.user, 'ENJAMBRE_ACCION_RECHAZAR', 'enjambre', {'accion': acc.pk}, request)
        acc.refresh_from_db()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'estado': acc.estado, 'salida': acc.salida,
                             'aprobada_por': acc.aprobada_por})
    return redirect('enjambre:bitacora', pk=acc.sesion_id)


@requiere_acceso
def informe(request, pk):
    """Exporta la bitácora de la mesa como informe de soporte (.md descargable). Fiel: refleja
    exactamente lo que quedó registrado (comando, motivo, estado, salida, quién, cuándo)."""
    sesion = get_object_or_404(Sesion, pk=pk)
    acciones = list(sesion.acciones.select_related('participante'))
    lineas = [f"# Informe de soporte — {sesion.nombre} (mesa #{sesion.pk})",
              f"_Generado: {timezone.now():%Y-%m-%d %H:%M:%S}_  ·  {len(acciones)} acción(es)", ""]
    ESTADO_TXT = {'ejecutada': '✅ ejecutada', 'pendiente': '⏳ pendiente',
                  'rechazada': '🚫 rechazada', 'error': '⚠️ error'}
    for a in acciones:
        tipo = '🔧 MUTACIÓN' if a.es_mutacion else '🔍 lectura'
        lineas.append(f"## {a.herramienta} · {tipo} · {ESTADO_TXT.get(a.estado, a.estado)}")
        lineas.append(f"- Silla: {a.emisor or '—'}  ·  {a.creado_at:%Y-%m-%d %H:%M:%S}")
        if a.aprobada_por:
            lineas.append(f"- Resuelta por: {a.aprobada_por}"
                          + (f" ({a.resuelto_at:%H:%M:%S})" if a.resuelto_at else ''))
        lineas.append(f"\n```\n$ {a.comando}\n```")
        if a.motivo:
            lineas.append(f"**Motivo:** {a.motivo}")
        if a.salida:
            lineas.append(f"\nSalida:\n```\n{a.salida[:4000]}\n```")
        lineas.append("")
    contenido = '\n'.join(lineas)
    resp = HttpResponse(contenido, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="informe-mesa-{sesion.pk}.md"'
    log_event(request.user, 'ENJAMBRE_INFORME', 'enjambre', {'sesion': sesion.pk}, request)
    return resp


@requiere_acceso
def ayuda(request):
    """Guía de uso de la mesa: conceptos, comandos y ejemplos. Estática, sin estado."""
    return render(request, 'enjambre/ayuda.html')
