# Revisión de Codex CLI en SWARM — 2026-10-15

**Resultado:** en el código publicado en `61d7ccf` la charla funciona, pero fabricar con
Codex CLI **0.162.1** falla antes de invocar al modelo. Corrección **publicada en v1.2.1** y
probada en Linux y Windows. Las pruebas no modificaron bases persistentes de SWARM.

## Hallazgos

### Fabricación incompatible con Codex instalado · gravedad alta · confianza verificado

evidencia: `codex exec --full-auto --help` devuelve exit **2** y
`error: unexpected argument '--full-auto' found`. La plantilla de fabricación de
`61d7ccf:enjambre/clientes.py:94` incluía ese flag. Se ejercitó también mediante
`ejecutar_cli`, con el mismo resultado.

Corrección: `enjambre/clientes.py:90`, sandbox explícito `read-only` / `workspace-write`
y `approval_policy="never"`. La migración `0011_codex_sandbox_explicito` actualiza las
plantillas antiguas guardadas, preserva el modelo y no pisa comandos personalizados.

firma: Sakura

### Un CLI fallido se contaba como respuesta válida · gravedad media · confianza verificado

evidencia: el mismo error de fabricación, invocado por `ejecutar_cli` en la versión original,
devolvió `ruido=False`. El motor no consultaba `returncode`. Por eso el error podía entrar
al historial/contexto y aparecer como una respuesta exitosa.

Corrección: `enjambre/engine.py:523`, toda salida no cero recibe marcador de error. Para
Codex, stderr de progreso sin stdout final se trata como «sin respuesta», no como aporte.
Regresiones en `enjambre/test_codex.py:38` y `:50`, incluida una ejecución real con exit 2
y salida larga/parcial.

firma: Sakura

### Detección de credenciales ignoraba CODEX_HOME · gravedad baja · confianza verificado

evidencia: la implementación anterior de `ruta_creds` solo usaba `SWARM_CODEX_CREDS` o
`~/.codex/auth.json`. La prueba `enjambre/test_codex.py:82` crea un `auth.json` vacío en un
CODEX_HOME temporal y verifica su detección sin leer contenido.

Corrección: `enjambre/conexiones.py:48`, respeta CODEX_HOME y conserva la prioridad del
override explícito. Sigue siendo un indicador de existencia del archivo, **no** validación
del login; no detecta autenticación guardada solo en keyring o variables de entorno.

firma: Sakura

### Windows sin sandbox nativo elegido degrada la escritura · gravedad media · confianza verificado

evidencia: con la plantilla nueva `--sandbox workspace-write`, Windows recién preparado devolvió
exit 0 y «No pude crear smoke.txt: el entorno solo permite lectura». El rollout registró
`sandbox_policy.type = read-only` y el archivo no existió. Con `windows.sandbox = "unelevated"`,
el mismo CLI creó un archivo mediante PowerShell y registró `workspace-write`.

Corrección: se documentó el requisito de configuración nativa de Windows; SWARM no pisa el backend
elegido por el usuario. En la VM de QA se agregó `[windows] sandbox = "unelevated"` preservando
el resto de su configuración. El motor ahora detecta el downgrade en el encabezado de stderr
y lo muestra como error accionable, en vez de contar una fabricación exitosa. El control solo
usa el primer encabezado: un `sandbox: read-only` citado dentro del pedido no dispara un falso error.

firma: Sakura

## Pruebas realizadas

| Camino | Resultado |
|---|---|
| `codex --version` / `codex login status` | 0.162.1, login ChatGPT disponible |
| Charla original con cuenta real | `SWARM_CODEX_OK`, sin ruido |
| Parser original de fabricación | Exit 2 por `--full-auto` |
| Parser de ambas plantillas nuevas | Exit 0, sin inferencia |
| Escritura real con sandbox read-only | Comando intentado; escritura bloqueada; archivo inexistente |
| POST de `/armar` → dispatch real → archivo → commit → mesa | POST 302, `smoke.txt` con contenido exacto, commit creado, árbol limpio, GET 200 |
| Suite antes / después | 179 / **191 tests**, todos verdes |
| `manage.py check` | Sin problemas |
| `makemigrations --check --dry-run` | Sin cambios pendientes de generar |
| Windows 11 build 26200 + Python portable 3.12.7 | Bundle reconstruido, `check` sin problemas, Codex CLI 0.162.1 arranca |
| Regresiones Codex en Windows real | **12 tests verdes**, incluidos parser real, stdin, errores, downgrade y actualización de sillas |
| Cuenta ChatGPT en Windows | Login propio del CLI, sin copiar tokens de jarvis; charla `SWARM_WINDOWS_OK` |
| Solo lectura en Windows | Intento real mediante PowerShell bloqueado, `denied.txt` inexistente |
| `/armar` en Windows con sandbox nativo | `smoke.txt` con contenido exacto, commit `ddbc780`, árbol limpio, mesa HTTP 200 |

Los recorridos reales documentados usaron una **base de tests en memoria**, mesas y datos en un directorio temporal.
No se modificó la DB persistente de SWARM ni la de hidralisk. Los rollouts del CLI
confirmaron el intento real de escritura bloqueado y el comando que creó `smoke.txt`.

Otros ajustes: prompt de Codex por **stdin**, para no depender del límite de argumentos de
Windows ni interpretar un prompt que empieza con un flag; modelo vacío como default del CLI
en lugar de sugerencias no verificadas para la cuenta. README y Ayuda ES/EN actualizados.

## Publicación verificada — 2026-10-15

