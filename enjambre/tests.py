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
from . import conexiones as conexiones_mod
from .conexiones import detectar, resolver_bin, ruta_corta
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

    def test_ruta_corta_colapsa_el_home_windows(self):
        self.assertEqual(ruta_corta(r'C:\Users\Fede\.local\share\opencode\auth.json'),
                         r'~\.local\share\opencode\auth.json')
        self.assertEqual(ruta_corta('C:/Users/Fede/.claude/.credentials.json'),
                         '~/.claude/.credentials.json')
        self.assertEqual(ruta_corta(r'D:\Users\Fede'), '~')                     # otra unidad, también home
        self.assertEqual(ruta_corta(r'C:\Users'), r'C:\Users')                  # sin usuario, intacta
        self.assertEqual(ruta_corta(r'C:\ProgramData\x.json'), r'C:\ProgramData\x.json')  # fuera del home
        self.assertEqual(ruta_corta(r'C:\Users\Fede2024\creds'), r'~\creds')    # usuario alfanumérico

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

    def test_resolver_bin_path_primero(self):
        self.assertTrue(resolver_bin('sh'))  # en PATH → lo devuelve de ahí

    def test_resolver_bin_fallback_a_dirs_tipicos(self):
        # Binario fuera del PATH pero en un dir típico de instalación (el caso
        # doble-clic del pendrive: el rc de la shell no cargó su export PATH).
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'clifantasma'
            fake.write_text('#!/bin/sh\n')
            fake.chmod(0o755)
            with mock.patch.object(conexiones_mod, '_DIRS_BIN', [d]):
                self.assertEqual(resolver_bin('clifantasma'), str(fake))
        self.assertIsNone(resolver_bin('clifantasma'))  # sin ese dir, no está


class VistasTests(TestCase):
    """El flujo real del usuario, con el test client (sin login: single-user)."""

    def test_seeds_vienen_apagadas_y_sin_keys(self):
        # Los CLIs siguen apagados (requieren login). La ÚNICA activa de fábrica es Chispa
        # (escalón 0: sin credencial, no hay nada que filtrar).
        sillas = {p.key: p for p in Participante.objects.all()}
        self.assertIn('claude', sillas)
        self.assertIn('opencode', sillas)
        self.assertFalse(sillas['claude'].activo)
        self.assertFalse(sillas['opencode'].activo)
        activas = [p.key for p in sillas.values() if p.activo]
        self.assertEqual(activas, ['chispa'])

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


class ToolbeltTests(TestCase):
    """Los frenos del toolbelt: la bóveda no se lee, y las banderas escritoras de find no pasan."""

    def test_read_file_no_lee_la_boveda(self):
        from .toolbelt import _read_file
        with tempfile.TemporaryDirectory() as tmp:
            secreto = Path(tmp) / '.secrets.runtime.json'
            secreto.write_text('{"anthropic": "sk-no-deberia-verse"}')
            out = _read_file(str(secreto))
            self.assertIn('⛔', out)
            self.assertNotIn('sk-no-deberia-verse', out)
            out = _read_file(str(Path(tmp) / 'secrets.enc'))
            self.assertIn('⛔', out)

    def test_inspect_no_menciona_la_boveda(self):
        from .toolbelt import _correr_readonly
        out, err = _correr_readonly('cat /algun/lado/.secrets.runtime.json')
        self.assertIsNone(out)
        self.assertIn('bóveda', err)
        out, err = _correr_readonly('grep -r clave secrets.enc')
        self.assertIsNone(out)
        self.assertIn('bóveda', err)

    def test_find_okdir_bloqueado(self):
        from .toolbelt import _correr_readonly
        for flag in ('-okdir', '-exec', '-delete'):
            out, err = _correr_readonly(f'find /tmp {flag} rm {{}} ;')
            self.assertIsNone(out, flag)
            self.assertIn(flag, err)

    def test_lecturas_normales_siguen_pasando(self):
        from .toolbelt import _correr_readonly
        out, err = _correr_readonly('pwd')
        self.assertIsNone(err)
        self.assertTrue(out)


