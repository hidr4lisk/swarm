# Hidr4lisk_Swarm

**English** | [Español](#hidr4lisk_swarm--español)

A **multi-agent worktable** in your browser: several AI CLIs (`claude`, `opencode`,
`agy`…) sit at the same table, **chat, debate and build real files** on an isolated
git folder, coordinated by a leader — using **your** already-logged-in accounts,
without ever asking for or storing any credential.

![A Swarm table: the seats debate the request, /armar builds the script for real (one commit per turn) and the worker's live flow shows on the right](docs/img/mesa.png)

> The UI is bilingual (EN/ES button in the navbar) — though its soul speaks Spanish,
> *rioplatense* to be precise.

## Quickstart

Requirements: **Linux**, Docker with compose, and at least one AI CLI installed and
logged in **in your terminal** (`claude`, `opencode` and/or `agy`).

<details>
<summary><b>No Docker yet? Install it (Ubuntu/Debian)</b></summary>

```bash
# Docker Engine + compose plugin, from Docker's official repo
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Run docker without sudo (compose mounts your ~/.enjambre and the docker.sock as you)
sudo usermod -aG docker $USER && newgrp docker
```

Other distros / macOS: see the [official guide](https://docs.docker.com/engine/install/).
Note: compose v2 is the `docker compose` subcommand (not the old `docker-compose`).

</details>

```bash
claude            # once, in your terminal: /login  (and/or `opencode auth login`, agy)
git clone git@github.com:hidr4lisk/swarm.git && cd swarm
docker compose up
```

Open **http://localhost:8080**:

1. **Sillas → Conexiones** shows which CLIs were detected.
2. Turn on the seats (*sillas*) for the CLIs you have (they ship disabled, no keys).
3. Create a table (*mesa*) and ask away. With `/armar <request>` the table builds real
   files in its git folder (`~/.enjambre/mesas/mesa-<id>`).

The UI is bilingual — the **EN/ES** button in the navbar switches language. The full
usage guide — table commands (`@alias`, `/armar`, `/debate`, `/continuo`, `/alto`…),
flat/leader topologies and seat configuration — lives in the app's **Help** button
([preview](docs/img/ayuda-en.png)).

Without Docker (dev mode): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
then `python manage.py migrate`, `python manage.py runserver 8080` + in another terminal
`python manage.py enjambre_worker` (CLIs run straight from your PATH; with
`ENJAMBRE_RUNNER=./runner/enjambre-run.sh` they run in throwaway containers, same as
compose). Heads-up: compose uses **postgres** (its own volume) and dev mode falls back
to **SQLite** (`db.sqlite3`) — two separate databases, your tables don't carry over.

Everything configurable comes in through environment variables with sane defaults:
see [.env.example](.env.example). Tests: `python manage.py test enjambre`.

## How it works

| Service | What it does | What it sees |
|---------|--------------|--------------|
| `web` | the table (queues messages, streams via SSE) | the DB and `~/.enjambre`; **no credentials, no docker.sock** |
| `db` | postgres | its own volume |
| `worker` | the real dispatch: launches a **runner** per turn | the DB, `~/.enjambre` and the **docker.sock**; passes credential paths around **without being able to read them** |
| runner | a **throwaway** container per CLI invocation | the CLI binary (RO), **its own** credential (RO → tmpfs copy) and the table's `/work` folder |

**Credentials** use an *ephemeral seed-copy*: your login on the host is the single
source of truth; each runner mounts the file read-only and a short, eyeball-auditable
[`entrypoint.sh`](runner/entrypoint.sh) copies it to a tmpfs that dies with the
container. Token refreshes never flow back to the host and never land in images or
volumes. Full detail in [runner/README.md](runner/README.md).

![Ephemeral seed-copy: login in your terminal → read-only ephemeral copy per turn → dies with the container](docs/img/seed-copy-flow.png)

![The Conexiones screen only reports whether each credential exists — never its contents](docs/img/conexiones.png)

## Threat model — read this before using it

Swarm is **single-user, on your machine**. No sugarcoating:

- The `worker` container mounts **`/var/run/docker.sock`**, which is equivalent to
  **root access to your host**. It's what makes the throwaway runners possible with
  `docker compose up` and nothing else. Do not expose this compose to third parties.
- Agents run arbitrary commands inside the runner and **can read the mounted token**:
  read-only protects your credential's integrity, not its confidentiality. A confused
  or prompt-injected agent could exfiltrate it. Accepted because these are **your**
  accounts running **your** requests on **your** machine.
- Runner mitigations: one throwaway container per invocation, `--cap-drop ALL`
  (+`DAC_OVERRIDE`), `no-new-privileges`, pids/memory/cpu limits, tmpfs HOME.
- The app **never** asks for, stores or logs credentials; the Conexiones screen only
  reports whether the file **exists**.
- **Linux-only**: on macOS `claude` keeps the token in the Keychain (no file to mount).
- The web listens on `localhost:8080` with no human login: don't publish it as-is.

Known limitation: containers run as root, so the files tables build under `~/.enjambre`
end up root-owned on your host (git even complains about *dubious ownership* if you
touch them as your user). It doesn't affect the app; to work on them from your
terminal: `sudo chown -R $USER ~/.enjambre`.

## License

MIT — see [LICENSE](LICENSE).

Built by [Federico Furgiuele](https://github.com/hidr4lisk).

---

# Hidr4lisk_Swarm — Español

Una **mesa de trabajo multi-agente** en tu navegador: varios CLIs de IA (`claude`,
`opencode`, `agy`…) se sientan a la misma mesa, **charlan, debaten y fabrican archivos**
sobre una carpeta git aislada, coordinados por un líder — usando **tus** cuentas ya
logueadas, sin pedirte ni guardar ninguna credencial.

## Quickstart

Requisitos: **Linux**, Docker con compose, y al menos un CLI de IA instalado y
logueado **en tu terminal** (`claude`, `opencode` y/o `agy`).

<details>
<summary><b>¿No tenés Docker? Instalalo (Ubuntu/Debian)</b></summary>

```bash
# Docker Engine + plugin compose, desde el repo oficial de Docker
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Usar docker sin sudo (compose monta tu ~/.enjambre y el docker.sock como tu usuario)
sudo usermod -aG docker $USER && newgrp docker
```

Otras distros / macOS: ver la [guía oficial](https://docs.docker.com/engine/install/).
Ojo: compose v2 es el subcomando `docker compose` (no el viejo `docker-compose`).

</details>

```bash
claude            # una vez, en tu terminal: /login  (y/o `opencode auth login`, agy)
git clone git@github.com:hidr4lisk/swarm.git && cd swarm
docker compose up
```

Abrí **http://localhost:8080**:

1. **Sillas → Conexiones** te muestra qué CLIs quedaron detectados.
2. Encendé las sillas de los CLIs que tengas (vienen apagadas, sin ninguna key).
3. Creá una mesa y preguntá. Con `/armar <pedido>` la mesa fabrica archivos de verdad
   en su carpeta git (`~/.enjambre/mesas/mesa-<id>`).

La UI es bilingüe — el botón **ES/EN** de la navbar cambia el idioma. La guía completa
de uso — comandos de la mesa (`@alias`, `/armar`, `/debate`, `/continuo`, `/alto`…),
topologías plana/líder y configuración de sillas — vive en el botón **Ayuda** de la app
([vista previa](docs/img/ayuda.png)).

Sin Docker (modo dev): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
después `python manage.py migrate`, `python manage.py runserver 8080` + en otra terminal
`python manage.py enjambre_worker` (los CLIs corren directo de tu PATH; con
`ENJAMBRE_RUNNER=./runner/enjambre-run.sh` corren en contenedores descartables como en
el compose). Ojo: el compose usa **postgres** (con su volumen) y el modo dev cae a
**SQLite** (`db.sqlite3`) — son dos bases separadas, las mesas no se comparten entre modos.

Todo lo configurable entra por variables de entorno con defaults razonables:
ver [.env.example](.env.example). Tests: `python manage.py test enjambre`.

## Cómo funciona

| Servicio | Qué hace | Qué ve |
|----------|----------|--------|
| `web` | la mesa (encola mensajes, streamea por SSE) | la DB y `~/.enjambre`; **ni credenciales ni docker.sock** |
| `db` | postgres | su volumen |
| `worker` | el dispatch real: por cada turno lanza un **runner** | la DB, `~/.enjambre` y el **docker.sock**; pasa rutas de credenciales **sin poder leerlas** |
| runner | un contenedor **descartable** por invocación de CLI | el binario del CLI (RO), **su** credencial (RO → copia en tmpfs) y la carpeta `/work` de la mesa |

Las **credenciales** usan un *seed-copy efímero*: tu login en el host es la única
fuente de verdad; cada runner monta el archivo solo-lectura y un
[`entrypoint.sh`](runner/entrypoint.sh) corto y auditable lo copia a un tmpfs que
muere con el contenedor. Los refresh de token nunca vuelven al host ni quedan en
imágenes o volúmenes. Detalle completo en [runner/README.md](runner/README.md).

![Seed-copy efímero: login en tu terminal → copia efímera solo-lectura por turno → muere con el contenedor](docs/img/credenciales-flujo.png)

## Modelo de amenaza — leelo antes de usarlo

Swarm es **single-user en tu máquina**. Dicho sin vueltas:

- El contenedor `worker` monta **`/var/run/docker.sock`**, que equivale a **acceso
  root a tu host**. Es lo que permite lanzar los runners descartables con `docker
  compose up` y nada más. No expongas este compose a terceros.
- Los agentes ejecutan comandos arbitrarios dentro del runner y **pueden leer el
  token montado**: el RO protege la integridad de tu credencial, no su
  confidencialidad. Un agente confundido o prompt-injected podría exfiltrarla. Se
  acepta porque son **tus** cuentas corriendo **tus** pedidos en **tu** máquina.
- Mitigaciones en el runner: contenedor descartable por invocación, `--cap-drop ALL`
  (+`DAC_OVERRIDE`), `no-new-privileges`, límites de pids/memoria/cpu, HOME en tmpfs.
- La app **jamás** pide, guarda ni loguea credenciales; la pantalla Conexiones solo
  reporta si el archivo **existe**.
- **Linux-only**: en macOS `claude` guarda el token en el Keychain (no hay archivo
  que montar).
- La web escucha en `localhost:8080` sin login humano: no la publiques tal cual.

Limitación conocida: los contenedores corren como root, así que los archivos que las
mesas fabrican en `~/.enjambre` quedan de root en tu host (git incluso se queja de
*dubious ownership* si los tocás desde tu usuario). Dentro de la app no afecta; para
trabajarlos desde tu terminal: `sudo chown -R $USER ~/.enjambre`.

## Licencia

MIT — ver [LICENSE](LICENSE).

Desarrollado por [Federico Furgiuele](https://github.com/hidr4lisk).
