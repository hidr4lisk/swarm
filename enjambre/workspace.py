"""
enjambre/workspace.py — workspaces aislados.

Principio no negociable: el agente NUNCA trabaja sobre el árbol desplegado. Cada Tarea
corre en un **git worktree** propio (branch `enjambre/...`), se comitea el resultado y
el worktree se desmonta. La branch queda para que el humano la revise/mergee (NO se
hace push ni PR).

Las funciones de git son puras (operan sobre rutas/params, sin ORM) para poder testearlas
en aislamiento; `ejecutar_tarea()` las orquesta sobre los modelos.
"""
import os
import subprocess
from pathlib import Path

from django.conf import settings

from .engine import ejecutar_cli
from .models import Mensaje, Tarea, Workspace

# Identidad para los commits del enjambre (no depende del git config del repo).
GIT_AUTOR = ['-c', 'user.name=Enjambre', '-c', 'user.email=enjambre@local']


def _git(repo, *args, check=True):
    """Corre git -C <repo> <args> y devuelve stdout (strip)."""
    r = subprocess.run(['git', '-C', str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falló: {r.stderr.strip()}")
    return r.stdout.strip()


def workspaces_dir():
    d = getattr(settings, 'ENJAMBRE_WORKSPACES_DIR', '') or str(Path.home() / '.enjambre' / 'workspaces')
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def mesas_dir():
    """Base de las carpetas de trabajo PERSISTENTES por mesa. Tiene que ser visible por el
    worker Y montable en el runner (en el compose: misma ruta host/contenedor, ver DooD)."""
    d = (getattr(settings, 'ENJAMBRE_MESAS_DIR', '')
         or str(Path.home() / '.enjambre' / 'mesas'))
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def chown_host(path):
    """Deja `path` (y su contenido) con dueño del worker del host (uid/gid de ENJAMBRE_HOST_UID/GID,
    default 1000). Lo usa el CONTENEDOR WEB (root): las carpetas/archivos de mesa que crea quedarían
    root:root y el worker (corre como el usuario del host) no podría hacerles `git init` →
    'Permission denied'. Best-effort: si no corre como root (p.ej. el propio worker), no hace nada."""
    uid = int(getattr(settings, 'ENJAMBRE_HOST_UID', 1000))
    gid = int(getattr(settings, 'ENJAMBRE_HOST_GID', 1000))
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return  # solo root puede chownear a otro uid; el worker ya crea con el dueño correcto
    p = Path(path)
    try:
        os.chown(p, uid, gid)
        for root, dirs, files in os.walk(p):
            for name in dirs + files:
                try:
                    os.chown(os.path.join(root, name), uid, gid)
                except OSError:
                    pass
    except OSError:
        pass


def mesa_workspace(sesion):
    """Carpeta persistente de la mesa (git repo, scratch). La crea+inicializa una sola vez;
    las tareas se acumulan ahí. Devuelve el Path (host). Idempotente.

    SIDE EFFECT: además de crear la carpeta, persiste la ruta en `sesion.workspace_dir` (un
    `save(update_fields=['workspace_dir'])`) si cambió — para que el resto del sistema sepa dónde
    quedó el repo sin recalcularlo. No es una función pura: crear la carpeta y fijar la ruta van
    juntas a propósito (la ruta depende del `sesion.id`)."""
    dest = Path(mesas_dir()) / f"mesa-{sesion.id}"
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / '.git').exists():
        _git(dest, 'init')
        # NOTAS.md = memoria compartida de la mesa: las sillas la leen/actualizan entre turnos.
        notas = dest / 'NOTAS.md'
        if not notas.exists():
            notas.write_text(
                f"# NOTAS — {sesion.nombre}\n\n"
                "Memoria compartida de la mesa. Las sillas anotan acá decisiones, TODOs y contexto\n"
                "que conviene recordar entre turnos.\n",
                encoding='utf-8',
            )
        _git(dest, 'add', '-A')
        _git(dest, *GIT_AUTOR, 'commit', '-m', 'Enjambre · mesa init')
    if sesion.workspace_dir != str(dest):
        sesion.workspace_dir = str(dest)
        sesion.save(update_fields=['workspace_dir'])
    return dest


# ── Funciones puras de git ──────────────────────────────────────────────────
def crear_worktree(repo_path, base_ref, branch, dest_path):
    """Crea un worktree aislado en `dest_path` con una branch nueva desde base_ref.
    Devuelve el SHA base."""
    base_sha = _git(repo_path, 'rev-parse', base_ref)
    _git(repo_path, 'worktree', 'add', '-b', branch, str(dest_path), base_sha)
    return base_sha


def hay_cambios(worktree_path):
    return bool(_git(worktree_path, 'status', '--porcelain'))


def comitear(worktree_path, mensaje):
    """Stagea TODO y comitea. Devuelve el SHA, o None si no hubo cambios."""
    _git(worktree_path, 'add', '-A')
    if not hay_cambios(worktree_path):
        return None
    _git(worktree_path, *GIT_AUTOR, 'commit', '-m', mensaje)
    return _git(worktree_path, 'rev-parse', 'HEAD')


def diff_stat(worktree_path, base_sha):
    return _git(worktree_path, 'diff', '--stat', f'{base_sha}..HEAD', check=False)


def remover_worktree(repo_path, dest_path):
    """Desmonta el worktree (la branch queda). Tolera que ya no exista."""
    _git(repo_path, 'worktree', 'remove', '--force', str(dest_path), check=False)


# ── Orquestación sobre los modelos ────────────────────────────────────────────
def ejecutar_tarea(tarea, timeout=None):
    """Ciclo completo de una Tarea: worktree → fabricar → comitear → desmontar.

    Deja la branch `enjambre/tarea-<id>` para revisión humana. Idempotente por tarea
    (una sola Workspace, OneToOne).
    """
    timeout = timeout or (tarea.sesion.timeout if tarea.sesion else 180)

    tarea.estado = Tarea.Estado.EN_CURSO
    tarea.save(update_fields=['estado', 'actualizado_at'])

    if tarea.persistente:
        return _ejecutar_persistente(tarea, timeout)

    # Guard: modo repo SIN repo_path apuntaría al cwd (¡el repo desplegado!). Nunca.
    if not tarea.repo_path:
        tarea.estado = Tarea.Estado.ERROR
        tarea.salida = "(❌ modo repo sin repo_path — abortado para no tocar el repo desplegado)"
        tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
        return tarea

    branch = f"enjambre/tarea-{tarea.pk}"
    dest = str(Path(workspaces_dir()) / f"tarea-{tarea.pk}")
    base_sha = crear_worktree(tarea.repo_path, tarea.base_ref, branch, dest)
    ws = Workspace.objects.create(
        tarea=tarea, path=dest, branch=branch, base_commit=base_sha,
        estado=Workspace.Estado.CREADO,
    )

    # Fabricar: el trabajador usa su comando de trabajo (modo agéntico), cwd = worktree.
    salida, ruido = ejecutar_cli(
        tarea.participante, tarea.ordenes, timeout,
        workdir=dest, comando=tarea.participante.cmd_trabajo(),
    )
    tarea.salida = salida
    if tarea.sesion:
        Mensaje.objects.create(
            sesion=tarea.sesion, emisor=tarea.participante.nombre,
            participante=tarea.participante, texto=salida, es_ruido=ruido,
        )

    try:
        if ruido:
            tarea.estado = Tarea.Estado.ERROR
        else:
            sha = comitear(dest, f"Enjambre · tarea {tarea.pk}: {tarea.titulo}")
            if sha:
                ws.commit_sha = sha
                ws.estado = Workspace.Estado.COMITEADO
                tarea.estado = Tarea.Estado.HECHA
            else:
                tarea.estado = Tarea.Estado.SIN_CAMBIOS
    finally:
        remover_worktree(tarea.repo_path, dest)
        if ws.estado != Workspace.Estado.COMITEADO:
            ws.estado = Workspace.Estado.LIMPIADO
        ws.save()
        tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])

    return tarea


