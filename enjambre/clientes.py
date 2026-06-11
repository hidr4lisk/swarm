CLIENTES = {
    # ...
    'codex': {
        'label': 'OpenAI Codex',
        'comando': ['codex', 'exec'],
        'comando_trabajo': ['codex', 'exec'],
        'model_flag': None,
        'modelos': [
            'code-davinci-002',
            'code-cushman-001',
        ],
    },
    # ...
}

def build_comando(cliente, modelo):
    """Devuelve (comando, comando_trabajo) para un cliente CLI + modelo opcional.
    Para clientes HTTP (ollama) devuelve ([], []) — esos van por endpoint_url/endpoint_model."""
    c = CLIENTES.get(cliente)
    if not c or c.get('http'):
        return [], []
    cmd = list(c['comando'])
    cmdt = list(c['comando_trabajo'])
    if c.get('model_flag') and modelo:
        flag = [c['model_flag'], modelo]
        if cliente == 'agy':
            # agy: el prompt se agrega después del -p final, así que el --model va ANTES del -p
            # (insertarlo después rompería: «-p --model X <prompt>» comería el flag como prompt)
            cmd.insert(-1, flag[0])
            cmd.insert(-1, flag[1])
            cmdt.insert(-1, flag[0])
            cmdt.insert(-1, flag[1])
        else:
            cmd.extend(flag)
            cmdt.extend(flag)
    return cmd, cmdt