class ParamTokensTests(TestCase):
    """OpenAI real exige max_completion_tokens; los compatibles siguen con max_tokens."""

    def test_openai_directo_usa_max_completion_tokens(self):
        from .providers.openai_compat import _param_tokens
        self.assertEqual(_param_tokens(''), 'max_completion_tokens')

    def test_compatibles_siguen_con_max_tokens(self):
        from .providers.openai_compat import _param_tokens
        self.assertEqual(_param_tokens('https://openrouter.ai/api/v1'), 'max_tokens')
        self.assertEqual(_param_tokens('https://api.groq.com/openai/v1'), 'max_tokens')


class SinKeyGateTests(TestCase):
    """El gate de API key del dispatcher: los proveedores SIN_KEY (pollinations) pasan sin
    credencial; el resto sigue exigiéndola. Es el cambio más delicado del escalón 0."""

    def test_pollinations_sin_key_no_devuelve_marcador_de_sin_key(self):
        from .providers import chat
        with mock.patch('enjambre.providers.openai_compat._http_json',
                        return_value=(True, {'choices': [{'message': {'content': 'hola'}}]})):
            self.assertEqual(chat('pollinations', 'openai-fast', 'hi', '', timeout=5), 'hola')

    def test_openai_sin_key_sigue_gateado(self):
        from .providers import chat
        salida = chat('openai', 'gpt-5.2', 'hi', '', timeout=5)
        self.assertIn('sin API key', salida)

    def test_chat_agentic_pollinations_degrada_a_charla_plana(self):
        # SEGURIDAD: un endpoint anónimo no dirige el toolbelt — chat_agentic cae a chat().
        from .providers import chat_agentic
        with mock.patch('enjambre.providers.openai_compat._http_json',
                        return_value=(True, {'choices': [{'message': {'content': 'ok'}}]})) as m:
            salida = chat_agentic('pollinations', 'openai-fast', 'hi', '', 5,
                                  sesion=None, participante=None)
        self.assertEqual(salida, 'ok')
        # el payload NO lleva tools (charla plana, no loop agéntico)
        payload = m.call_args[0][1]
        self.assertNotIn('tools', payload)


class PollinationsClienteTests(TestCase):
    """El cliente del escalón 0: URL final correcta y Bearer con/sin token."""

    def _llamar(self, api_key):
        from .providers import pollinations
        with mock.patch('enjambre.providers.openai_compat._http_json',
                        return_value=(True, {'choices': [{'message': {'content': 'x'}}]})) as m:
            pollinations.chat('', 'hola', api_key, timeout=5)
        return m.call_args[0]  # (url, payload, headers, timeout)

    def test_url_modelo_default_y_atribucion(self):
        url, payload, headers, _ = self._llamar('')
        self.assertEqual(url, 'https://text.pollinations.ai/openai/chat/completions')
        self.assertEqual(payload['model'], 'openai-fast')
        self.assertIn('Referer', headers)
        # base_url custom → habla max_tokens, no max_completion_tokens (verificado con curl)
        self.assertIn('max_tokens', payload)

    def test_bearer_vacio_y_con_token(self):
        _, _, headers, _ = self._llamar('')
        self.assertEqual(headers['Authorization'], 'Bearer ')  # verificado inocuo en vivo
        _, _, headers, _ = self._llamar('tok123')
        self.assertEqual(headers['Authorization'], 'Bearer tok123')


