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

# ── 4) Launchers ───────────────────────────────────────────────────────────────────
echo "▶ Launchers…"
cat > "$OUT/enjambre.sh" <<'SH'
#!/usr/bin/env bash
# Doble-clic (o ./enjambre.sh) para arrancar Swarm en Linux. Sin instalar nada.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$DIR/app:$DIR/runtime/linux/site-packages"
export DJANGO_SETTINGS_MODULE="swarm.settings"
export DATABASE_URL="sqlite:///$DIR/data/db.sqlite3"
export SWARM_DATA_DIR="$DIR/data"
# Para que las sillas operen esta máquina (toolbelt), descomentá:
# export SWARM_TOOLBELT=1
exec "$DIR/runtime/linux/python/bin/python3" "$DIR/app/manage.py" serve "$@"
SH
chmod +x "$OUT/enjambre.sh"

cat > "$OUT/Enjambre.bat" <<'BAT'
@echo off
rem Doble-clic para arrancar Swarm en Windows. Sin instalar nada.
setlocal
set "DIR=%~dp0"
set "PYTHONPATH=%DIR%app;%DIR%runtime\win\site-packages"
set "DJANGO_SETTINGS_MODULE=swarm.settings"
set "DATABASE_URL=sqlite:///%DIR%data/db.sqlite3"
set "SWARM_DATA_DIR=%DIR%data"
rem Para que las sillas operen esta maquina (toolbelt), descomenta:
rem set "SWARM_TOOLBELT=1"
"%DIR%runtime\win\python\python.exe" "%DIR%app\manage.py" serve %*
endlocal
BAT

cat > "$OUT/LEEME.txt" <<'TXT'
SWARM — enjambre multi-agente PORTÁTIL
======================================
No hace falta instalar nada (ni Python ni Docker).

  · Linux:   doble-clic en  enjambre.sh   (o  ./enjambre.sh  en una terminal)
  · Windows: doble-clic en  Enjambre.bat

Se abre el navegador en http://127.0.0.1:8799. Andá a "Conexiones → API keys", cargá tus
API keys (se guardan CIFRADAS con una passphrase que elegís vos) y ya podés armar mesas.

Tus datos (base + bóveda de keys) viven en la carpeta data/ de este pendrive.

Para que las sillas puedan OPERAR la máquina donde enchufás el pendrive (revisar disco, procesos,
proponer arreglos), editá el launcher y descomentá la línea SWARM_TOOLBELT. Ojo: es poderoso —
leé cada cambio propuesto antes de aprobarlo en la Bitácora. Solo en máquinas que estés autorizado
a atender.
TXT

echo "✅ Listo: $OUT"
echo "   Probalo:  cd '$OUT' && ./enjambre.sh"