- [Release v1.2.1](https://github.com/hidr4lisk/swarm/releases/tag/v1.2.1): código
  `55bf979c26dc6ec0987b66b91e43b1cca9d5bd35`; el workflow dejó el sello de versión en
  `main` mediante `6f5fe0f71968225bc882afcc721bd1b955c0bb3e`. El tag apunta al código de
  release; el empaquetado sella la versión antes de construir.
- [CI](https://github.com/hidr4lisk/swarm/actions/runs/38764767088),
  [portable-release](https://github.com/hidr4lisk/swarm/actions/runs/38765116839) y
  [Pages](https://github.com/hidr4lisk/swarm/actions/runs/38765230245): terminados con éxito.
- La [web pública](https://hidr4lisk.github.io/swarm/) respondió HTTP 200, mostró
  **v1.2.1 · 15/10/2026** y el texto de compatibilidad con Codex 0.162.1.
- Se descargó el ZIP público por `releases/latest/download/swarm-portable.zip` (HTTP 200):
  **101.596.805 bytes**, SHA256
  `91070fd83422c8d16d004114a225b9bb3c56fb2899f9e4f64e6e48794ea8af9c`, coincidente con
  el digest de GitHub. CRC íntegro, versión v1.2.1, migración 0011 incluida y los archivos
  modificados de ejecución iguales al código publicado.
- Se verificaron los SHA256 truncados de **ambos** runtimes contra sus `.ver`; el runtime
  Linux extraído del ZIP final pasó `manage.py check` y `migrate --noinput` completo sobre
  SQLite temporal. Esto no es un reensayo del launcher ni una prueba Windows del ZIP final:
  Windows se ejercitó antes con el bundle de QA y overlay descrito abajo.

### Reproducir las regresiones desde fuente

Con las dependencias del repo instaladas, desde la raíz de SWARM (Linux):

```bash
DJANGO_SETTINGS_MODULE=swarm.settings SWARM_SECRET_KEY=swarm-audit-only \
DATABASE_URL=sqlite:///:memory: ENJAMBRE_MESAS_DIR=/tmp/swarm-audit-suite/mesas \
.venv/bin/python manage.py test enjambre --noinput -v 0
```

Para solo las regresiones de Codex, reemplazar `enjambre` por `enjambre.test_codex` en ese comando.
El parser real se comprueba si el ejecutable está disponible; las pruebas con inferencia
requieren además login válido y fueron ejercitadas por separado, no por toda la suite.
Las evidencias crudas de QA en `/tmp` son auxiliares y pueden desaparecer; los resultados,
entorno, alcance y huella del artefacto quedan registrados en este informe versionado.

### Actualizar y publicar de nuevo

Las [notas v1.2.1](releases/v1.2.1.md) detallan cómo conservar **`data/` completa** y
configurar Codex en Windows. Se actualizaron README, Ayuda ES/EN y LEEME.
Para otro release: tests → código en main → notas `docs/releases/<tag>.md` → tag `vX.Y.Z` →
workflow portable-release → comprobar digest/versión/migraciones del ZIP y versión/fecha de
Pages. No editar el sello a mano ni retaggear una versión publicada.

## Contrato y alcance pendiente

Codex conserva el sandbox **workspace-write** aunque el toolbelt esté encendido. Sirve para
construir dentro del workspace de la mesa, pero no equivale al acceso arbitrario a escritura
del host que tienen otras sillas CLI de SWARM. No se agregó bypass del sandbox.

La [referencia oficial de comandos de OpenAI](https://developers.openai.com/codex/cli/reference)
documenta los sandbox explícitos y el prompt `-` por stdin; además se contrastaron los flags
con la ayuda del ejecutable instalado.

### Windows: entorno de QA

Se encendieron el Laboratorio y la VM `win11`. La contraseña local estaba vencida y se
renovó con autorización del dueño. La NIC virtio del snapshot no tenía conexión; se agregó
una e1000e **solo en vivo**, sin modificar la definición persistente de la VM.

La instalación no tenía Codex ni Git: se prepararon binarios oficiales en una carpeta de
QA del usuario, sin instalación global ni copia de credenciales de jarvis. El runtime del
bundle omite `django.test` deliberadamente; para ejecutar las regresiones se agregó solo
esa librería de la misma versión de Django como suplemento **de QA**, no al producto.

La prueba final agregó al bundle reconstruido los dos archivos de código/tests corregidos durante
el diagnóstico mediante un overlay de QA. Se repitieron las regresiones y el recorrido completo
con el código actualizado, una mesa nueva y DB de tests en memoria. Resultado final: `stage=complete`.
Evidencia cruda temporal en jarvis: `/tmp/swarm-codex-windows-20261015/windows-results-final.json`.

El backend `unelevated` usado en la prueba conserva token restringido y límites de filesystem,
pero tiene menor aislamiento de red que `elevated`; no se desactivó el sandbox. El backend se elige
en la configuración de Codex, no desde SWARM. Referencia:
[guía oficial de Windows](https://developers.openai.com/codex/windows).

**No revisado/ejercitado:** versiones anteriores o posteriores de Codex, todos los modelos/planes,
backends Windows `elevated`/MXC, npm wrappers, launchers persistente/efímero, keyring, mesas paralelas con varias sillas
Codex, ni una auditoría general de los demás proveedores. El estado de publicación se consulta en
[Releases](https://github.com/hidr4lisk/swarm/releases). El soporte sigue marcado **experimental**, pero el recorrido probado funciona
en ambos sistemas con Codex CLI 0.162.1.