def _ejecutar_persistente(tarea, timeout):
    """Modo scratch: fabrica IN-PLACE en la carpeta persistente de la mesa (NO worktree
    efímero), commitea ahí y NO desmonta. Las tareas se acumulan (la siguiente ve lo anterior).
    El árbol desplegado nunca se toca: esta carpeta es un sandbox git aparte (ver mesas_dir)."""
    dest = str(mesa_workspace(tarea.sesion))
    base_sha = _git(dest, 'rev-parse', 'HEAD', check=False)
    branch = _git(dest, 'rev-parse', '--abbrev-ref', 'HEAD', check=False) or 'master'
    ws = Workspace.objects.create(
        tarea=tarea, path=dest, branch=branch, base_commit=base_sha, estado=Workspace.Estado.CREADO,
    )

    salida, ruido = ejecutar_cli(
        tarea.participante, tarea.ordenes, timeout,
        workdir=dest, comando=tarea.participante.cmd_trabajo(),
    )
    tarea.salida = salida
    if tarea.sesion:
        Mensaje.objects.create(
            sesion=tarea.sesion, emisor=tarea.participante.nombre,
            participante=tarea.participante, texto=salida, es_ruido=ruido,
        )

    if ruido:
        tarea.estado = Tarea.Estado.ERROR
    else:
        sha = comitear(dest, f"Enjambre · tarea {tarea.pk}: {tarea.titulo}")
        if sha:
            ws.commit_sha = sha
            ws.estado = Workspace.Estado.COMITEADO
            tarea.estado = Tarea.Estado.HECHA
        else:
            tarea.estado = Tarea.Estado.SIN_CAMBIOS
    # Persistente: NO se desmonta la carpeta (se acumula para la próxima tarea).
    ws.save()
    tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
    return tarea
