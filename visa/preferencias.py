"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Preferências de cada usuário: os valores iniciais e a migração das que já estão salvas.

Sem NiceGUI: quem guarda as preferências no navegador é interface.py (app.storage.user);
aqui só se decide o que elas contêm. Ver DECISOES 5.1.
"""
import copy

import animacao
import config
import espaco
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
    'radares': [r.codigo for r in radar.RADARES],
    'radar_produto': 'maxcappi',
    'radar_alcance': '400km',
    'radar_altura': '3',
    'dbz_min': 0,
    'radar_circulos': True,
    'radar_status_filtro': dict(radar.STATUS_FILTRO_PADRAO),
    'vento_ligado': False,
    'vento_fl': 340,
    'vento_horario': 'recente',
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
    'wifs': {c.chave: False for c in wifs.CAMADAS},
    'espaco': {c.chave: c.chave == 'tma' for c in espaco.CAMADAS},          # camada ligada?
    'estilos': {c.chave: dict(espaco.ESTILO_PADRAO) for c in espaco.CAMADAS if c.estilizavel},
    'animacao_janela_min': animacao.JANELA_PADRAO,
    'terminal': 'SBGR',
    'legenda_aberta': True,
    'fabia_baixa': False,            # FAB.ia: mostrar os conflitos de confiança baixa (o registro guarda todos)
}


def completar(prefs: dict) -> dict:
    """Acrescenta às preferências salvas o que faltar (chaves novas de versões novas) e corrige
    valores que deixaram de existir. Altera e devolve o mesmo dicionário."""
    for chave, valor in PADRAO.items():
        prefs.setdefault(chave, copy.deepcopy(valor))   # cópia: o PADRAO nunca é alterado
        if isinstance(valor, dict):  # chaves novas dentro de grupos já salvos
            for sub, padrao in valor.items():
                prefs[chave].setdefault(sub, copy.deepcopy(padrao))
    # Valores salvos que deixaram de existir (ex.: nível de vento removido) voltam ao padrão
    if prefs['vento_fl'] not in config.VENTO_FL_PARA_PA:
        prefs['vento_fl'] = PADRAO['vento_fl']
    return prefs
