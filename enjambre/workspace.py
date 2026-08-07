"""
enjambre/workspace.py — workspaces aislados.

Principio no negociable: el agente NUNCA trabaja sobre el árbol desplegado. Cada Tarea
corre en un **git worktree** propio (branch `enjambre/...`), se comitea el resultado y
el worktree se desmonta. La branch queda para que el humano la revise/mergee (NO se
hace push ni PR).

Las funciones de git son puras (operan sobre rutas/params, sin ORM) para poder testearlas
en aislamiento; `ejecutar_tarea()` las orquesta sobre los modelos.

Invariante de las DOS rutas de ejecución (worktree y carpeta persistente de la mesa): **ninguna
deja una Tarea a medio camino ni deja escapar la excepción**. Pase lo que pase con git, la Tarea
termina en un estado final (HECHA / SIN_CAMBIOS / ERROR con el motivo en `salida`) y el worker
sigue vivo — no envuelve estas llamadas, así que una excepción que suba se lleva puesto el tick
entero. Las dos rutas se tocan JUNTAS: divergir es como se cuela un estado zombi en una sola.
"""
import os
import subprocess
from pathlib import Path

from django.conf import settings

from .engine import ejecutar_cli
from .models import Mensaje, Tarea, Workspace

# Identidad para los commits del enjambre (no depende del git config del repo).
GIT_AUTOR = ['-c', 'user.name=Enjambre', '-c', 'user.email=enjambre@local']

# exFAT/FAT32 (o sea: TODO pendrive) no registra dueño de archivo, así que git considera el repo
# de dueño ajeno y se planta con «detected dubious ownership» antes de hacer nada. Es el caso
# normal de Swarm, no un borde: las mesas viven en `data/` del pendrive (Oficina, 2026-08-07).
#
# git manda hacer `git config --global --add safe.directory <ruta>`, y eso es justo lo que NO
# podemos hacer: escribe en el ~/.gitconfig de la PC del cliente y deja rastro en la máquina que
# el modo SIN RASTRO promete no tocar. La marca va por invocación.
#
# Va `*` y no la ruta exacta por dos motivos MEDIDOS (git 2.43): (1) el match es textual y
# quisquilloso —una barra final de más y ya no aplica—, y en Windows nosotros pasamos
# `D:\...\mesa-1` mientras git razona sobre `D:/.../mesa-1`; (2) NO existe comodín de subárbol:
# `<carpeta-de-mesas>/*` no habilita nada, probado. Alcance real: solo los procesos que lanza
# Swarm, y solo apaga un chequeo de DUEÑO sobre carpetas que Swarm mismo creó — no da acceso
# que la silla no tuviera (el permiso sigue siendo el switch del toolbelt).
GIT_SAFE = ['-c', 'safe.directory=*']


def env_git_safe(env):
    """Mete la misma excepción de `safe.directory` en un environment, para que la hereden los
    procesos hijos. `GIT_SAFE` cubre el git que corremos NOSOTROS; esto cubre el que corre la
    silla CLI por su cuenta dentro de la carpeta de la mesa (`git status`, `git diff`…), que si
    no se planta con el mismo error y la silla lo reporta como si el trabajo hubiera fallado.

    Respeta un `GIT_CONFIG_COUNT` que ya venga del entorno en vez de pisarlo: se agrega al final
    y se incrementa el contador. Muta y devuelve el mismo dict."""
    try:
        n = int(env.get('GIT_CONFIG_COUNT', '0') or '0')
    except ValueError:
        n = 0
    env[f'GIT_CONFIG_KEY_{n}'] = 'safe.directory'
    env[f'GIT_CONFIG_VALUE_{n}'] = '*'
    env['GIT_CONFIG_COUNT'] = str(n + 1)
    return env

# .gitignore de toda mesa nueva: temporales, dependencias y credenciales. Deliberadamente corto —
# lo que la mesa produce (informes, código, notas) TIENE que quedar versionado.
GITIGNORE_MESA = """# Temporales y basura de herramientas
*.tmp
*.swp
*.pyc
__pycache__/
.DS_Store
# Dependencias (pesan y se reinstalan)
node_modules/
.venv/
venv/
# Credenciales: nunca en el historial de la mesa
.env
*.key
*.pem
"""


GIT_TIMEOUT = 120  # un `add -A` sobre una carpeta enorme no puede colgar el worker para siempre


def _git(repo, *args, check=True):
    """Corre git -C <repo> <args> y devuelve stdout (strip)."""
    try:
        r = subprocess.run(['git', *GIT_SAFE, '-C', str(repo), *args],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           # git habla UTF-8 en todas las plataformas; `text=True` a secas lo
                           # decodificaría con la del SO (cp1252 en Windows) y rompería los
                           # acentos de nombres de archivo y mensajes de commit.
                           encoding='utf-8', errors='replace')
    except FileNotFoundError:
        # git NO está instalado. Pasa en cualquier Windows recién instalada, que es justo el
        # caso de uso del pendrive. Sin esto subía el `FileNotFoundError` crudo y la mesa
        # mostraba «[WinError 2] El sistema no puede encontrar el archivo especificado», que
        # no nombra a git ni dice qué hacer. `check=False` NO exime: sin git no hay repo, así
        # que todos los caminos tienen que cortar acá con el mismo mensaje.
        from .conexiones import como_instalar_git
        raise RuntimeError(
            f"git no está instalado en esta máquina y /armar lo necesita para versionar la "
            f"carpeta de la mesa. Instalalo con:  {como_instalar_git()}   "
            f"(después reiniciá Swarm para que tome el PATH nuevo). "
            f"Charlar con las sillas funciona igual sin git.")
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"git {' '.join(args)} no terminó en {GIT_TIMEOUT}s")
        return ''
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
        # `comitear()` hace `git add -A`: sin esto, la basura que genere una silla (venv,
        # node_modules, un .env) queda versionada en la mesa y viaja en el zip de descarga.
        gitignore = dest / '.gitignore'
        if not gitignore.exists():
            gitignore.write_text(GITIGNORE_MESA, encoding='utf-8')
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
    except Exception as e:  # noqa: BLE001 — ver el bloque gemelo de _ejecutar_persistente
        tarea.estado = Tarea.Estado.ERROR
        tarea.salida = f"{tarea.salida}\n\n(❌ no se pudo commitear: {e})"
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

    # try/except/finally, igual que la ruta no-persistente. Si `comitear()` explota (git colgado,
    # permisos, repo roto) pasan dos cosas malas sin esto: la Tarea queda clavada en EN_CURSO para
    # siempre —nadie la reintenta ni la marca— y la excepción sube al worker, que no envuelve esta
    # llamada: se lleva puesto el tick entero y las tareas que venían atrás no corren. Marcarla
    # ERROR con el motivo en la salida deja el fallo VISIBLE en la mesa y el worker sigue vivo.
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
    except Exception as e:  # noqa: BLE001
        tarea.estado = Tarea.Estado.ERROR
        tarea.salida = f"{tarea.salida}\n\n(❌ no se pudo commitear: {e})"
    finally:
        # Persistente: NO se desmonta la carpeta (se acumula para la próxima tarea).
        ws.save()
        tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
    return tarea
