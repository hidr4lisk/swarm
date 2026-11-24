"""Le pone el retrato de fábrica a CHISPA (la silla gratis del escalón 0).

Chispa es la primera impresión del producto: aparece charlando sin que el usuario configure
nada, y hasta ahora salía con el cuadradito de color. El retrato vive como data-URI JPEG 128px
(mismo formato que produce el recorte del navegador, ~4 KB) en `enjambre/seed_avatars/`.

Solo pisa el avatar si está VACÍO: si el usuario ya le puso el suyo, no se toca.
Reversible: saca el avatar solo si es exactamente el de fábrica.
"""
from pathlib import Path

from django.db import migrations

AVATAR = (Path(__file__).resolve().parent.parent / 'seed_avatars' / 'chispa.txt').read_text().strip()


def poner(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    Participante.objects.filter(key='chispa', avatar='').update(avatar=AVATAR)


def sacar(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    Participante.objects.filter(key='chispa', avatar=AVATAR).update(avatar='')


class Migration(migrations.Migration):
    dependencies = [
        ('enjambre', '0006_seed_silla_gratis'),
    ]
    operations = [
        migrations.RunPython(poner, sacar),
    ]
