#!/usr/bin/env bash
# scripts/build_bundle.sh — arma el PENDRIVE portátil de Swarm (Linux + Windows en la misma carpeta).
#
# Produce dist/swarm-portable/ con:
#   app/                  el código de Swarm
#   runtime/linux/        Python autocontenido (python-build-standalone) + site-packages Linux
#   runtime/win/          Python autocontenido (python-build-standalone) + site-packages Windows
#   data/                 db.sqlite3 + secrets.enc (se crean al primer arranque; persistente)
#   enjambre.sh           doble-clic en Linux  → migra + worker + web + navegador
#   Enjambre.bat          doble-clic en Windows → idem
#
# El técnico enchufa el pendrive en una PC pelada (SIN Python ni Docker), corre el launcher de su SO
# y ya tiene el Enjambre en el navegador. Solo carga sus API keys (Conexiones) y le da masa.
#
# Requisitos para BUILDEAR (no para usar): bash, curl, tar, y un pip que pueda descargar wheels
# win_amd64 (cross-build; no compila nada, solo baja y descomprime). Corré esto en jarvis (tiene red).
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────────
PY_VER="${PY_VER:-3.12.7}"
# Release "install_only" de python-build-standalone (astral). Actualizá TAG/DATE a un release vigente:
#   https://github.com/astral-sh/python-build-standalone/releases
PBS_TAG="${PBS_TAG:-20241016}"
PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
LINUX_TARBALL="cpython-${PY_VER}+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
WIN_TARBALL="cpython-${PY_VER}+${PBS_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/dist/swarm-portable}"
REQ="$ROOT/requirements-portable.txt"
WIN_PYTAG="cp312"       # etiqueta de wheels para el cross-build de Windows (ajustá si cambia PY_VER)
WIN_PYVER="3.12"

# ── Poda del runtime ────────────────────────────────────────────────────────────────
# El bundle sin podar son ~515 MB en ~18k archivos (casi todo runtime). Un server Django
# HEADLESS no usa el suite de tests del stdlib, tkinter/idle, headers, símbolos debug ni el
# admin de Django (no está en INSTALLED_APPS). Sacarlos baja a ~280 MB/~9k archivos → menos
# para comprimir, transferir y descomprimir. Se aplica igual a Linux y Windows (stdlib en
# lib/python3.12 vs Lib respectivamente).
prune_runtime() {  # $1 = dir del runtime (contiene python/ + site-packages/)
  local rt="$1" py="$1/python" std=""
  local cand
  for cand in "$py/lib/python3.12" "$py/Lib"; do [ -d "$cand" ] && std="$cand" && break; done
  [ -n "$std" ] || { echo "  ⚠ no encontré el stdlib en $py — no podo"; return 0; }
  # stdlib que un server no usa
  rm -rf "$std/test" "$std/idlelib" "$std/turtledemo" "$std/tkinter" \
         "$std/lib2to3" "$std/pydoc_data" "$std/ensurepip" "$std/config-3.12"* 2>/dev/null || true
  # artefactos de build/dev que no corren nada
  rm -rf "$py/include" "$py/tcl" "$py/libs" 2>/dev/null || true          # headers, datos tk, import-libs (win)
  find "$py" -maxdepth 1 -name '*.pdb' -delete 2>/dev/null || true       # símbolos debug (win)
  rm -rf "$rt/site-packages/django/contrib/admin" 2>/dev/null || true    # admin no está instalado
  # basura común, en stdlib y en nuestro site-packages
  find "$rt" -depth -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$rt" -type f \( -name '*.pyc' -o -name '*.po' -o -name '*.a' \) -delete 2>/dev/null || true
  find "$rt/site-packages" -depth -type d \( -name tests -o -name test \) -exec rm -rf {} + 2>/dev/null || true
}

echo "▶ Swarm portable → $OUT  (Python $PY_VER, PBS $PBS_TAG)"
rm -rf "$OUT"
mkdir -p "$OUT/app" "$OUT/runtime/linux" "$OUT/runtime/win" "$OUT/data"

