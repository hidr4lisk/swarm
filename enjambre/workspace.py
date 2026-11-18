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
import hashlib
import os
import re
import stat
import subprocess
import sys
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


# El git del HOST corre sobre carpetas que escribe una silla (auditoría Sakura 28/10/2026, S-01).
# Lo que se puede apagar por línea de comandos se apaga en TODA llamada: hooks, fsmonitor y la
# caché de untracked (que también lee de `.git`). Los `filter.<x>.clean`/`smudge` no se pueden
# apagar así —el nombre lo inventa quien escribe la config y `-c` no tiene comodín—: a esos los
# frena `asegurar_git()` antes de commitear. Un repo ANIDADO también lo bloquea `asegurar_git()`.
# `os.devnull` y no '/dev/null': en Windows es `nul` y git lo entiende igual (no hay hooks ahí).
GIT_SEGURO = ['-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false',
              '-c', 'core.untrackedCache=false', '-c', 'commit.gpgSign=false',
              '-c', 'tag.gpgSign=false']


def _git_env():
    """Entorno del git del host: sin ningún `GIT_*` heredado (GIT_DIR, GIT_CONFIG_*, GIT_EXEC_PATH…)
    y sin config de sistema ni global: git lee solo la del repo. La identidad de los commits va por
    `GIT_AUTOR` y la excepción de dueño por `GIT_SAFE`, así que la global no hace falta."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    return env


def _git(repo, *args, check=True, git_dir=None):
    """Corre git -C <repo> <args> y devuelve stdout (strip). Con `git_dir`, usa ese directorio de
    git explícito en vez de descubrirlo leyendo el `.git` de la carpeta (worktrees: ver
    `_ejecutar_repo`)."""
    cmd = ['git', *GIT_SEGURO, *GIT_SAFE, '-C', str(repo)]
    if git_dir:
        cmd += ['--git-dir', str(git_dir), '--work-tree', str(repo)]
    try:
        r = subprocess.run([*cmd, *args],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_git_env(),
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


# ── Lectura/escritura de lo que escribe la silla, sin seguir symlinks (S-02) ─────────────────
# La carpeta de la mesa la escribe la silla y la lee/sirve el host o la web: un `NOTAS.md` cambiado
# por un symlink a `~/.ssh/…` o al `.env` terminaría en el prompt de un modelo o en una descarga.
# POSIX: se recorre componente por componente con `O_NOFOLLOW` relativo al fd del anterior (ni un
# cambio entre mirar y abrir se sigue). Windows no tiene `O_NOFOLLOW` ni `dir_fd`: ahí se mira cada
# componente con `lstat` (symlink o junction = rechazado) y se abre. Queda una ventana mínima entre
# el `lstat` y el `open` que en POSIX no existe; crear symlinks en Windows exige privilegio.
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_O_BINARY = getattr(os, 'O_BINARY', 0)   # solo Windows: sin esto el fd traduce \r\n
_O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_CON_DIRFD = bool(_O_NOFOLLOW and _O_DIRECTORY and os.open in os.supports_dir_fd)


def _es_enlace(path):
    """True si `path` es symlink o junction (sin seguirlo). Inexistente → False."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(os.path.isjunction(path))


def _partes_validas(rel):
    partes = [p for p in str(rel).replace('\\', '/').split('/') if p]
    if not partes or any(p in ('.', '..') or p.lower() == '.git' for p in partes):
        return None
    if sys.platform == 'win32' and any(':' in p for p in partes):
        return None
    return partes


