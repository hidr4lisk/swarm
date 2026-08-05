#!/usr/bin/env bash
# scripts/build_bundle.sh — arma la CARPETA portátil de Swarm (Linux + Windows en la misma carpeta).
# Va en un pendrive para llevarla de máquina en máquina, o en una carpeta del disco: es lo mismo.
#
# Produce dist/swarm-portable/ con:
#   app/                  el código de Swarm
#   runtime/linux/        Python autocontenido (python-build-standalone) + site-packages Linux
#   runtime/win/          Python autocontenido (python-build-standalone) + site-packages Windows
#   data/                 db.sqlite3 + secrets.enc (se crean al primer arranque; persistente)
#   enjambre.sh           doble-clic en Linux  → migra + worker + web + navegador
#   Enjambre.bat          doble-clic en Windows → idem
#
# Se enchufa (o se copia) en una PC pelada (SIN Python ni Docker), corre el launcher de su SO
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
BASE="${XDG_CACHE_HOME:-$HOME/.cache}/swarm-portable"
CACHE="$BASE/$VER"
MODE_FILE="$BASE/mode"          # elección recordada POR ESTA PC (no en el pendrive) → cada PC nueva pregunta
mkdir -p "$BASE" "$DIR/data"

# El `.ver` no es solo el nombre del cache: es el sha256 (12) del tar.gz que viaja al lado. Se
# verifica ANTES de extraer porque un pendrive puede devolver un archivo del tamaño exacto y con el
# contenido podrido (pasó, 2026-07-28): sin esto el usuario ve un `gzip: invalid compressed
# data--crc error` crudo, que no le dice ni qué archivo es ni qué hacer.
verificar_runtime() {
  [ "$VER" != "dev" ] || return 0            # bundle sin sellar (clone de dev) → no hay con qué comparar
  local real=""
  if command -v sha256sum >/dev/null 2>&1; then real="$(sha256sum "$ARCHIVE" | cut -c1-12)"
  elif command -v shasum >/dev/null 2>&1; then real="$(shasum -a 256 "$ARCHIVE" | cut -c1-12)"
  else return 0; fi                          # sin herramienta de hash → seguimos (tar avisará si rompe)
  [ "$real" != "$VER" ] || return 0
  echo
  echo "  ✗ El runtime llegó DAÑADO: no coincide con su sello."
  echo "      archivo:  $ARCHIVE"
  echo "      esperado: $VER   ·   encontrado: $real"
  echo
  echo "  Suele pasar copiando a un pendrive (copia interrumpida o unidad con errores), o con una"
  echo "  descarga a medias. Volvé a copiar runtime-linux.tar.gz, o bajá de nuevo el zip:"
  echo "      https://github.com/hidr4lisk/swarm/releases/latest"
  echo "  Tus datos NO se tocan: mesas, sillas y bóveda viven en data/, aparte del runtime."
  echo
  exit 1
}

extract() {
  verificar_runtime
  echo "▶ Descomprimiendo el runtime (una vez)…"
  mkdir -p "$1"
  if ! tar -xzf "$ARCHIVE" -C "$1"; then
    echo "  ✗ No se pudo descomprimir el runtime (¿disco lleno, o sin permisos en $1?)."
    exit 1
  fi
}

RUNTIME=""; EPHEMERAL=""
if [ -f "$CACHE/.ok" ]; then
  RUNTIME="$CACHE"                       # ya instalado en esta PC → arranque directo, sin preguntar
