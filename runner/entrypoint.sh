#!/usr/bin/env bash
# Swarm · runner — seed-copy efímero de credenciales.
#
# enjambre-run.sh monta las credenciales del host SOLO-LECTURA en /creds/ y el
# HOME del contenedor (/root) como tmpfs. Este script COPIA las credenciales al
# tmpfs y ejecuta el CLI. Los refresh de token ocurren sobre el tmpfs y MUEREN
# con el contenedor: nunca se escriben a imágenes, volúmenes ni de vuelta al host.
#
# Corto y auditable a propósito: esto es todo lo que pasa con tus credenciales.
set -euo pipefail

# claude: solo el archivo de credenciales OAuth. La config global (~/.claude.json)
# del host NO se monta (tiene historial y proyectos del usuario); se sintetiza una
# mínima para que el CLI no dispare el onboarding interactivo.
if [ -e /creds/claude-credentials.json ]; then
  mkdir -p /root/.claude
  cp /creds/claude-credentials.json /root/.claude/.credentials.json
  echo '{"hasCompletedOnboarding": true}' > /root/.claude.json
fi

# opencode: el auth.json alcanza para loguear; el config (opcional) trae TU default
# de modelo/proveedores — sin él, el opencode fresco elige su propio default.
if [ -e /creds/opencode-auth.json ]; then
  mkdir -p /root/.local/share/opencode
  cp /creds/opencode-auth.json /root/.local/share/opencode/auth.json
fi
if [ -e /creds/opencode-config.json ]; then
  mkdir -p /root/.config/opencode
  cp /creds/opencode-config.json /root/.config/opencode/opencode.json
fi

# agy: necesita el perfil de antigravity-cli (token + installation_id + settings).
if [ -d /creds/agy ]; then
  mkdir -p /root/.gemini
  cp -r /creds/agy /root/.gemini/antigravity-cli
fi

chmod -R go-rwx /root/.claude /root/.claude.json /root/.local /root/.gemini 2>/dev/null || true

exec "$@"