# ── 1) Copiar el app (sin basura ni datos locales) ─────────────────────────────────
echo "▶ Copiando el código…"
copy_app() {
  tar -C "$ROOT" \
      --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
      --exclude='*.pyc' --exclude='dist' --exclude='data' --exclude='db.sqlite3' \
      --exclude='staticfiles' --exclude='node_modules' \
      -cf - enjambre swarm locale manage.py requirements-portable.txt 2>/dev/null \
    | tar -C "$OUT/app" -xf -
}
copy_app

# ── 2) Runtimes Python autocontenidos por SO ───────────────────────────────────────
fetch_runtime() {  # $1=tarball  $2=destino
  local tb="$1" dest="$2" tmp
  tmp="$(mktemp -d)"
  echo "▶ Bajando $tb…"
  curl -fSL "$PBS_BASE/$tb" -o "$tmp/py.tar.gz"
  tar -C "$tmp" -xf "$tmp/py.tar.gz"          # extrae a $tmp/python/
  mv "$tmp/python" "$dest/python"
  rm -rf "$tmp"
}
fetch_runtime "$LINUX_TARBALL" "$OUT/runtime/linux"
fetch_runtime "$WIN_TARBALL"   "$OUT/runtime/win"

# ── 3) Dependencias por SO en site-packages ────────────────────────────────────────
LINUX_PY="$OUT/runtime/linux/python/bin/python3"
echo "▶ Deps Linux…"
"$LINUX_PY" -m pip install --upgrade pip >/dev/null
"$LINUX_PY" -m pip install -r "$REQ" --target "$OUT/runtime/linux/site-packages"

echo "▶ Deps Windows (cross-build, solo wheels)…"
# No ejecutamos el Python de Windows en Linux: con el Python de Linux BAJAMOS las wheels win_amd64
# (deps transitivas incluidas) y las descomprimimos en el target (pip no compila con
# --only-binary=:all:; solo baja y descomprime → cross-build seguro desde Linux).
"$LINUX_PY" -m pip download -r "$REQ" \
    --platform win_amd64 --python-version "$WIN_PYVER" \
    --implementation cp --abi "$WIN_PYTAG" --only-binary=:all: \
    --dest "$OUT/runtime/win/_wheels"
