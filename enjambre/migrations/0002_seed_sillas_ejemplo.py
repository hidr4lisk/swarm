"""Siembra 2 sillas de ejemplo, APAGADAS y sin ninguna credencial.

Las sillas son datos, no código: esto es solo para que la mesa no arranque vacía.
Encendelas desde el panel de Sillas cuando la pantalla Conexiones detecte el login
del CLI correspondiente (el login es tuyo, en tu terminal; acá nunca hay keys).

Los comandos están validados con el runner:
- claude charla con `-p --output-format text` y fabrica con `--permission-mode
  acceptEdits` (NO `--dangerously-skip-permissions`: claude lo prohíbe corriendo
  como root, y el contenedor del runner corre como root).
- opencode charla y fabrica con `opencode run` (edita el cwd /work del worktree).

Reversible: borra solo estas 2 keys.
"""
from django.db import migrations

SILLAS = [
    {
        'key': 'claude',
        'nombre': 'Claude Code',
        'comando': ['claude', '-p', '--output-format', 'text'],
        'comando_trabajo': ['claude', '-p', '--permission-mode', 'acceptEdits'],
        'rol': 'lider',
        'orden': 0,
        'persona': (
            "Sos Claude Code, ingeniero senior. Respondé técnico, directo y conciso, "
            "sin relleno. Si detectás un riesgo de seguridad, lo señalás. Sin emojis."
        ),
        'recordatorio': "Conciso y técnico. Sin relleno.",
        'especialidad': "código en general, planificación, revisión",
    },
    {
        'key': 'opencode',
        'nombre': 'OpenCode',
        'comando': ['opencode', 'run'],
        'comando_trabajo': ['opencode', 'run'],
        'rol': 'trabajador',
        'orden': 1,
        'persona': "Sos OpenCode. Directo y breve, máximo 2 oraciones. Sin relleno.",
        'recordatorio': "Breve y directo.",
        'especialidad': "scripts, fabricación de archivos",
    },
]


def sembrar(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    for datos in SILLAS:
        # get_or_create: no pisar si el usuario ya las editó/renombró.
        Participante.objects.get_or_create(key=datos['key'], defaults={
            **datos, 'activo': False, 'permitir_consulta': False,
        })


def revertir(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    Participante.objects.filter(key__in=[s['key'] for s in SILLAS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('enjambre', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