else
  MODE="$(cat "$MODE_FILE" 2>/dev/null || echo "")"   # ¿preferencia ya elegida en ESTA PC?
  if [ -z "$MODE" ]; then
    echo
    echo "  Primera vez en esta PC."
    echo "  ¿Instalar Swarm acá para que arranque rápido la próxima vez? (deja ~280 MB en esta PC)"
    echo "  Respondé 'n' para modo SIN RASTRO (se borra al cerrar; se re-arma en cada arranque)."
    if [ -t 0 ]; then read -r -t 60 -p "  ¿Instalar en esta PC? [S/n]: " ans || ans=""; else ans=""; fi
    case "${ans:-S}" in [nN]*) MODE="efimero";; *) MODE="persistente";; esac
    printf '%s' "$MODE" > "$MODE_FILE"
  elif [ "$MODE" = "efimero" ] && [ ! -f "$BASE/noask" ] && [ -t 0 ]; then
    # Ya elegiste efímero antes → ofrecer instalar por si te arrepentís, sin molestar de más
    echo
    echo "  Esta PC está configurada SIN persistencia (Swarm se re-arma en cada arranque)."
    echo "    [I] Instalar acá (arranca rápido la próxima)"
    echo "    [M] Mantener sin rastro   (default, sigue en 6s)"
    echo "    [N] No preguntarme más"
    read -r -t 6 -p "  Elegí [i/M/n]: " up || up=""
    case "${up:-M}" in
      [iI]*) MODE="persistente"; printf '%s' "$MODE" > "$MODE_FILE";;   # pasa a persistente
      [nN]*) : > "$BASE/noask";;                                        # no volver a ofrecer en esta PC
      *) :;;                                                            # mantener efímero
    esac
  fi
  if [ "$MODE" = "persistente" ]; then
    extract "$CACHE"
    "$CACHE/python/bin/python3" -m compileall -q "$CACHE" >/dev/null 2>&1 || true
    touch "$CACHE/.ok"; RUNTIME="$CACHE"
    # Barrer los runtimes de releases anteriores. El cache es `$BASE/$VER` y cada release estrena
    # carpeta, así que sin esto se apilan ~310 MB por versión en TODA PC donde elegiste instalar
    # (medido: 3,0 GB en una máquina que venía usándolo desde julio). Va DESPUÉS del `.ok`, nunca
    # antes: si la extracción falla, la copia vieja sigue sirviendo. El glob `*/` solo matchea
    # directorios, así que `mode` y `noask` —que son archivos— quedan afuera.
    for viejo in "$BASE"/*/; do
      viejo="${viejo%/}"
      [ -d "$viejo" ] || continue
      [ "$viejo" = "$CACHE" ] || rm -rf "$viejo"
    done
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
# Carpetas de trabajo de las mesas (/armar): sin esto caían a ~/.enjambre/mesas del HOST (no
# portátil — te ibas con el pendrive y dejabas los archivos fabricados en esa PC). Mismo sandbox
# de siempre (mesa_workspace crea su propio git repo, nunca toca el árbol desplegado ni el resto
# de la PC — eso es el toolbelt, aparte); esto solo cambia DÓNDE vive ese sandbox.
export ENJAMBRE_MESAS_DIR="$DIR/data/mesas"
export ENJAMBRE_WORKSPACES_DIR="$DIR/data/workspaces"
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
set "BASE=%LOCALAPPDATA%\swarm-portable"
if not exist "%BASE%" mkdir "%BASE%"
set "CACHE=%BASE%\%VER%"
set "MODEFILE=%BASE%\mode"
set "CLEANUP="

if exist "%CACHE%\.ok" (set "RUNTIME=%CACHE%" & goto run)

set "MODE="
set "FIRSTTIME="
if exist "%MODEFILE%" set /p MODE=<"%MODEFILE%"
if defined MODE goto have_mode
set "FIRSTTIME=1"
echo.
echo   Primera vez en esta PC.
echo   Instalar Swarm en esta PC para que arranque rapido la proxima vez?
echo   Deja ~280 MB en el disco. Elegi N para modo SIN RASTRO (se borra al cerrar).
choice /C SN /T 60 /D S /M "  Instalar en esta PC"
if errorlevel 2 (set "MODE=efimero") else (set "MODE=persistente")
> "%MODEFILE%" echo %MODE%

:have_mode
if /I not "%MODE%"=="efimero" goto persistent
rem Ya elegiste efimero antes: ofrecer instalar por si te arrepentis (no apenas lo elegis, ni si pediste no molestar)
if defined FIRSTTIME goto ephemeral
if exist "%BASE%\noask" goto ephemeral
echo.
echo   Esta PC esta configurada SIN persistencia (Swarm se re-arma en cada arranque).
echo     [I] Instalar aca (arranca rapido la proxima)
echo     [M] Mantener sin rastro   (default, sigue en 6s)
echo     [N] No preguntarme mas
choice /C IMN /T 6 /D M /M "  Elegi"
if errorlevel 3 goto noask_set
if errorlevel 2 goto ephemeral
set "MODE=persistente"
> "%MODEFILE%" echo persistente
goto persistent
:noask_set
type nul > "%BASE%\noask"
goto ephemeral

:persistent
set "RUNTIME=%CACHE%"
if exist "%CACHE%\.ok" goto run
call :verificar_runtime
if errorlevel 1 goto fin_error
echo Descomprimiendo el runtime (una vez)...
if not exist "%CACHE%" mkdir "%CACHE%"
tar -xzf "%DIR%runtime-win.tar.gz" -C "%CACHE%" || goto tar_roto
"%CACHE%\python\python.exe" -m compileall -q "%CACHE%" >nul 2>&1
type nul > "%CACHE%\.ok"
rem Barrer los runtimes de releases anteriores (mismo motivo que en enjambre.sh: cada release
rem estrena carpeta y sin esto se apilan ~310 MB por version). Va DESPUES del .ok: si la
rem extraccion falla, la copia vieja sigue sirviendo. Se compara por NOMBRE de carpeta contra
rem %VER%, no por ruta completa, para no depender de como quede formateada la ruta.
for /d %%D in ("%BASE%\*") do if /I not "%%~nxD"=="%VER%" rd /s /q "%%~fD"
goto run

:ephemeral
call :verificar_runtime
if errorlevel 1 goto fin_error
set "RUNTIME=%TEMP%\swarm-%RANDOM%%RANDOM%"
set "CLEANUP=%RUNTIME%"
mkdir "%RUNTIME%"
echo Descomprimiendo el runtime (modo sin rastro)...
tar -xzf "%DIR%runtime-win.tar.gz" -C "%RUNTIME%" || goto tar_roto

:run
set "PYTHONPATH=%DIR%app;%RUNTIME%\site-packages"
set "PYTHONNOUSERSITE=1"
set "DJANGO_SETTINGS_MODULE=swarm.settings"
set "DATABASE_URL=sqlite:///%DIR%data/db.sqlite3"
set "SWARM_DATA_DIR=%DIR%data"
rem Carpetas de trabajo de las mesas (/armar): sin esto caian a %%USERPROFILE%%\.enjambre\mesas
rem del host (no portatil). Mismo sandbox de siempre, solo cambia donde vive.
set "ENJAMBRE_MESAS_DIR=%DIR%data\mesas"
set "ENJAMBRE_WORKSPACES_DIR=%DIR%data\workspaces"
rem El toolbelt (que las sillas operen esta maquina) se prende desde la interfaz:
rem en Conexiones, pestana Toolbelt, activa el switch. NO hace falta tocar este archivo. La linea
rem de abajo es un override opcional para forzarlo SIEMPRE encendido desde el launcher:
rem set "SWARM_TOOLBELT=1"
"%RUNTIME%\python\python.exe" "%DIR%app\manage.py" serve %*

if defined CLEANUP if exist "%CLEANUP%" rmdir /S /Q "%CLEANUP%"
endlocal
goto :eof

rem El .ver que viaja al lado del tar.gz es su sha256 (12). Se verifica ANTES de extraer: un
rem pendrive puede devolver un archivo del tamano exacto y con el contenido podrido, y sin esto
rem el usuario ve un error crudo de tar que no dice ni que archivo es ni que hacer.
:verificar_runtime
if "%VER%"=="dev" goto :eof
set "REAL="
for /f "skip=1 tokens=* delims=" %%H in ('certutil -hashfile "%DIR%runtime-win.tar.gz" SHA256 2^>nul') do (
  if not defined REAL set "REAL=%%H"
)
if not defined REAL goto :eof
set "REAL=%REAL: =%"
set "REAL=%REAL:~0,12%"
if /I "%REAL%"=="%VER%" goto :eof
echo.
echo   X El runtime llego DANADO: no coincide con su sello.
echo       archivo:  %DIR%runtime-win.tar.gz
echo       esperado: %VER%   -   encontrado: %REAL%
echo.
echo   Suele pasar copiando a un pendrive (copia interrumpida o unidad con errores), o con una
echo   descarga a medias. Volve a copiar runtime-win.tar.gz, o baja de nuevo el zip:
echo       https://github.com/hidr4lisk/swarm/releases/latest
echo   Tus datos NO se tocan: mesas, sillas y boveda viven en data\, aparte del runtime.
echo.
rem `exit /b 1` dentro de un `call` NO termina el .bat, solo la subrutina: el que corta es el
rem `if errorlevel 1 goto fin_error` de cada punto de llamada.
exit /b 1

:tar_roto
echo.
echo   X No se pudo descomprimir el runtime (disco lleno, o sin permisos en la carpeta destino).
goto fin_error

:fin_error
echo.
pause
endlocal
exit /b 1
BAT

cat > "$OUT/LEEME.txt" <<'TXT'
SWARM — enjambre multi-agente PORTÁTIL
======================================
No hace falta instalar nada (ni Python ni Docker). Esta carpeta se basta sola: podés dejarla
en el disco de la PC o llevarla en un pendrive — funciona igual en los dos casos.

  · Linux:   doble-clic en  enjambre.sh  → "Ejecutar en terminal"  (o  ./enjambre.sh  en una terminal)
  · Windows: doble-clic en  Enjambre.bat

  ¿Windows avisa "Windows protegió su PC"? Es normal —el instalador no tiene firma paga, el
  código es público en GitHub—: click en "Más información" y después "Ejecutar de todos modos".

La PRIMERA vez te pregunta (en la terminal/consola) si querés INSTALAR Swarm en esta PC para que
arranque rápido la próxima vez —descomprime el runtime al disco local, deja ~280 MB— o usarlo en
modo SIN RASTRO (se borra al cerrar y se re-arma en cada arranque). No hay que editar ningún
archivo: elegís una vez POR PC (se recuerda en esa PC, no en el pendrive → cada máquina nueva
vuelve a preguntar). Si elegiste SIN RASTRO y te arrepentís, el próximo arranque te ofrece
instalarlo en esa PC (o "no preguntarme más"). Tus datos —mesas, sillas y bóveda— viven SIEMPRE
en la carpeta data/ del pendrive y se mueven con él entre Windows y Linux, elijas lo que elijas.

Se abre el navegador en http://127.0.0.1:8799. Para tener sillas hablando hay dos caminos, y la
pantalla "Conexiones" te muestra esa ESCALERA:

  1) CLI opencode — login gratis desde tu terminal; Swarm lo detecta y usa esa cuenta.
  2) API keys — "Conexiones → API keys": elegí una passphrase (mín. 8), pegá tu primera API key
     y tocá Guardar. Con ese único paso la bóveda queda creada, cifrada y ACTIVA. Cuando reabras
     Swarm, desbloqueá con esa misma passphrase.

Tus datos (base + bóveda de keys) viven en la carpeta data/ de acá al lado. Si movés o
copiás esta carpeta entera, se van con ella.

¿Ya tenías tus sillas armadas en otra PC o en una versión anterior? No las rehagas a mano:
en "Sillas" (arriba) tenés EXPORTAR e IMPORTAR. Exportar te baja un archivo .json con toda tu
config —nombres, modelos, prompts, retratos, colores y orden—; importarlo acá te la repone.
Al importar elegís FUSIONAR (pisa las que coinciden y agrega las nuevas, sin borrar nada) o
REEMPLAZAR (deja la mesa igual al archivo). Ese archivo NO lleva credenciales: las API keys
quedan cifradas en la bóveda, aparte — en la máquina nueva las cargás una vez.

Para que las sillas puedan OPERAR la máquina donde corre Swarm (revisar disco, procesos, editar
archivos, arreglar cosas), prendé el TOOLBELT desde la interfaz: "Conexiones → Toolbelt" y activá
el switch (arranca apagado, no hace falta editar nada). OJO, esto es lo importante: EL SWITCH ES
EL PERMISO. Prendido, las sillas —por API key y por CLI— leen, editan y construyen, y todo SE
APLICA EN EL MOMENTO: no se te pide un OK por cada cambio. Lo ves pasar en la mesa mientras pasa,
y todo queda en la Bitácora. Prendelo SOLO en máquinas que estés autorizado a atender.

Salvo que tu pedido nombre una ruta (o escribas /libre), lo que las sillas produzcan queda en la
CARPETA DE LA MESA: junto, versionado y descargable con 📁. Las credenciales del sistema (.ssh,
.gnupg, .aws, /etc/shadow) y la bóveda de keys no las toca el toolbelt.

Para las sillas por API key hace falta un modelo que soporte herramientas (function-calling); las
de CLI usan las suyas.
TXT

echo "✅ Listo: $OUT"
echo "   Al pendrive copiás POCOS archivos grandes (runtime-*.tar.gz + launchers + app/ + data/)."
echo "   Tamaño:  $(du -sh "$OUT" 2>/dev/null | cut -f1)   ·   archivos sueltos en la raíz: $(find "$OUT" -maxdepth 1 -type f | wc -l)"
echo "   Probalo:  cd '$OUT' && ./enjambre.sh"
