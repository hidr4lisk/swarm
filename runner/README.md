# Swarm · runner

Corre los CLIs de IA (`claude`, `opencode`, `agy`) **headless en contenedores
descartables** (`docker run --rm`). Es el sandbox de las sillas: el worker setea
`ENJAMBRE_RUNNER` apuntando a `enjambre-run.sh` y el motor antepone el wrapper a
cada CLI. Mismo script en dos modos:

- **dev** — invocado en tu host; chequea rutas localmente.
- **DooD** (`SWARM_DOOD=1`, lo setea el compose) — invocado dentro del contenedor
  `worker`, contra el docker.sock del host. Ahí las rutas de los montajes se
  resuelven **en el host**: se usa `--mount` (que, a diferencia de `-v`, falla
  limpio si la ruta no existe, sin crear directorios basura).

## Credenciales: seed-copy efímero

El host es la única fuente de verdad; cada contenedor recibe una **copia efímera**:

1. Te logueás **una vez en tu terminal** (`claude` → `/login`, `opencode auth login`,
   agy). La app jamás pide ni guarda credenciales propias.
2. `enjambre-run.sh` monta **solo el archivo de credenciales del agente que corre**
   (nunca `~/.claude/` entero) **solo-lectura**, y el HOME del contenedor es un
   **tmpfs**.
3. `entrypoint.sh` (corto, auditable a ojo; horneado en la imagen `swarm-runner`)
   copia las creds al tmpfs. Los refresh de token ocurren ahí y **mueren con el
   contenedor** — nunca vuelven al host.

**Modelo de amenaza (sin vueltas):** los agentes ejecutan comandos arbitrarios dentro
del contenedor y **pueden leer el token montado** — el RO protege integridad, no
confidencialidad. Aceptado para el modelo single-user (tu máquina, tus cuentas).

**Linux-only:** en macOS claude guarda el token en el Keychain, no en archivo.

## Hardening

- `--cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges`
  (DAC_OVERRIDE es la única cap necesaria: leer la cred 600 de tu uid y escribir
  en `/work`, propiedad del uid host).
- `--pids-limit 512 --memory 2g --cpus 2` — un script generado no se lleva puesto el host.
- Binarios de los CLIs montados RO desde el host (son self-contained, un archivo;
  se mantienen actualizados solos con tu instalación).

## Uso directo (debug)

```bash
docker build -f Dockerfile.runner -t swarm-runner:latest .   # el worker del compose lo hace solo

./enjambre-run.sh claude   -p 'Responde solo: PONG'
./enjambre-run.sh opencode run 'Responde solo: PONG'
./enjambre-run.sh agy      -p 'Responde solo: PONG'

# Con workspace montado en /work (cwd del CLI): RW fabrica, RO solo lee (líder)
ENJAMBRE_WORKDIR=/ruta/al/worktree ./enjambre-run.sh claude -p '...'
```

Variables con default razonable en [.env.example](../.env.example).