# Un wheel es un zip: lo DESEMPAQUETAMOS en el site-packages destino. NO usar `pip install`:
# rechaza wheels win_amd64 corriendo en Linux ("not a supported wheel on this platform"),
# incluso con --no-deps. Descomprimir preserva los .pyd/.dll compilados (cffi/cryptography).
for whl in "$OUT/runtime/win/_wheels"/*.whl; do
  "$LINUX_PY" -m zipfile -e "$whl" "$OUT/runtime/win/site-packages"
done
rm -rf "$OUT/runtime/win/_wheels"

# ── 4) Podar y EMPAQUETAR cada runtime en UN tar.gz por SO ──────────────────────────
# Clave de la portabilidad: al pendrive tienen que llegar POCOS archivos grandes. Los ~18k
# archivos del runtime van DENTRO de runtime-<so>.tar.gz (copia secuencial = rápida). El
# launcher lo descomprime UNA vez en el disco local de la PC (no en el pendrive lento).
pack_runtime() {  # $1 = so (linux|win)
  local so="$1"
  local src="$OUT/runtime/$so"
  echo "▶ Podando runtime $so…"
  prune_runtime "$src"
  echo "▶ Empaquetando runtime-$so.tar.gz…"
  tar -C "$src" -czf "$OUT/runtime-$so.tar.gz" python site-packages
  # Sello de versión: si cambia el tar, cambia el hash → el launcher re-extrae al cache nuevo.
  sha256sum "$OUT/runtime-$so.tar.gz" | cut -c1-12 | tr -d '\n' > "$OUT/runtime-$so.ver"
}
pack_runtime linux
pack_runtime win
rm -rf "$OUT/runtime"     # el árbol suelto ya vive dentro de los tar.gz

# ── 5) Launchers ───────────────────────────────────────────────────────────────────
echo "▶ Launchers…"
cat > "$OUT/enjambre.sh" <<'SH'
#!/usr/bin/env bash
# Arrancá Swarm en Linux SIN instalar nada:  ./enjambre.sh  (o doble-clic → "Ejecutar en terminal").
# El runtime viaja comprimido en runtime-linux.tar.gz. La PRIMERA vez pregunta si lo instala en
# esta PC (arranque rápido) o lo usa en modo sin-rastro. No hay que editar ningún archivo.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$DIR/runtime-linux.tar.gz"
VER="$(cat "$DIR/runtime-linux.ver" 2>/dev/null || echo dev)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/swarm-portable/$VER"
MODE_FILE="$DIR/data/.portable_mode"
mkdir -p "$DIR/data"

extract() { echo "▶ Descomprimiendo el runtime (una vez)…"; mkdir -p "$1"; tar -xzf "$ARCHIVE" -C "$1"; }

RUNTIME=""; EPHEMERAL=""
if [ -f "$CACHE/.ok" ]; then
  RUNTIME="$CACHE"                       # ya instalado en esta PC → arranque directo, sin preguntar
else
  MODE="$(cat "$MODE_FILE" 2>/dev/null || echo "")"   # ¿preferencia ya elegida en este pendrive?
  if [ -z "$MODE" ]; then
    echo
    echo "  Primera vez en esta PC."
    echo "  ¿Instalar Swarm acá para que arranque rápido la próxima vez? (deja ~280 MB en esta PC)"
    echo "  Respondé 'n' para modo SIN RASTRO (se borra al cerrar; se re-arma en cada arranque)."
    if [ -t 0 ]; then read -r -t 60 -p "  ¿Instalar en esta PC? [S/n]: " ans || ans=""; else ans=""; fi
    case "${ans:-S}" in [nN]*) MODE="efimero";; *) MODE="persistente";; esac
    printf '%s' "$MODE" > "$MODE_FILE"
  fi
  if [ "$MODE" = "persistente" ]; then
    extract "$CACHE"
    "$CACHE/python/bin/python3" -m compileall -q "$CACHE" >/dev/null 2>&1 || true
    touch "$CACHE/.ok"; RUNTIME="$CACHE"
  else
    RUNTIME="$(mktemp -d)"; EPHEMERAL=1
    trap 'rm -rf "$RUNTIME"' EXIT INT TERM
    extract "$RUNTIME"
  fi
fi

export PYTHONPATH="$DIR/app:$RUNTIME/site-packages"
export PYTHONNOUSERSITE=1   # ignorar ~/.local del equipo ajeno: el bundle es hermético
export DJANGO_SETTINGS_MODULE="swarm.settings"
export DATABASE_URL="sqlite:///$DIR/data/db.sqlite3"
export SWARM_DATA_DIR="$DIR/data"       # los datos SIEMPRE viven en el pendrive, no en el cache
# El toolbelt (que las sillas operen esta máquina) se prende desde la interfaz:
# Conexiones → Toolbelt → switch. NO hace falta tocar este archivo. La línea de abajo es un
# override opcional para forzarlo SIEMPRE encendido desde el launcher:
# export SWARM_TOOLBELT=1
PY="$RUNTIME/python/bin/python3"
if [ -n "$EPHEMERAL" ]; then
  "$PY" "$DIR/app/manage.py" serve "$@"   # sin exec: al salir, el trap borra el temp
else
  exec "$PY" "$DIR/app/manage.py" serve "$@"
fi
SH
chmod +x "$OUT/enjambre.sh"

cat > "$OUT/Enjambre.bat" <<'BAT'
@echo off
rem Doble-clic para arrancar Swarm en Windows. Sin instalar nada. El runtime viaja comprimido en
rem runtime-win.tar.gz; la PRIMERA vez pregunta si lo instala en esta PC o lo usa sin dejar rastro.
setlocal EnableDelayedExpansion
set "DIR=%~dp0"
if not exist "%DIR%data" mkdir "%DIR%data"
set "VER=dev"
if exist "%DIR%runtime-win.ver" set /p VER=<"%DIR%runtime-win.ver"
set "CACHE=%LOCALAPPDATA%\swarm-portable\%VER%"
set "MODEFILE=%DIR%data\.portable_mode"
set "CLEANUP="

if exist "%CACHE%\.ok" (set "RUNTIME=%CACHE%" & goto run)

set "MODE="
if exist "%MODEFILE%" set /p MODE=<"%MODEFILE%"
if defined MODE goto have_mode
echo.
echo   Primera vez en esta PC.
echo   Instalar Swarm en esta PC para que arranque rapido la proxima vez?
echo   Deja ~280 MB en el disco. Elegi N para modo SIN RASTRO (se borra al cerrar).
choice /C SN /T 60 /D S /M "  Instalar en esta PC"
if errorlevel 2 (set "MODE=efimero") else (set "MODE=persistente")
> "%MODEFILE%" echo %MODE%

:have_mode
if /I "%MODE%"=="efimero" goto ephemeral
set "RUNTIME=%CACHE%"
if exist "%CACHE%\.ok" goto run
echo Descomprimiendo el runtime (una vez)...
if not exist "%CACHE%" mkdir "%CACHE%"
tar -xzf "%DIR%runtime-win.tar.gz" -C "%CACHE%"
"%CACHE%\python\python.exe" -m compileall -q "%CACHE%" >nul 2>&1
type nul > "%CACHE%\.ok"
goto run

:ephemeral
set "RUNTIME=%TEMP%\swarm-%RANDOM%%RANDOM%"
set "CLEANUP=%RUNTIME%"
mkdir "%RUNTIME%"
echo Descomprimiendo el runtime (modo sin rastro)...
tar -xzf "%DIR%runtime-win.tar.gz" -C "%RUNTIME%"

:run
set "PYTHONPATH=%DIR%app;%RUNTIME%\site-packages"
set "PYTHONNOUSERSITE=1"
set "DJANGO_SETTINGS_MODULE=swarm.settings"
set "DATABASE_URL=sqlite:///%DIR%data/db.sqlite3"
set "SWARM_DATA_DIR=%DIR%data"
rem El toolbelt (que las sillas operen esta maquina) se prende desde la interfaz:
rem en Conexiones, pestana Toolbelt, activa el switch. NO hace falta tocar este archivo. La linea
rem de abajo es un override opcional para forzarlo SIEMPRE encendido desde el launcher:
rem set "SWARM_TOOLBELT=1"
"%RUNTIME%\python\python.exe" "%DIR%app\manage.py" serve %*

if defined CLEANUP if exist "%CLEANUP%" rmdir /S /Q "%CLEANUP%"
endlocal
BAT

cat > "$OUT/LEEME.txt" <<'TXT'
SWARM — enjambre multi-agente PORTÁTIL
======================================
No hace falta instalar nada (ni Python ni Docker).

  · Linux:   doble-clic en  enjambre.sh  → "Ejecutar en terminal"  (o  ./enjambre.sh  en una terminal)
  · Windows: doble-clic en  Enjambre.bat

La PRIMERA vez te pregunta (en la terminal/consola) si querés INSTALAR Swarm en esta PC para que
arranque rápido la próxima vez —descomprime el runtime al disco local, deja ~280 MB— o usarlo en
modo SIN RASTRO (se borra al cerrar y se re-arma en cada arranque). No hay que editar ningún
archivo: elegís una vez y se recuerda. Tus datos viven SIEMPRE en la carpeta data/ del pendrive,
elijas lo que elijas.

Se abre el navegador en http://127.0.0.1:8799. Andá a "Conexiones → API keys": elegí una
passphrase (mín. 8), pegá tu primera API key y tocá Guardar. Con ese único paso la bóveda queda
creada, cifrada y ACTIVA — ya podés armar mesas. Cuando reabras Swarm, desbloqueá con esa misma
passphrase.

Tus datos (base + bóveda de keys) viven en la carpeta data/ de este pendrive.

Para que las sillas puedan OPERAR la máquina donde enchufás el pendrive (revisar disco, procesos,
proponer arreglos), prendé el TOOLBELT desde la interfaz: "Conexiones → Toolbelt" y activá el
switch (arranca apagado, no hace falta editar nada). Ojo: es poderoso — las LECTURAS son
automáticas, pero cada CAMBIO queda pendiente en la Bitácora hasta que vos lo aprobás. Solo en
máquinas que estés autorizado a atender. Necesita una silla por API key con un modelo que soporte
herramientas (function-calling).
TXT

echo "✅ Listo: $OUT"
echo "   Al pendrive copiás POCOS archivos grandes (runtime-*.tar.gz + launchers + app/ + data/)."
echo "   Tamaño:  $(du -sh "$OUT" 2>/dev/null | cut -f1)   ·   archivos sueltos en la raíz: $(find "$OUT" -maxdepth 1 -type f | wc -l)"
echo "   Probalo:  cd '$OUT' && ./enjambre.sh"
