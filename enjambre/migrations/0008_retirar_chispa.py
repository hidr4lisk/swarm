"""Retira CHISPA — la silla gratis del escalón 0 (Pollinations, tier anónimo).

El tier anónimo sin key de Pollinations murió el 2026-07-20 (HTTP 402 "budget too low": pasó a
créditos «pollen»). Chispa quedó sentada tirando 402 en cada mesa. Pollinations sigue existiendo,
pero como proveedor por API key más (token de enter.pollinations.ai) — ver providers/pollinations.py
y clientes.py. La escalera de arranque pasó de 3 a 2 escalones (opencode / API key).

Borra la silla SOLO si sigue siendo el seed original (key='chispa' y comando por api-pollinations):
si el usuario la renombró/reconfiguró hacia otro proveedor, no es «Chispa la gratis» y no se toca.
One-way a propósito: revertir no la re-siembra (ya no tiene sentido resucitar el tier muerto).
"""
from django.db import migrations


def retirar(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    for p in Participante.objects.filter(key='chispa'):
        # Solo el seed intacto: comando que sigue apuntando a api-pollinations.
        if p.comando and p.comando[0] == 'api-pollinations':
            p.delete()


def noop(apps, schema_editor):
    # No se re-siembra: el tier anónimo que hacía útil a Chispa ya no existe.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('enjambre', '0007_avatar_chispa'),
    ]
    operations = [
        migrations.RunPython(retirar, noop),
    ]
