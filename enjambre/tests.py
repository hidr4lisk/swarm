"""
enjambre/tests.py — la suite del módulo. Correr con: python manage.py test enjambre

Cubre lo que protege de verdad:
- es_ruido / parse_comando: la semántica fina del filtro y los comandos de mesa.
- El contrato del líder («@alias: subtarea») que consume _parse_asignaciones.
- La degradación de sillas (CLI inexistente, timeout, endpoint HTTP caído): la mesa
  nunca se rompe, la silla queda muda con su marcador.
- Las vistas con el test client: el flujo real del usuario (mesas, sillas, /alto).
- Conexiones: detección por existencia y rutas colapsadas a ~ (sin usuario del host).
- El workspace git de la mesa (idempotente, NOTAS.md, commit inicial).

Sin mocks de red ni servicios externos: los CLIs no se invocan (se prueban los caminos
de error, que son los nuestros); git sí se usa de verdad sobre un tmpdir.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from .clientes import build_comando
from .conexiones import detectar, ruta_corta
from .engine import (
    Enjambre, ejecutar_cli, ejecutar_http, es_ruido, limpiar_salida, parse_comando,
)
from .models import Mensaje, Participante, Sesion
from .workspace import mesa_workspace


class EsRuidoTests(TestCase):
    """Los marcadores propios cuentan solo como PREFIJO; los de proveedor, solo en salidas cortas."""

    def test_marcadores_propios_al_inicio_son_ruido(self):
        self.assertTrue(es_ruido("(❌ claude no instalado)"))
        self.assertTrue(es_ruido("(⏰ timeout tras 180s)"))
        self.assertTrue(es_ruido("(sin respuesta)"))
        self.assertTrue(es_ruido("  (❌ error: x)  "))  # con espacios alrededor

    def test_marcador_propio_en_el_medio_no_es_ruido(self):
        # Regresión mesa 131: una respuesta legítima describía un scoreboard «✅❌📊»
        # y quedaba marcada como error.
        self.assertFalse(es_ruido("El scoreboard quedó así: ✅❌📊 con 3 pasadas verdes."))

    def test_marcador_de_proveedor_corto_es_ruido(self):
        self.assertTrue(es_ruido("Rate limit exceeded. Try again later."))
        self.assertTrue(es_ruido("API error: Overloaded"))

    def test_marcador_de_proveedor_en_texto_largo_no_es_ruido(self):
        # Regresión mesa 127: una respuesta larga puede MENCIONAR "rate limit" sin ser un error.
        texto = ("El endpoint aplica rate limiting agresivo, así que conviene cachear. " * 10)
        self.assertFalse(es_ruido(texto))

    def test_texto_normal_y_vacio(self):
        self.assertFalse(es_ruido("Listo, el script quedó en ordenar.sh"))
        self.assertFalse(es_ruido(""))
        self.assertFalse(es_ruido(None))


class LimpiarSalidaTests(TestCase):
    """Regresiones del primer beta real: el contenedor fresco hace que el CLI re-inicialice
    su estado y lo cuente por stdout; nada de eso es la respuesta."""

    def test_saca_ansi_y_preambulo_de_opencode(self):
        crudo = (
            "Performing one time database migration, may take a few minutes...\n"
            "sqlite-migration:done\n"
            "Database migration complete.\n"
            "\x1b[0m\n> build · big-pickle\n\x1b[0m\n"
            "Hola Humano. Acá funcionando con big-pickle."
        )
        self.assertEqual(limpiar_salida(crudo), "Hola Humano. Acá funcionando con big-pickle.")

    def test_preambulo_sin_respuesta_queda_vacio(self):
        # turno de Nahuel donde opencode solo migró y mostró el banner, sin contestar
        crudo = "sqlite-migration:done\n\x1b[0m\n> build · big-pickle\n\x1b[0m"
        self.assertEqual(limpiar_salida(crudo), "")  # → ejecutar_cli cae a "(sin respuesta)"

    def test_no_toca_el_cuerpo_de_la_respuesta(self):
        texto = "El script usa sqlite-migration: como key del config.\nFin."
        self.assertEqual(limpiar_salida(texto), texto)  # el marker NO está al inicio

    def test_error_de_proveedor_corto_es_ruido(self):
        # opencode logueado con cuenta ChatGPT y modelo no permitido → Bad Request crudo
        self.assertTrue(es_ruido(
            'Error: Bad Request: {"detail":"The model is not supported..."}'))

    def test_mount_de_docker_roto_se_traduce_a_marcador_amigable(self):
        # silla activada SIN tener el CLI en el host: el --mount corta con el error del
        # daemon; tiene que llegar a la mesa como (❌ …) apuntando a Conexiones, no crudo.
        fake = mock.Mock()
        fake.stdout = ''
        fake.stderr = ('docker: Error response from daemon: invalid mount config for type '
                       '"bind": bind source path does not exist: /root/.gemini/antigravity-cli.')
        p = Participante.objects.create(key='sin-cli', nombre='Pelado', comando=['agy', '-p'])
        with mock.patch('enjambre.engine.subprocess.run', return_value=fake):
            salida, ruido = ejecutar_cli(p, 'hola', timeout=5)
        self.assertTrue(ruido)
        self.assertIn('Conexiones', salida)
        self.assertTrue(salida.startswith('(❌ Pelado'))


class ParseComandoTests(TestCase):
    def test_todos_los_verbos(self):
        casos = {
            '/armar un script': ('build', 'un script'),
            '/build algo': ('build', 'algo'),
            '🔨 algo': ('build', 'algo'),
            '/deshacer': ('undo', ''),
            '/volver a1b2c3': ('volver', 'a1b2c3'),
            '/debate tabs vs espacios': ('debate', 'tabs vs espacios'),
            '/alto': ('alto', ''),
            '/continuo armar un blog': ('continuo', 'armar un blog'),
            '/seguí': ('segui', ''),
            '/segui': ('segui', ''),
            '/auto': ('auto', ''),
            '/cerrar': ('cerrar', ''),
        }
        for texto, esperado in casos.items():
            self.assertEqual(parse_comando(texto), esperado, msg=texto)

    def test_sin_verbo_y_case_insensitive(self):
        self.assertEqual(parse_comando('hola mesa'), (None, 'hola mesa'))
        self.assertEqual(parse_comando('/ARMAR algo'), ('build', 'algo'))
        self.assertEqual(parse_comando('  /alto'), ('alto', ''))
        self.assertEqual(parse_comando(None), (None, None))


class ParticipanteTests(TestCase):
    def test_alias_es_la_primera_palabra_limpia(self):
        p = Participante(key='claude', nombre='Claude Code')
        self.assertEqual(p.alias, 'claude')
        p = Participante(key='oc-1', nombre='Zerg-Ling! veloz')
        self.assertEqual(p.alias, 'zergling')
        p = Participante(key='x9', nombre='')
        self.assertEqual(p.alias, 'x9')  # sin nombre cae al key

    def test_motor_prioriza_endpoint_y_lee_el_model_del_comando(self):
        self.assertEqual(Participante(key='q', endpoint_model='qwen2.5:3b').motor, 'qwen2.5:3b')
        p = Participante(key='oc-bp', comando=['opencode', 'run', '--model', 'opencode/big-pickle'])
        self.assertEqual(p.motor, 'big-pickle')
        self.assertEqual(Participante(key='claude', comando=['claude', '-p']).motor, 'Claude Code')

    def test_cmd_trabajo_cae_al_comando_de_charla(self):
        p = Participante(comando=['opencode', 'run'], comando_trabajo=[])
        self.assertEqual(p.cmd_trabajo(), ['opencode', 'run'])
        p.comando_trabajo = ['opencode', 'run', '--agent', 'build']
        self.assertEqual(p.cmd_trabajo(), ['opencode', 'run', '--agent', 'build'])

    def test_persona_para_rango(self):
        p = Participante(persona='A', persona_consulta='B')
        self.assertEqual(p.persona_para(es_consulta=False), 'A')
        self.assertEqual(p.persona_para(es_consulta=True), 'B')
        p.persona_consulta = '   '
        self.assertEqual(p.persona_para(es_consulta=True), 'A')  # B vacía cae a A


class BuildComandoTests(TestCase):
    def test_claude_con_y_sin_modelo(self):
        cmd, cmdt = build_comando('claude', '')
        self.assertEqual(cmd, ['claude', '-p', '--output-format', 'text'])
        cmd, _ = build_comando('claude', 'opus')
        self.assertEqual(cmd[-2:], ['--model', 'opus'])

    def test_agy_inserta_el_modelo_antes_del_p_final(self):
        # En agy el prompt va inmediatamente después de -p: «-p --model X <prompt>» se rompería.
        cmd, _ = build_comando('agy', 'Gemini 3.1 Pro (High)')
        self.assertEqual(cmd[-1], '-p')
        self.assertEqual(cmd[-3:-1], ['--model', 'Gemini 3.1 Pro (High)'])

    def test_ollama_es_http_sin_comando(self):
        self.assertEqual(build_comando('ollama', 'qwen2.5:3b'), ([], []))
        self.assertEqual(build_comando('no-existe', ''), ([], []))


class ParseAsignacionesTests(TestCase):
    """El contrato del modo líder: extraer «@alias: subtarea» del plan, en orden, agrupando."""

    def setUp(self):
        self.sesion = Sesion.objects.create(nombre='t')
        self.w1 = Participante.objects.create(key='w-oc', nombre='Opencito', comando=['opencode', 'run'])
        self.w2 = Participante.objects.create(key='w-muta', nombre='Mutalisco', comando=['opencode', 'run'])
        self.enj = Enjambre(self.sesion)

    def test_extrae_agrupa_y_respeta_el_orden(self):
        plan = (
            "Plan de trabajo:\n"
            "- @opencito: escribir el script base\n"
            "* @mutalisco: armar los tests\n"
            "- @opencito: documentar el uso\n"
            "- @fantasma: esto se ignora (no es worker)\n"
        )
        asignaciones = self.enj._parse_asignaciones(plan, [self.w1, self.w2])
        self.assertEqual([s.key for s, _ in asignaciones], ['w-oc', 'w-muta'])
        self.assertIn('escribir el script base', asignaciones[0][1])
        self.assertIn('documentar el uso', asignaciones[0][1])  # agrupó las 2 del mismo worker
        self.assertEqual(asignaciones[1][1], 'armar los tests')

    def test_plan_sin_asignaciones(self):
        self.assertEqual(self.enj._parse_asignaciones("charla sin arrobas", [self.w1]), [])


class DegradacionSillasTests(TestCase):
    """Cualquier fallo de una silla es un marcador de ruido, nunca una excepción que rompa la mesa."""

    def test_cli_inexistente(self):
        p = Participante.objects.create(key='rota', nombre='Rota', comando=['cli-inexistente-xyz'])
        salida, ruido = ejecutar_cli(p, 'hola', timeout=5)
        self.assertTrue(ruido)
        self.assertIn('no instalado', salida)

    def test_timeout(self):
        p = Participante.objects.create(key='lenta', nombre='Lenta', comando=['sleep'])
        salida, ruido = ejecutar_cli(p, '5', timeout=1)  # corre `sleep 5` con techo de 1s
        self.assertTrue(ruido)
        self.assertIn('timeout', salida)

    def test_endpoint_http_caido(self):
        p = Participante.objects.create(
            key='local', nombre='Local', endpoint_url='http://127.0.0.1:9', endpoint_model='x')
        salida, ruido = ejecutar_http(p, 'hola', timeout=2)
        self.assertTrue(ruido)
        self.assertIn('❌', salida)


class ConexionesTests(TestCase):
    def test_ruta_corta_colapsa_el_home(self):
        self.assertEqual(ruta_corta('/home/pepe/.claude/.credentials.json'), '~/.claude/.credentials.json')
        self.assertEqual(ruta_corta('/root/.gemini/antigravity-cli'), '~/.gemini/antigravity-cli')
        self.assertEqual(ruta_corta('/srv/creds/x.json'), '/srv/creds/x.json')  # fuera del home, intacta
        self.assertEqual(ruta_corta('/home/pepe'), '~')

    def test_detectar_por_existencia(self):
        with tempfile.NamedTemporaryFile() as f:
            env = {
                'SWARM_CLAUDE_CREDS': f.name,                       # existe
                'SWARM_OPENCODE_CREDS': '/no/existe/auth.json',     # no existe
                'SWARM_AGY_CREDS': '/no/existe/antigravity-cli',
            }
            with mock.patch.dict('os.environ', env):
                estados = detectar()
        self.assertTrue(estados['claude'])
        self.assertFalse(estados['opencode'])
        self.assertFalse(estados['agy'])


class VistasTests(TestCase):
    """El flujo real del usuario, con el test client (sin login: single-user)."""

    def test_seeds_vienen_apagadas_y_sin_keys(self):
        sillas = {p.key: p for p in Participante.objects.all()}
        self.assertIn('claude', sillas)
        self.assertIn('opencode', sillas)
        for p in sillas.values():
            self.assertFalse(p.activo)

    def test_paginas_principales_responden(self):
        for nombre in ('enjambre:home', 'enjambre:gestionar_sillas',
                       'enjambre:conexiones', 'enjambre:ayuda'):
            resp = self.client.get(reverse(nombre))
            self.assertEqual(resp.status_code, 200, msg=nombre)

    def test_boton_de_idioma_cambia_la_ui(self):
        # default: español
        self.assertContains(self.client.get(reverse('enjambre:home')), 'Nueva mesa')
        # el botón EN postea al set_language de Django (cookie) → la UI pasa a inglés
        self.client.post('/i18n/setlang/', {'language': 'en'})
        resp = self.client.get(reverse('enjambre:home'))
        self.assertContains(resp, 'New table')
        self.assertNotContains(resp, 'Nueva mesa')

    def test_conexiones_no_expone_el_usuario_del_host(self):
        resp = self.client.get(reverse('enjambre:conexiones'))
        self.assertNotIn(b'/home/', resp.content)  # rutas colapsadas a ~

    def test_ciclo_de_una_mesa(self):
        # crear
        self.client.post(reverse('enjambre:crear_sesion'), {'nombre': 'mi mesa'})
        sesion = Sesion.objects.get(nombre='mi mesa')
        # renombrar
        self.client.post(reverse('enjambre:renombrar_sesion', args=[sesion.pk]), {'nombre': 'otra'})
        sesion.refresh_from_db()
        self.assertEqual(sesion.nombre, 'otra')
        # fijar (toggle)
        self.client.post(reverse('enjambre:fijar_sesion', args=[sesion.pk]))
        sesion.refresh_from_db()
        self.assertTrue(sesion.fijada)
        # preguntar encola el mensaje del humano (participante nulo)
        self.client.post(reverse('enjambre:preguntar', args=[sesion.pk]), {'texto': 'hola mesa'})
        self.assertTrue(Mensaje.objects.filter(
            sesion=sesion, participante__isnull=True, texto='hola mesa').exists())
        # borrar (cascada)
        self.client.post(reverse('enjambre:borrar_sesion', args=[sesion.pk]))
        self.assertFalse(Sesion.objects.filter(pk=sesion.pk).exists())

    def test_alto_prende_la_senal_sin_encolar_el_verbo(self):
        sesion = Sesion.objects.create(nombre='m')
        self.client.post(reverse('enjambre:preguntar', args=[sesion.pk]), {'texto': '/alto'})
        sesion.refresh_from_db()
        self.assertTrue(sesion.detener_solicitado)
        # el /alto NO queda como mensaje humano (quedaría enterrado); va un ack de sistema
        self.assertFalse(Mensaje.objects.filter(sesion=sesion, texto='/alto').exists())
        self.assertTrue(Mensaje.objects.filter(sesion=sesion, es_sistema=True).exists())

    def test_ciclo_de_una_silla(self):
        # crear
        self.client.post(reverse('enjambre:crear_silla'), {'nombre': 'Mi Silla'})
        silla = Participante.objects.get(nombre='Mi Silla')
        self.assertTrue(silla.activo)
        # guardar: cliente+modelo derivan el comando (el usuario no edita JSON)
        self.client.post(reverse('enjambre:guardar_silla', args=[silla.key]), {
            'nombre': 'Mi Silla', 'cliente': 'opencode', 'modelo': 'opencode/big-pickle',
            'persona_a': 'Sos breve.', 'rango': 'control', 'activo': 'on', 'orden': '3',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        silla.refresh_from_db()
        self.assertEqual(silla.comando, ['opencode', 'run', '--model', 'opencode/big-pickle'])
        self.assertEqual(silla.orden, 3)
        # modelo LIBRE: cualquier ID de proveedor vale, no solo los sugeridos (regresión
        # del beta: el <select> cerrado no dejaba elegir gemini/gpt de la cuenta propia)
        self.client.post(reverse('enjambre:guardar_silla', args=[silla.key]), {
            'nombre': 'Mi Silla', 'cliente': 'opencode', 'modelo': 'google/gemini-3-pro',
            'persona_a': 'x', 'rango': 'control', 'activo': 'on', 'orden': '3',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        silla.refresh_from_db()
        self.assertEqual(silla.comando, ['opencode', 'run', '--model', 'google/gemini-3-pro'])
        # clonar: misma config, key nueva
        self.client.post(reverse('enjambre:clonar_silla', args=[silla.key]))
        self.assertEqual(Participante.objects.filter(nombre__startswith='Mi Silla').count(), 2)
        # borrar
        self.client.post(reverse('enjambre:borrar_silla', args=[silla.key]))
        self.assertFalse(Participante.objects.filter(key=silla.key).exists())


class WorkspaceTests(TestCase):
    def test_mesa_workspace_idempotente(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(ENJAMBRE_MESAS_DIR=tmp):
                sesion = Sesion.objects.create(nombre='ws')
                dest = mesa_workspace(sesion)
                self.assertTrue((dest / '.git').exists())
                self.assertTrue((dest / 'NOTAS.md').exists())
                sesion.refresh_from_db()
                self.assertEqual(sesion.workspace_dir, str(dest))
                # segunda llamada: misma carpeta, no re-inicializa ni re-commitea
                self.assertEqual(mesa_workspace(sesion), dest)
                self.assertEqual(Path(dest, 'NOTAS.md').read_text()[:7], '# NOTAS')