class GeminiClienteTests(TestCase):
    """El proveedor gemini: URL final del endpoint OpenAI-compat de Google, Bearer y registro."""

    def _llamar(self, model=''):
        from .providers import gemini
        with mock.patch('enjambre.providers.openai_compat._http_json',
                        return_value=(True, {'choices': [{'message': {'content': 'x'}}]})) as m:
            gemini.chat(model, 'hola', 'AIza-test', timeout=5)
        return m.call_args[0]  # (url, payload, headers, timeout)

    def test_url_base_fija_y_modelo_default(self):
        url, payload, headers, _ = self._llamar()
        self.assertEqual(
            url, 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')
        self.assertEqual(payload['model'], 'gemini-3.5-flash')
        self.assertEqual(headers['Authorization'], 'Bearer AIza-test')

    def test_dispatcher_rutea_y_sin_key_es_marcador(self):
        from . import providers
        with mock.patch('enjambre.providers.gemini.chat', return_value='ok') as m:
            self.assertEqual(providers.chat('gemini', 'g', 'hola', 'k', 5), 'ok')
        m.assert_called_once()
        self.assertTrue(providers.chat('gemini', 'g', 'hola', '', 5).startswith('(❌ sin API key'))

    def test_registro_y_vault(self):
        from . import vault
        from .clientes import api_de, es_api, modelo_de
        self.assertIn('gemini', vault.PROVIDERS)
        p = Participante.objects.create(
            key='gem-t', nombre='Gema', comando=['api-gemini', '--model', 'gemini-3.1-pro'])
        self.assertTrue(es_api('api-gemini'))
        self.assertEqual(api_de(p), 'gemini')
        self.assertEqual(modelo_de(p), 'gemini-3.1-pro')

    def test_listar_modelos_pela_el_prefijo_models(self):
        from . import providers
        data = {'data': [{'id': 'models/gemini-3.5-flash'}, {'id': 'models/gemini-3.1-pro'}]}
        with mock.patch('enjambre.providers._http_get_json', return_value=data):
            src, models, _ = providers.listar_modelos('gemini', api_key='AIza-test')
        self.assertEqual(src, 'live')
        self.assertEqual([m['id'] for m in models], ['gemini-3.5-flash', 'gemini-3.1-pro'])


class ClientePollinationsTests(TestCase):
    """El registro del cliente sin_key: derivaciones y el precio $0 del escalón 0."""

    def _silla(self, **kw):
        base = dict(key='chispa-t', nombre='Chispa',
                    comando=['api-pollinations', '--model', 'openai-fast'])
        base.update(kw)
        return Participante.objects.create(**base)

    def test_derivaciones(self):
        from .clientes import api_de, cliente_de, edita_archivos, es_api, modelo_de
        p = self._silla()
        self.assertTrue(es_api('api-pollinations'))
        self.assertEqual(api_de(p), 'pollinations')
        self.assertEqual(cliente_de(p), 'api-pollinations')
        self.assertEqual(modelo_de(p), 'openai-fast')
        self.assertFalse(edita_archivos(p))  # api:* no fabrica por CLI

    def test_endpoint_url_gana_sobre_api(self):
        from .clientes import api_de
        p = self._silla(key='chispa-ollama', endpoint_url='http://lab:11434')
        self.assertEqual(api_de(p), '')

    def test_precio_cero(self):
        # Regresión: 'openai-fast' no matchea PRECIOS_POR_MODELO ni FREE_MARKERS → sin la regla
        # sin_key caería al default (3/15) y la silla gratis marcaría costo falso.
        from .clientes import precio_silla
        self.assertEqual(precio_silla(self._silla()), (0.0, 0.0))

    def test_vault_acepta_token_opcional(self):
        from . import vault
        self.assertIn('pollinations', vault.TODOS)
        self.assertNotIn('pollinations', vault.PROVIDERS)

    def test_listar_modelos_filtra_tier_anonimo(self):
        from .providers import listar_modelos
        catalogo = [
            {'name': 'openai-fast', 'tier': 'anonymous', 'tools': True},
            {'name': 'gemini-fast', 'tier': 'seed', 'tools': True},
        ]
        with mock.patch('enjambre.providers._http_get_json', return_value=catalogo):
            source, out, nota = listar_modelos('pollinations')
        self.assertEqual(source, 'live')
        self.assertEqual([m['id'] for m in out], ['openai-fast'])  # sin token, solo anónimo
        self.assertTrue(out[0]['free'] and out[0]['tools'])
        self.assertIn('15 s', nota)
        with mock.patch('enjambre.providers._http_get_json', return_value=catalogo):
            _, out, nota = listar_modelos('pollinations', api_key='tok')
        self.assertEqual(len(out), 2)  # con token entra el tier seed
        self.assertEqual(nota, '')


class RatelimitTests(TestCase):
    """El espaciador del tier gratis: espacia, persiste a archivo y castiga tras 429."""

    def setUp(self):
        from . import ratelimit
        ratelimit._ultimo.clear()
        ratelimit._castigo.clear()
        self.tmp = tempfile.mkdtemp()

    def _con_data_dir(self):
        return override_settings(SWARM_DATA_DIR=self.tmp)

    def test_espacia_dos_requests(self):
        import time as t
        from . import ratelimit
        with self._con_data_dir(), \
                mock.patch.dict(ratelimit.INTERVALOS, {'x': (0.2, 0.1)}), \
                mock.patch('enjambre.vault.get_key', return_value=''):
            t0 = t.monotonic()
            ratelimit.esperar('x')
            ratelimit.esperar('x')
            self.assertGreaterEqual(t.monotonic() - t0, 0.2)

    def test_token_acelera(self):
        from . import ratelimit
        with mock.patch.dict(ratelimit.INTERVALOS, {'x': (0.2, 0.1)}):
            with mock.patch('enjambre.vault.get_key', return_value='tok'):
                self.assertEqual(ratelimit.intervalo_de('x'), 0.1)
            with mock.patch('enjambre.vault.get_key', return_value=''):
                self.assertEqual(ratelimit.intervalo_de('x'), 0.2)

    def test_key_sin_limite_es_noop(self):
        import time as t
        from . import ratelimit
        t0 = t.monotonic()
        ratelimit.esperar('openai')  # no está en INTERVALOS
        self.assertLess(t.monotonic() - t0, 0.05)

    def test_persiste_el_sello_a_archivo(self):
        import json as j
        from . import ratelimit
        with self._con_data_dir(), \
                mock.patch.dict(ratelimit.INTERVALOS, {'x': (0.01, 0.01)}), \
                mock.patch('enjambre.vault.get_key', return_value=''):
            ratelimit.esperar('x')
            data = j.loads((Path(self.tmp) / '.ratelimit.json').read_text())
        self.assertIn('x', data)

    def test_castigo_estira_la_ventana(self):
        import time as t
        from . import ratelimit
        with self._con_data_dir(), \
                mock.patch.dict(ratelimit.INTERVALOS, {'x': (0.05, 0.05)}), \
                mock.patch('enjambre.vault.get_key', return_value=''):
            ratelimit.esperar('x')
            ratelimit.castigar('x', 0.4)  # Retry-After simulado
            t0 = t.monotonic()
            ratelimit.esperar('x')
            self.assertGreaterEqual(t.monotonic() - t0, 0.3)

    def test_throttle_key_llega_desde_pollinations(self):
        from .providers import pollinations
        with mock.patch('enjambre.providers.openai_compat._http_json',
                        return_value=(True, {'choices': [{'message': {'content': 'x'}}]})) as m:
            pollinations.chat('', 'hola', '', timeout=5)
        self.assertEqual(m.call_args.kwargs.get('throttle_key'), 'pollinations')


class ClasificarErroresTests(TestCase):
    """El contrato del que depende la cascada: cada marcador que el camino API puede emitir."""

    def test_tabla_completa(self):
        from .providers.errores import clasificar
        casos = {
            'Listo, el script quedó en ordenar.sh': 'ok',
            '(sin respuesta)': 'ok',   # el modelo contestó vacío — no es error
            '(❌ HTTP 429: rate limited)': 'reintentable',
            '(❌ HTTP 500)': 'reintentable',
            '(❌ HTTP 529: overloaded)': 'reintentable',
            '(⏰ timeout tras 180s)': 'reintentable',
            '(❌ respuesta no-JSON del proveedor)': 'reintentable',
            '(❌ sin conexión: [Errno -3] …)': 'reintentable',
            '(❌ HTTP 400: bad request)': 'terminal',
            '(❌ HTTP 401: invalid key)': 'terminal',
            '(❌ HTTP 404: model not found)': 'terminal',
            '(❌ sin API key para este proveedor — …)': 'terminal',
            '(❌ proveedor API desconocido: x)': 'terminal',
            '(❌ API key inválida: tiene caracteres raros — …)': 'terminal',
            '(❌ respuesta inesperada del proveedor)': 'terminal',
        }
        for marcador, esperado in casos.items():
            with self.subTest(marcador=marcador):
                self.assertEqual(clasificar(marcador), esperado)


class RetryHttpJsonTests(TestCase):
    """La capa 1 de la cascada: retry de transitorios en _http_json, sin retry de terminales."""

    def _correr(self, intentos):
        from .providers import _http_json
        with mock.patch('enjambre.providers._intento', side_effect=intentos) as m, \
                mock.patch('enjambre.providers.time.sleep'):
            resultado = _http_json('http://x/v1/chat/completions', {}, {}, timeout=5)
        return resultado, m.call_count

    def test_429_reintenta_y_sale_ok(self):
        (ok, data), llamadas = self._correr([
            (False, ('(❌ HTTP 429: rate limited)', None)),
            (True, {'choices': []}),
        ])
        self.assertTrue(ok)
        self.assertEqual(llamadas, 2)

    def test_400_no_reintenta(self):
        (ok, marcador), llamadas = self._correr([(False, ('(❌ HTTP 400: bad request)', None))])
        self.assertFalse(ok)
        self.assertEqual(llamadas, 1)
        self.assertIn('400', marcador)

    def test_sin_conexion_no_reintenta(self):
        (ok, marcador), llamadas = self._correr([(False, '(❌ sin conexión: DNS caído)')])
        self.assertFalse(ok)
        self.assertEqual(llamadas, 1)

    def test_transitorio_persistente_agota_los_intentos(self):
        from .providers import MAX_INTENTOS
        fallo = (False, ('(❌ HTTP 503)', None))
        (ok, marcador), llamadas = self._correr([fallo] * MAX_INTENTOS)
        self.assertFalse(ok)
        self.assertEqual(llamadas, MAX_INTENTOS)
        self.assertIn('503', marcador)

    def test_respeta_retry_after(self):
        from .providers import _http_json
        with mock.patch('enjambre.providers._intento', side_effect=[
                (False, ('(❌ HTTP 429)', '7')), (True, {})]), \
                mock.patch('enjambre.providers.time.sleep') as s:
            _http_json('http://x', {}, {}, timeout=5)
        s.assert_called_once_with(7.0)

    def test_timeout_sale_como_reloj_y_reintenta(self):
        (ok, marcador), llamadas = self._correr([
            (False, '(⏰ timeout tras 5s)'), (False, '(⏰ timeout tras 5s)'),
            (True, {'r': 1})])
        self.assertTrue(ok)
        self.assertEqual(llamadas, 3)


class SeedChispaTests(TestCase):
    """La migración 0006: Chispa existe, apunta a Pollinations y es la única activa de fábrica."""

    def test_chispa_sembrada_activa_y_gratis(self):
        from .clientes import api_de, precio_silla
        chispa = Participante.objects.get(key='chispa')
        self.assertTrue(chispa.activo)
        self.assertTrue(chispa.permitir_consulta)
        self.assertEqual(api_de(chispa), 'pollinations')
        self.assertEqual(precio_silla(chispa), (0.0, 0.0))
        self.assertEqual(Participante.objects.filter(activo=True).count(), 1)


class OnboardingTests(TestCase):
    """El estado de la escalera, con filesystem y bóveda mockeados (sin red, sin binarios)."""

    def test_arranque_de_fabrica(self):
        from . import onboarding
        with mock.patch('enjambre.conexiones.resolver_bin', return_value=None), \
                mock.patch('enjambre.conexiones.detectar', return_value={'opencode': False}), \
                mock.patch('enjambre.vault.configured_providers', return_value=[]):
            e = onboarding.escalones()
        self.assertTrue(e[0]['listo'])    # Chispa sembrada y activa
        self.assertFalse(e[1]['listo'])
        self.assertFalse(e[1]['instalado'])
        self.assertFalse(e[2]['listo'])
        self.assertFalse(onboarding.completa())

    def test_opencode_instalado_sin_login(self):
        # El estado más probable del escalón 1: binario sí, credencial no → CTA de login.
        from . import onboarding
        with mock.patch('enjambre.conexiones.resolver_bin', return_value='/usr/bin/opencode'), \
                mock.patch('enjambre.conexiones.detectar', return_value={'opencode': False}):
            e = onboarding.escalones()
        self.assertFalse(e[1]['listo'])
        self.assertTrue(e[1]['instalado'])
        self.assertFalse(e[1]['logueado'])

    def test_escalera_completa(self):
        from . import onboarding
        with mock.patch('enjambre.conexiones.resolver_bin', return_value='/usr/bin/opencode'), \
                mock.patch('enjambre.conexiones.detectar', return_value={'opencode': True}), \
                mock.patch('enjambre.vault.configured_providers', return_value=['openrouter']):
            e = onboarding.escalones()
            self.assertTrue(all(x['listo'] for x in e))
            self.assertTrue(onboarding.completa())

    def test_chispa_apagada_apaga_el_escalon_0(self):
        from . import onboarding
        Participante.objects.filter(key='chispa').update(activo=False)
        with mock.patch('enjambre.conexiones.resolver_bin', return_value=None), \
                mock.patch('enjambre.conexiones.detectar', return_value={}), \
                mock.patch('enjambre.vault.configured_providers', return_value=[]):
            self.assertFalse(onboarding.escalones()[0]['listo'])
