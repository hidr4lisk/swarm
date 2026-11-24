"""Regresiones Codex: el parser real se ejercita sin login ni llamadas al modelo."""
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.db import connection
from django.test import TestCase, SimpleTestCase

from .clientes import build_comando
from .conexiones import detectar, resolver_bin, ruta_creds
from .engine import ejecutar_cli
from .models import Participante
from .tests import proceso_falso


class CodexExecTests(SimpleTestCase):
    def setUp(self):
        self.silla = Participante(key='codex', nombre='Codex',
                                  comando=build_comando('codex', '')[0])

    def test_prompt_largo_por_stdin_y_sin_interpolar_flags(self):
        prompt = '--help\n' + 'á' * 40000
        proc = proceso_falso(stdout='Listo')
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc) as popen:
            self.assertEqual(ejecutar_cli(self.silla, prompt, 5), ('Listo', False))
        self.assertEqual(popen.call_args.args[0][-1], '-')
        self.assertNotIn(prompt, popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        proc.communicate.assert_called_once_with(input=prompt, timeout=5)

    def test_stderr_de_progreso_sin_respuesta_no_es_un_aporte(self):
        proc = proceso_falso(stderr='Codex\nMCP startup complete\n' + 'progreso\n' * 50)
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            self.assertEqual(ejecutar_cli(self.silla, 'hola', 5), ('(sin respuesta)', True))

    def test_salida_final_no_incluye_progreso(self):
        proc = proceso_falso(stdout='Respuesta final', stderr='MCP startup complete')
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            self.assertEqual(ejecutar_cli(self.silla, 'hola', 5), ('Respuesta final', False))

    def test_fabricacion_degradada_a_read_only_no_es_un_exito(self):
        self.silla.comando = build_comando('codex', '')[1]
        proc = proceso_falso(stdout='No pude crear smoke.txt: el entorno solo permite lectura.',
                             stderr='OpenAI Codex v0.162.1\nsandbox: read-only\n')
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            salida, ruido = ejecutar_cli(self.silla, 'creá smoke.txt', 5)
        self.assertTrue(ruido)
        self.assertIn('[windows].sandbox', salida)

    def test_charla_read_only_sigue_siendo_una_respuesta_valida(self):
        proc = proceso_falso(stdout='Respuesta final', stderr='sandbox: read-only\n')
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            self.assertEqual(ejecutar_cli(self.silla, 'hola', 5), ('Respuesta final', False))

    def test_read_only_citado_en_el_prompt_no_pisa_el_header_real(self):
        self.silla.comando = build_comando('codex', '')[1]
        proc = proceso_falso(stdout='Listo',
                             stderr='sandbox: workspace-write [workdir]\nuser\n'
                                    'Explicá este log:\nsandbox: read-only\n')
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            self.assertEqual(ejecutar_cli(self.silla, 'creá el informe', 5), ('Listo', False))

    def test_error_real_con_texto_largo_y_stdout_parcial_es_ruido(self):
        # Sin mock: el exit status debe prevalecer sobre un stdout que parece respuesta.
        self.silla.comando = [sys.executable, '-c',
                              'import sys; print("respuesta parcial"); '
                              'print("falló el proceso " * 40, file=sys.stderr); sys.exit(2)']
        with mock.patch('enjambre.engine._dir_efimero', return_value=None):
            salida, ruido = ejecutar_cli(self.silla, 'hola', 5)
        self.assertTrue(ruido)
        self.assertIn('código 2', salida)
        self.assertIn('falló el proceso', salida)

    def test_timeout_codex_drena_sin_reenviar_el_prompt(self):
        proc = proceso_falso(timeout=True)
        with mock.patch('enjambre.engine._dir_efimero', return_value=None), \
                mock.patch('enjambre.engine._matar_arbol') as matar, \
                mock.patch('enjambre.engine.subprocess.Popen', return_value=proc):
            salida, ruido = ejecutar_cli(self.silla, 'hola', 5)
        self.assertTrue(ruido)
        self.assertIn('timeout', salida)
        matar.assert_called_once()
        self.assertEqual(proc.communicate.call_args_list,
                         [mock.call(input='hola', timeout=5), mock.call()])

    def test_plantillas_aceptadas_por_el_codex_instalado(self):
        binario = resolver_bin('codex')
        if not binario:
            self.skipTest('Codex CLI no instalado (prueba sin inferencia)')
        for cmd in build_comando('codex', ''):
            result = subprocess.run([binario] + cmd[1:] + ['--help'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_detectar_respeta_codex_home_sin_leer_credenciales(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'auth.json').touch()
            with mock.patch.dict(os.environ, {'CODEX_HOME': d, 'SWARM_CODEX_CREDS': ''}), \
                    mock.patch.object(Path, 'read_text', side_effect=AssertionError('no leer')):
                self.assertEqual(ruta_creds('codex'), str(Path(d, 'auth.json')))
                self.assertTrue(detectar()['codex'])

    def test_override_de_credenciales_tiene_prioridad(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': '/otro',
                                         'SWARM_CODEX_CREDS': '/explicito/auth.json'}):
            self.assertEqual(ruta_creds('codex'), '/explicito/auth.json')


class CodexUpgradeTests(TestCase):
    def test_actualiza_sillas_guardadas_sin_tocar_personalizadas_ni_otros_clientes(self):
        chat = ['codex', 'exec', '--skip-git-repo-check', '--sandbox', 'read-only',
                '--color', 'never', '--model', 'mi-modelo']
        trabajo = ['codex', 'exec', '--skip-git-repo-check', '--full-auto', '--color', 'never',
                   '--model', 'mi-modelo']
        silla = Participante.objects.create(key='codex-viejo', nombre='Codex',
                                            comando=chat, comando_trabajo=trabajo)
        custom = Participante.objects.create(key='codex-custom', nombre='Custom',
                                             comando=['codex', 'exec', '--json'])
        otra = Participante.objects.get(key='claude')
        original = list(otra.comando)
        migration = importlib.import_module('enjambre.migrations.0011_codex_sandbox_explicito')
        for _ in range(2):  # idempotente
            migration.actualizar_codex(apps, SimpleNamespace(connection=connection))
        silla.refresh_from_db()
        self.assertEqual((silla.comando, silla.comando_trabajo),
                         build_comando('codex', 'mi-modelo'))
        custom.refresh_from_db()
        self.assertEqual(custom.comando, ['codex', 'exec', '--json'])
        otra.refresh_from_db()
        self.assertEqual(otra.comando, original)
