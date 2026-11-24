import copy

import preferencias
from preferencias import PADRAO


def test_preferencias_novas_vem_do_padrao_sem_compartilhar_objetos():
    prefs = preferencias.completar({})
    assert prefs == PADRAO
    prefs['wifs']['gelo_severo'] = True                  # mexer nas de um usuário...
    prefs['radares'].clear()
    assert PADRAO['radares'] and not any(PADRAO['wifs'].values())   # ...não muda o padrão


def test_preferencias_salvas_ganham_chaves_novas_e_mantem_as_escolhas():
    antigas = {'radar_ligado': False, 'wifs': {'gelo_severo': True}, 'vento_fl': 999}
    prefs = preferencias.completar(copy.deepcopy(antigas))
    assert prefs['radar_ligado'] is False                       # escolha mantida
    assert prefs['wifs']['gelo_severo'] is True                 # escolha dentro de um grupo mantida
    assert set(prefs['wifs']) == set(PADRAO['wifs'])            # chaves novas do grupo acrescentadas
    assert prefs['vento_fl'] == PADRAO['vento_fl']              # valor que deixou de existir volta ao padrão
    assert set(prefs) == set(PADRAO)


def test_preferencias_do_modelo_invalidas_voltam_ao_padrao():
    prefs = preferencias.completar({'modelo_campo': 'sumiu', 'modelo_fl': 999, 'modelo_ligado': True})
    assert prefs['modelo_campo'] == PADRAO['modelo_campo'] and prefs['modelo_fl'] == PADRAO['modelo_fl']
    assert prefs['modelo_ligado'] is True
