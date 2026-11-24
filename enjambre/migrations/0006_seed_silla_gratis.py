"""Siembra CHISPA — la silla gratis del escalón 0 (Pollinations, tier anónimo, SIN key).

Es la primera impresión del producto: al primer arranque tiene que haber una silla charlando
sin que el usuario configure nada. Va por el proveedor api-pollinations (sin credencial;
1 request cada 15 s, un modelo chico — el techo honesto del tier anónimo).

ACTIVA solo si no hay ninguna otra silla activa: en una instalación nueva se cumple (el seed
0002 deja las dos apagadas) y el escalón 0 anda de una; en una instalación ya configurada se
siembra APAGADA y no se mete en las mesas de nadie.

Reversible: borra solo esta key.
"""
from django.db import migrations

CHISPA = {
    'key': 'chispa',
    'nombre': 'Chispa',
    'comando': ['api-pollinations', '--model', 'openai-fast'],
    'rol': 'trabajador',
    'orden': 2,
    'color_ui': '#f59e0b',  # ámbar: la chispa
    'persona': (
        "Sos Chispa, la silla gratis del Enjambre: funcionás sin cuentas ni API keys. "
        "Directa y útil, máximo 3 oraciones. Sos un modelo chico con ritmo pausado — "
        "si la tarea te queda grande, decilo sin vueltas y sugerí sumar sillas desde Conexiones."
    ),
    'recordatorio': "Breve. Si no llegás, decilo.",
    'especialidad': "responder al toque, sin configurar nada",
}


def sembrar(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    activa = not Participante.objects.filter(activo=True).exists()
    # get_or_create: no pisar si el usuario ya la editó/renombró.
    Participante.objects.get_or_create(key=CHISPA['key'], defaults={
        **CHISPA, 'activo': activa, 'permitir_consulta': True,
    })


def revertir(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    Participante.objects.filter(key=CHISPA['key']).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('enjambre', '0005_accion'),
    ]
    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
