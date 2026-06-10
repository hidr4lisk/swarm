#!/usr/bin/env bash
# Swarm · runner — los CLIs de IA headless en un contenedor descartable.
#
# Ejecuta uno de los 3 CLIs (claude | opencode | agy) dentro de un contenedor
# `docker run --rm`. Las credenciales del host entran SOLO-LECTURA y se copian a
# un tmpfs efímero (ver entrypoint.sh); los binarios se montan RO desde el host.
#
# Corre en dos modos, mismo script:
#   · dev   — invocado en tu host (ENJAMBRE_RUNNER=./runner/enjambre-run.sh).
#   · DooD  — invocado dentro del contenedor `worker` del compose (SWARM_DOOD=1),
#     que tiene el docker.sock del host montado. Ahí los paths de los montajes se
#     resuelven en el HOST (no en el worker), así que no se chequea existencia
#     local; `--mount` (a diferencia de `-v`) falla limpio si el path no existe
#     en el host, sin crear directorios basura.
#
# Uso (igual a como lo invoca el worker — ENJAMBRE_RUNNER apunta acá):
#   ./enjambre-run.sh claude   -p 'Responde solo: PONG'
#   ./enjambre-run.sh opencode run 'Responde solo: PONG'
#   ./enjambre-run.sh agy      -p 'Responde solo: PONG'
#
# Variables (todas con default; ver .env.example):
#   SWARM_CLAUDE_CREDS / SWARM_OPENCODE_CREDS / SWARM_AGY_CREDS  -> creds en el host
#   SWARM_CLAUDE_BIN / SWARM_OPENCODE_BIN / SWARM_AGY_BIN        -> binarios en el host
#   ENJAMBRE_IMG               -> imagen (default swarm-runner; cae a python:3.11-slim)
#   ENJAMBRE_WORKDIR=/ruta     -> se monta en /work (RW) y es el cwd del CLI (fabricación)
#   ENJAMBRE_WORKDIR_RO=/ruta  -> se monta en /work SOLO-LECTURA (líder: lee sin editar)
set -euo pipefail

AGENT="${1:?uso: enjambre-run.sh <claude|opencode|agy> [args...]}"; shift || true

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FALLBACK_IMG="python:3.11-slim"

# Imagen de trabajo: swarm-runner (tools + entrypoint horneado). Build:
#   docker build -f Dockerfile.runner -t swarm-runner:latest .
# Sin imagen cae a python:3.11-slim montando el entrypoint del repo (solo dev:
# en DooD ese path local no existe en el host, la imagen es obligatoria).
IMG="${ENJAMBRE_IMG:-swarm-runner:latest}"
docker image inspect "$IMG" >/dev/null 2>&1 || IMG="$FALLBACK_IMG"

# Límites de recursos del contenedor descartable: que un script generado (loop/fork-bomb)
# no se lleve puesto el host. La red queda activa (el CLI la necesita para su API).
LIMITS=(--pids-limit 512 --memory 2g --cpus 2)

# Hardening: el CLI corre como root pero NO necesita las capabilities peligrosas del
# kernel (SYS_ADMIN/SYS_PTRACE/NET_ADMIN/…) → dropear TODAS reduce el blast-radius de un
# script roto/malicioso, sin tocar red ni creds.
# ⚠️ EXCEPCIÓN OBLIGATORIA `--cap-add DAC_OVERRIDE`: tanto las credenciales (600 de tu
#    uid) como la carpeta de la mesa (creada con tu uid) pertenecen al usuario host, pero
#    el contenedor corre como root (uid 0). Sin DAC_OVERRIDE, root NO saltea los permisos
#    de archivo: no puede leer la cred ni escribir en /work. DAC_OVERRIDE solo permite
#    saltear permisos de archivo dentro de los montajes (no da red ni escape de
#    contenedor): es la cap de menor riesgo y la única que hace falta.
# no-new-privileges bloquea escaladas por binarios setuid.
HARDEN=(--cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges)

# Credenciales y binarios EN EL HOST (Linux-only; ver README, § modelo de amenaza).
# En DooD estos defaults con $HOME no sirven (el HOME del worker no es el tuyo):
# el compose pasa las rutas ya resueltas por env.
CLAUDE_CREDS="${SWARM_CLAUDE_CREDS:-$HOME/.claude/.credentials.json}"
OPENCODE_CREDS="${SWARM_OPENCODE_CREDS:-$HOME/.local/share/opencode/auth.json}"
# Config de opencode (default model, proveedores). OPCIONAL (vacío = no se monta):
# sin esto, el opencode del contenedor arranca pelado y elige SU default de proveedor,
# no el tuyo — visto en el primer beta real (quería big-pickle, agarraba GPT).
OPENCODE_CONFIG="${SWARM_OPENCODE_CONFIG:-}"
AGY_CREDS="${SWARM_AGY_CREDS:-$HOME/.gemini/antigravity-cli}"
CLAUDE_BIN="${SWARM_CLAUDE_BIN:-$HOME/.local/bin/claude}"
OPENCODE_BIN="${SWARM_OPENCODE_BIN:-$HOME/.opencode/bin/opencode}"
AGY_BIN="${SWARM_AGY_BIN:-$HOME/.local/bin/agy}"

