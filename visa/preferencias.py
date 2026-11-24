"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Preferências de cada usuário: os valores iniciais e a migração das que já estão salvas.

Sem NiceGUI: quem guarda as preferências no navegador é interface.py (app.storage.user);
aqui só se decide o que elas contêm. Ver DECISOES 5.1.

Os nomes das chaves de `PADRAO` estão gravados nos navegadores dos usuários: renomear uma
chave faz cada usuário perder o valor salvo dela.
"""
import copy

import animacao
import config
import espaco
import modelo
import radar
import wifs

# Preferências iniciais de cada usuário (depois ficam salvas por navegador)
PADRAO = {
    'satelite_ligado': True,
    'satelite_tipo': 'realcada',
    'satelite_opacidade': 0.6,
    'radar_ligado': True,
    'radar_fonte': 'api',            # 'api' (REDEMET, sem VPN) ou 'geoserver' (precisa da VPN)
    'radar_produto_api': 'maxcappi',
    'radares': [estacao.codigo for estacao in radar.RADARES],
    'radar_produto': 'maxcappi',
    'radar_alcance': '400km',
    'radar_altura': '3',
    'dbz_min': 0,
    'radar_circulos': True,
    'radar_status_filtro': dict(radar.STATUS_FILTRO_PADRAO),
    'vento_ligado': False,
    'vento_fl': 340,
    'vento_horario': 'recente',
    'modelo_ligado': False,
    'modelo_campo': 'refletividade',  # chave de modelo.POR_CHAVE
    'modelo_fl': 340,                # nível de voo dos campos por nível (os mesmos do vento)
    'modelo_opacidade': 0.7,
    'modelo_vento': False,           # vento em partículas no nível escolhido
    'aeronaves_ligado': True,
    'aeronaves_prefixos': '',
    'aeronaves_fl': {'min': 0, 'max': 600},
    'aeronaves_rotulos': False,
    'aerodromos_ligado': True,
    'raios_ligado': True,
    'raios_fonte': 'api',            # 'api' (REDEMET, sem VPN) ou 'geoserver' (precisa da VPN)
    'raios_classificacao': 'tempo',
    'raios_raio_km': 10,
    'severo_ligado': True,
    'severo_estilo': {'cor': '#6366f1', 'opacidade': 0.6, 'raio_km': 5},
    'wifs_fl': wifs.NIVEL_PADRAO,
    'wifs': {camada.chave: False for camada in wifs.CAMADAS},
    'espaco': {camada.chave: camada.chave == 'tma' for camada in espaco.CAMADAS},   # camada ligada?
    'estilos': {camada.chave: dict(espaco.ESTILO_PADRAO) for camada in espaco.CAMADAS if camada.estilizavel},
    'animacao_janela_min': animacao.JANELA_PADRAO,
    'terminal': 'SBGR',
    'legenda_aberta': True,
    'fabia_baixa': False,            # FAB.ia: mostrar os conflitos de confiança baixa (o registro guarda todos)
}


def completar(prefs: dict) -> dict:
    """Atualiza preferências salvas por uma versão antiga para a versão atual.

    Fluxo:
        1. acrescenta as chaves de `PADRAO` que faltarem, inclusive dentro de grupos (dicts);
        2. devolve ao padrão os valores que deixaram de existir (ex.: nível de vento removido).

    Parâmetros:
        prefs: preferências salvas de um usuário (alterado no lugar).

    Retorna:
        O mesmo dicionário `prefs`, já completo.
    """
    for chave, valor_padrao in PADRAO.items():
        prefs.setdefault(chave, copy.deepcopy(valor_padrao))   # cópia: o PADRAO nunca é alterado
        if isinstance(valor_padrao, dict):  # chaves novas dentro de grupos já salvos
            for subchave, subvalor_padrao in valor_padrao.items():
                prefs[chave].setdefault(subchave, copy.deepcopy(subvalor_padrao))
    if prefs['vento_fl'] not in config.VENTO_FL_PARA_PA:
        prefs['vento_fl'] = PADRAO['vento_fl']
    if prefs['modelo_fl'] not in config.VENTO_FL_PARA_PA:
        prefs['modelo_fl'] = PADRAO['modelo_fl']
    if prefs['modelo_campo'] not in modelo.POR_CHAVE:
        prefs['modelo_campo'] = PADRAO['modelo_campo']
    return prefs