def abrir_artefacto(base, rel):
    """fd de solo lectura de `base/rel`, o None. Ningún symlink del camino se sigue. Solo devuelve
    archivos regulares (`O_NONBLOCK` en POSIX: un FIFO no cuelga el `open`). `.`, `..` y `.git` no
    son componentes válidos. El llamador cierra el fd."""
    partes = _partes_validas(rel)
    if partes is None:
        return None
    if not _CON_DIRFD:
        path = Path(base)
        try:
            for parte in partes:
                path = path / parte
                if _es_enlace(path):
                    return None
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            fd = os.open(path, os.O_RDONLY | _O_BINARY | _O_NONBLOCK)
        except OSError:
            return None
    else:
        try:
            dfd = os.open(base, os.O_RDONLY | _O_DIRECTORY)
        except OSError:
            return None
        try:
            for parte in partes[:-1]:
                sub = os.open(parte, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dfd)
                os.close(dfd)
                dfd = sub
            fd = os.open(partes[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dfd)
        except OSError:
            return None  # inexistente, o symlink en el camino (ELOOP/ENOTDIR)
        finally:
            os.close(dfd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None  # FIFO, dispositivo, socket
    if _O_NONBLOCK:
        os.set_blocking(fd, True)
    return fd


def abrir_escritura(base, nombre):
    """fd de escritura de `base/nombre` (un solo componente), sin seguir symlinks y solo sobre un
    archivo regular. NO trunca: el llamador lo hace (`fdopen(fd,'wb').truncate()`) cuando ya sabe que
    puede escribir. Levanta OSError si `nombre` es un enlace, un FIFO o algo que no es regular."""
    if (not nombre or os.sep in nombre or '/' in nombre or nombre in ('.', '..')
            or (sys.platform == 'win32' and ':' in nombre)):
        raise OSError(f'nombre inválido: {nombre!r}')
    if not _CON_DIRFD:
        path = Path(base) / nombre
        if _es_enlace(path):
            raise OSError('es un enlace')
        if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError('no es un archivo regular')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | _O_BINARY, 0o644)
    else:
        dfd = os.open(base, os.O_RDONLY | _O_DIRECTORY)
        try:
            fd = os.open(nombre, os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK, 0o644, dir_fd=dfd)
        finally:
            os.close(dfd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError('no es un archivo regular')
    if _O_NONBLOCK:
        os.set_blocking(fd, True)
    return fd


def leer_fd(fd, limite):
    """Como mucho `limite` bytes de `fd`, sin leer de más."""
    partes, falta = [], limite
    while falta > 0:
        trozo = os.read(fd, min(falta, 1 << 20))
        if not trozo:
            break
        partes.append(trozo)
        falta -= len(trozo)
    return b''.join(partes)


def leer_artefacto(base, nombre, limite):
    """Lee como mucho `limite` bytes de `base/nombre` SIN seguir symlinks. Devuelve str ('' si no
    se puede). `nombre` es un solo componente; para rutas anidadas, `abrir_artefacto`."""
    if not nombre or os.sep in nombre or '/' in nombre or nombre in ('.', '..'):
        raise ValueError(f'nombre de artefacto inválido: {nombre!r}')
    fd = abrir_artefacto(base, nombre)
    if fd is None:
        return ''
    try:
        return leer_fd(fd, limite).decode('utf-8', errors='replace')
    finally:
        os.close(fd)


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


# ── Testigo de integridad de `.git` (S-01) ────────────────────────────────────────
# La silla corre en el host con la carpeta de la mesa escribible, `.git` incluido (Swarm no tiene
# runner que lo monte solo lectura). Antes del turno se toma la huella; antes de commitear se
# verifica. Si cambió, o trae algo que ejecuta, no se toca nada y la falla queda a la vista en la
# mesa. La marca la reconoce quien busque `GIT_ALTERADO_MARCA`.
GIT_ALTERADO_MARCA = '🛑 .git alterado'

# Control ABSOLUTO del `.git` de una mesa: lista de lo PERMITIDO, no de lo prohibido. `git init`
# deja estas claves y nada más (symlinks/ignorecase/precomposeunicode según plataforma: Windows y
# macOS las escriben), así que cualquier otra es una mesa tocada.
_CONFIG_PERMITIDA = re.compile(
    r'^core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode'
    r'|symlinks)$', re.IGNORECASE)
# Archivos que, si aparecen dentro de `.git` de una mesa, hacen que git lea config o metadata de
# OTRO lado (y ese otro lado puede ser una carpeta que la silla escribe).
_GIT_INDIRECCIONES = ('commondir', 'config.worktree', 'gitdir')
# Worktree de una Tarea: la config es la del repo real, así que no hay lista blanca posible. Lo que
# se niega es lo que, durante add/status/commit/reset, corre un programa o trae config de otro lado.
_CONFIG_EJECUTA = re.compile(
    r'^(filter\.|include\.|includeif\.|diff\.external$|diff\..+\.(command|textconv)$'
    r'|merge\..+\.driver$)', re.IGNORECASE)


class GitAlterado(RuntimeError):
    """El `.git` de la carpeta no es el que dejó Swarm: no se commitea."""


def _huella_archivo(path):
    """Bytes que identifican un archivo SIN seguirlo: tipo + contenido acotado si es regular,
    destino si es symlink. POSIX abre con `O_NOFOLLOW|O_NONBLOCK` y mira el tipo sobre el fd (ni un
    FIFO cuelga ni un cambio entre `lstat` y `open` hace leer otra cosa). Sin `O_NOFOLLOW`
    (Windows) se mira con `lstat` antes de abrir."""
    try:
        st = os.lstat(path)
    except OSError:
        return b'-'
    if stat.S_ISLNK(st.st_mode) or os.path.isjunction(path):
        try:
            return b'link:' + os.fsencode(os.readlink(path))
        except OSError:
            return b'link:?'
    if stat.S_ISDIR(st.st_mode):
        return b'dir'
    if not stat.S_ISREG(st.st_mode):
        return f'tipo:{stat.S_IFMT(st.st_mode)}'.encode()
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_BINARY)
    except OSError:
        return b'-'   # cambió entre el lstat y el open (en POSIX: pasó a ser un enlace)
    try:
        modo = os.fstat(fd).st_mode
        if stat.S_ISDIR(modo):
            return b'dir'
        if not stat.S_ISREG(modo):
            return f'tipo:{stat.S_IFMT(modo)}'.encode()
        return b'reg:' + hashlib.sha256(leer_fd(fd, 1_000_000)).digest()
    finally:
        os.close(fd)


def huella_git(carpeta):
    """Hash de lo de `.git` que decide qué ejecuta git: el propio `.git` (directorio, archivo
    `gitdir:` o symlink), `config`, `info/attributes` y cada archivo de `hooks/`."""
    g = Path(carpeta) / '.git'
    h = hashlib.sha256(b'.git:' + _huella_archivo(g))
    if not _es_enlace(g) and g.is_dir():
        for rel in ('config', 'info/attributes', 'hooks', *_GIT_INDIRECCIONES):
            h.update(f'\0{rel}:'.encode() + _huella_archivo(g / rel))
        hooks = g / 'hooks'
        if not _es_enlace(hooks) and hooks.is_dir():
            for p in sorted(hooks.iterdir()):
                h.update(f'\0hooks/{p.name}:'.encode() + _huella_archivo(p))
    return h.hexdigest()


def _es_nombre_git(nombre):
    if sys.platform in ('win32', 'darwin'):
        return nombre.lower() == '.git'
    return nombre == '.git'


def _repos_anidados(carpeta):
    """Cada `.git` debajo de la raíz (directorio, archivo `gitdir:` o symlink), sin seguir symlinks.
    Un repo anidado es un gitlink: `status`/`add` del host consultan su estado con un git hijo que
    lee SU config —escribible por la silla— y ejecuta sus filtros. No hay flag que lo apague."""
    raiz = Path(carpeta)
    hallados = []

    def no_se_pudo(e):
        # Fallar cerrado: un directorio que no se deja listar puede esconder un `.git` que git sí
        # alcanza. Recorrido incompleto ≠ árbol limpio.
        donde = Path(e.filename or raiz)
        rel = donde.relative_to(raiz) if donde.is_relative_to(raiz) else donde
        hallados.append(f'no se pudo recorrer {rel} ({e.strerror or e})')

    for actual, dirs, archivos in os.walk(raiz, onerror=no_se_pudo):
        if Path(actual) == raiz:
            dirs[:] = [d for d in dirs if not _es_nombre_git(d)]
            continue
        if any(_es_nombre_git(x) for x in dirs) or any(_es_nombre_git(x) for x in archivos):
            hallados.append(f'repo anidado {Path(actual).relative_to(raiz)}/.git')
            dirs[:] = [d for d in dirs if not _es_nombre_git(d)]
    return sorted(hallados)


def peligros_worktree(carpeta, git_dir):
    """Lo que no puede tener el worktree de una Tarea: claves que ejecutan en la config efectiva
    del repo real y repos anidados en el árbol. Lista de motivos; vacía = limpio."""
    motivos = []
    try:
        claves = _git(carpeta, 'config', '--name-only', '--list', git_dir=git_dir)
    except RuntimeError:
        motivos.append('config ilegible')
    else:
        motivos = [f'config {c}' for c in claves.splitlines() if _CONFIG_EJECUTA.match(c)]
    return motivos + _repos_anidados(carpeta)


def peligros_git(carpeta):
    """Lo que hoy tiene el `.git` de una MESA y no debería. Lista de motivos; vacía = limpio.

    No depende del testigo: una mesa que ya quedó tocada sigue bloqueada turno tras turno hasta que
    alguien limpie su `.git`. En una mesa `.git` es siempre un directorio: un archivo `gitdir:` o un
    symlink es una mesa tocada. Los worktrees de `_ejecutar_repo` usan `peligros_worktree`."""
    g = Path(carpeta) / '.git'
    if _es_enlace(g) or not g.is_dir():
        return ['.git no es un directorio']
    motivos = [f'.git/{n}' for n in _GIT_INDIRECCIONES if os.path.lexists(g / n)]
    hooks = g / 'hooks'
    if _es_enlace(hooks):
        motivos.append('.git/hooks es un enlace')
    elif hooks.is_dir():
        motivos += [f'hook {p.name}' for p in sorted(hooks.iterdir()) if not p.name.endswith('.sample')]
    config = g / 'config'
    if _es_enlace(config) or not config.is_file():
        motivos.append('.git/config no es un archivo regular')
    else:
        try:
            claves = _git(carpeta, 'config', '--file', str(config), '--name-only', '--list')
        except RuntimeError:
            motivos.append('.git/config ilegible')
        else:
            motivos += [f'config {c}' for c in claves.splitlines() if not _CONFIG_PERMITIDA.match(c)]
    return motivos + _repos_anidados(carpeta)


def asegurar_git(carpeta, huella=None, git_dir=None):
    """Levanta `GitAlterado` si el `.git` cambió desde `huella` (tomada antes del turno) o trae algo
    que ejecuta. Se llama ANTES de cualquier git que lea el árbol (`add`, `status`, `reset`).

    Con `git_dir` (worktree de una Tarea) la metadata vive en el repo real y su config es legítima:
    en vez de la lista blanca de la mesa se niegan las claves que ejecutan (`peligros_worktree`).

    Lo que esto NO cubre: una carrera entre este control y el `git` que sigue. Sin un runner que
    monte `.git` solo lectura (Swarm no lo tiene) la silla ya corre en el host con los permisos de
    quien la lanzó: esto frena el escape accidental o inducido por un prompt, no es un sandbox."""
    motivos = peligros_worktree(carpeta, git_dir) if git_dir else peligros_git(carpeta)
    if huella is not None and huella_git(carpeta) != huella:
        motivos.insert(0, 'cambió durante el turno')
    if motivos:
        raise GitAlterado(f"{GIT_ALTERADO_MARCA} en {carpeta}: {', '.join(motivos)} — no se commitea")


def hay_cambios(worktree_path, git_dir=None):
    return bool(_git(worktree_path, 'status', '--porcelain', git_dir=git_dir))


def comitear(worktree_path, mensaje, huella=None, git_dir=None):
    """Stagea TODO y comitea. Devuelve el SHA, o None si no hubo cambios.
    `huella`: la de `huella_git()` antes del turno; si `.git` cambió, `GitAlterado` y no se toca nada."""
    asegurar_git(worktree_path, huella, git_dir=git_dir)
    # Mesa: el `.git` que se acaba de controlar, explícito; git no vuelve a descubrir nada.
    git_dir = git_dir or Path(worktree_path) / '.git'
    _git(worktree_path, 'add', '-A', git_dir=git_dir)
    if not hay_cambios(worktree_path, git_dir=git_dir):
        return None
    _git(worktree_path, *GIT_AUTOR, 'commit', '-m', mensaje, git_dir=git_dir)
    return _git(worktree_path, 'rev-parse', 'HEAD', git_dir=git_dir)


def diff_stat(worktree_path, base_sha):
    return _git(worktree_path, 'diff', '--no-ext-diff', '--no-textconv', '--stat',
                f'{base_sha}..HEAD', check=False)


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

    # Lo que explota ANTES del try/finally de cada ruta (crear el worktree, la huella, el Workspace,
    # el Mensaje) dejaba la Tarea en EN_CURSO para siempre y tumbaba el tick del worker. Acá se
    # cierra en ERROR con el motivo a la vista, y el worker sigue con la próxima.
    try:
        if tarea.persistente:
            return _ejecutar_persistente(tarea, timeout)
        return _ejecutar_repo(tarea, timeout)
    except Exception as e:  # noqa: BLE001 — cualquier fallo de la tarea se ve en la tarea
        if not tarea.persistente and tarea.repo_path:
            try:
                remover_worktree(tarea.repo_path, Path(workspaces_dir()) / f"tarea-{tarea.pk}")
            except Exception:  # noqa: BLE001 — limpieza best-effort; lo que importa es cerrar la tarea
                pass
        tarea.estado = Tarea.Estado.ERROR
        tarea.salida = f"{tarea.salida or ''}\n\n(❌ la tarea se cayó: {e})".strip()
        tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
        return tarea


def _ejecutar_repo(tarea, timeout):
    """Modo repo: worktree efímero sobre `repo_path`, commit en su branch y desmontaje."""
    # Guard: modo repo SIN repo_path apuntaría al cwd (¡el repo desplegado!). Nunca.
    if not tarea.repo_path:
        tarea.estado = Tarea.Estado.ERROR
        tarea.salida = "(❌ modo repo sin repo_path — abortado para no tocar el repo desplegado)"
        tarea.save(update_fields=['estado', 'salida', 'actualizado_at'])
        return tarea

    branch = f"enjambre/tarea-{tarea.pk}"
    dest = str(Path(workspaces_dir()) / f"tarea-{tarea.pk}")
    base_sha = crear_worktree(tarea.repo_path, tarea.base_ref, branch, dest)
    # Antes del turno, mientras el archivo `gitdir:` lo escribió git y no la silla: de acá sale el
    # git dir que se usa al commitear, nunca de lo que diga ese archivo después.
    git_dir = _git(dest, 'rev-parse', '--absolute-git-dir')
    huella = huella_git(dest)
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
            sha = comitear(dest, f"Enjambre · tarea {tarea.pk}: {tarea.titulo}",
                           huella=huella, git_dir=git_dir)
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
    huella = huella_git(dest)   # testigo de `.git`: se compara antes de commitear
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
            sha = comitear(dest, f"Enjambre · tarea {tarea.pk}: {tarea.titulo}", huella=huella)
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