# Chequeo local de existencia: solo en dev (en DooD el host no es visible; si la ruta
# no existe allá, --mount corta con "bind source path does not exist: <ruta>").
chequear() { # $1 = CLI, $2 = ruta
  [ -n "${SWARM_DOOD:-}" ] && return 0
  [ -e "$2" ] && return 0
  echo "ERROR: no encuentro $2 en el host (CLI: $1)." >&2
  echo "Logueá/instalá el CLI en tu terminal o ajustá la variable en .env." >&2
  exit 1
}

# HOME efímero: tmpfs que muere con el contenedor. exec: los CLIs cachean helpers
# (p.ej. ripgrep) en ~/.cache y los ejecutan desde ahí. El entrypoint (en el repo:
# lo que leés ahí es lo que corre) copia las creds del montaje RO al tmpfs.
run=(docker run --rm -i "${LIMITS[@]}" "${HARDEN[@]}"
     -e HOME=/root --tmpfs /root:rw,exec,size=256m)
PREFIX=()
if [ "$IMG" = "$FALLBACK_IMG" ]; then
  # Imagen genérica sin el entrypoint horneado: montarlo desde el repo (solo dev).
  run+=(-v "$RUNNER_DIR/entrypoint.sh":/entrypoint.sh:ro)
  PREFIX=(bash /entrypoint.sh)
fi

# workspace opcional (worktrees aislados). RW para fabricar; RO para el líder
# (lee/grepea el código real al planificar/integrar, sin poder editar ni commitear).
#
# Con carpeta montada hay commits. El agente puede correr su propio `git commit` DENTRO
# del contenedor; sin esto, esos commits salían con la identidad que el agente dejó en
# .git/config y en UTC, mezclando autores y horas con los del worker (que commitea como
# «Enjambre <enjambre@local>» en hora local). Forzamos identidad y TZ por env
# (pisa .git/config) para que TODO commit de la mesa quede homogéneo.
if [ -n "${ENJAMBRE_WORKDIR:-}" ] || [ -n "${ENJAMBRE_WORKDIR_RO:-}" ]; then
  run+=(-e TZ="${SWARM_TZ:-America/Argentina/Buenos_Aires}"
        -e GIT_AUTHOR_NAME=Enjambre -e GIT_AUTHOR_EMAIL=enjambre@local
        -e GIT_COMMITTER_NAME=Enjambre -e GIT_COMMITTER_EMAIL=enjambre@local)
fi
if [ -n "${ENJAMBRE_WORKDIR:-}" ]; then
  run+=(--mount "type=bind,source=$ENJAMBRE_WORKDIR,target=/work" -w /work)
elif [ -n "${ENJAMBRE_WORKDIR_RO:-}" ]; then
  run+=(--mount "type=bind,source=$ENJAMBRE_WORKDIR_RO,target=/work,readonly" -w /work)
fi

# Cada corrida monta SOLO las credenciales y el binario del agente que va a correr.
# Los binarios de los 3 CLIs son self-contained (un archivo); docker resuelve el
# symlink ~/.local/bin/claude -> versions/<v> del lado del host.
case "$AGENT" in
  claude)
    chequear claude "$CLAUDE_CREDS"; chequear claude "$CLAUDE_BIN"
    run+=(--mount "type=bind,source=$CLAUDE_CREDS,target=/creds/claude-credentials.json,readonly"
          --mount "type=bind,source=$CLAUDE_BIN,target=/usr/local/bin/claude,readonly"
          "$IMG" "${PREFIX[@]}" claude)
    ;;
  opencode)
    chequear opencode "$OPENCODE_CREDS"; chequear opencode "$OPENCODE_BIN"
    run+=(--mount "type=bind,source=$OPENCODE_CREDS,target=/creds/opencode-auth.json,readonly"
          --mount "type=bind,source=$OPENCODE_BIN,target=/usr/local/bin/opencode,readonly")
    if [ -n "$OPENCODE_CONFIG" ]; then
      chequear opencode "$OPENCODE_CONFIG"
      run+=(--mount "type=bind,source=$OPENCODE_CONFIG,target=/creds/opencode-config.json,readonly")
    fi
    run+=("$IMG" "${PREFIX[@]}" opencode)
    ;;
  agy)
    chequear agy "$AGY_CREDS"; chequear agy "$AGY_BIN"
    run+=(--mount "type=bind,source=$AGY_CREDS,target=/creds/agy,readonly"
          --mount "type=bind,source=$AGY_BIN,target=/usr/local/bin/agy,readonly"
          "$IMG" "${PREFIX[@]}" agy)
    ;;
  *)
    echo "ERROR: agente desconocido '$AGENT' (claude|opencode|agy)" >&2; exit 1 ;;
esac

exec "${run[@]}" "$@"
