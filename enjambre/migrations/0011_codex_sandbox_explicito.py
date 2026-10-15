"""Actualizar SOLO las plantillas Codex de v1.2; conservar comandos personalizados."""
from django.db import migrations


def actualizar_codex(apps, schema_editor):
    Participante = apps.get_model('enjambre', 'Participante')
    bases = (
        ['codex', 'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '--color', 'never'],
        ['codex', 'exec', '--skip-git-repo-check', '--full-auto', '--color', 'never'],
    )
    for silla in Participante.objects.using(schema_editor.connection.alias).all():
        cambios = []
        for campo in ('comando', 'comando_trabajo'):
            cmd = getattr(silla, campo)
            for base, sandbox in zip(bases, ('read-only', 'workspace-write')):
                if cmd == base or (isinstance(cmd, list) and cmd[:len(base)] == base
                                   and len(cmd) == len(base) + 2 and cmd[-2] == '--model'):
                    nuevo = ['codex', 'exec', '--skip-git-repo-check', '--sandbox', sandbox,
                             '-c', 'approval_policy="never"', '--color', 'never']
                    setattr(silla, campo, nuevo + cmd[len(base):])
                    cambios.append(campo)
                    break
        if cambios:
            silla.save(using=schema_editor.connection.alias, update_fields=cambios)


class Migration(migrations.Migration):
    dependencies = [('enjambre', '0010_sesion_participantes_vacio_ninguna')]
    operations = [migrations.RunPython(actualizar_codex, migrations.RunPython.noop)]
