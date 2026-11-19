"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

WIFS — previsão em altitude: camadas WMS do GeoServer da REDEMET, por nível de voo.

As imagens passam pelo proxy /radar/wms (o GeoServer é interno). O vento do WIFS
fica na seção "Vento em altitude" (partículas, a partir dos arquivos JSON).

Usado pela camada WIFS (`camadas/prevista.py`), pelas preferências, pelos modos e pela legenda.
"""
from dataclasses import dataclass

RECARREGA_S = 600   # TIME=current: a cada 10 min o mapa pede os tiles de novo (previsão vigente)


@dataclass(frozen=True)
class CamadaWifs:
    """Uma camada WIFS do GeoServer.

    Atributos:
        chave: nome da camada no GeoServer (redemet:<chave>); também a chave nas preferências.
        titulo: nome no painel.
        estilo: SLD usado pelo original.
        icone: ícone do Material (painel).
    """
    chave: str
    titulo: str
    estilo: str
    icone: str


CAMADAS = [
    CamadaWifs('temperatura', 'Temperatura', 'temperatura_novo', 'thermostat'),
    CamadaWifs('turbulencia', 'Turbulência', 'turbulencia', 'waves'),
    CamadaWifs('umidade', 'Umidade relativa', 'umidade_relativa', 'water_drop'),
    CamadaWifs('gelo_severo', 'Gelo severo', 'gelo_wifs', 'ac_unit'),
    CamadaWifs('cb_extensao', 'Extensão CB', 'cb_extensao', 'cloud'),
]
POR_CHAVE = {camada.chave: camada for camada in CAMADAS}

# Nível de voo -> rótulo (altitude aproximada, como no original)
NIVEIS = {
    50: 'FL050 (~1.500 m)', 100: 'FL100 (~3.000 m)', 140: 'FL140 (~4.300 m)',
    180: 'FL180 (~5.500 m)', 240: 'FL240 (~7.300 m)', 300: 'FL300 (~9.100 m)',
    340: 'FL340 (~10.400 m)', 390: 'FL390 (~11.900 m)', 450: 'FL450 (~13.700 m)',
    530: 'FL530 (~16.200 m)',
}
NIVEL_PADRAO = 240
OPACIDADE = 0.6


def opcoes_wms(chave: str, nivel: int) -> dict:
    """Monta as opções da camada WMS do Leaflet.

    O que não é opção do Leaflet (TIME, ELEVATION) vai como parâmetro do WMS.

    Parâmetros:
        chave: chave da camada (de CAMADAS).
        nivel: nível de voo (FL, chave de NIVEIS).

    Retorna:
        Dict de opções para o `L.tileLayer.wms`.
    """
    camada = POR_CHAVE[chave]
    return {
        'layers': f'redemet:{camada.chave}', 'styles': camada.estilo,
        'format': 'image/png', 'transparent': True, 'version': '1.1.1',
        'TIME': 'current', 'ELEVATION': str(nivel),
        'opacity': OPACIDADE, 'zIndex': 2, 'attribution': 'REDEMET / WIFS',
    }


def url_legenda(chave: str) -> str:
    """Monta a URL da imagem de legenda da camada (GetLegendGraphic, pelo proxy /radar/wms).

    Parâmetros:
        chave: chave da camada (de CAMADAS).

    Retorna:
        URL relativa ao nosso servidor.
    """
    camada = POR_CHAVE[chave]
    return ('/radar/wms?REQUEST=GetLegendGraphic&VERSION=1.1.1&FORMAT=image/png&WIDTH=20&HEIGHT=20'
            f'&LAYER=redemet:{camada.chave}&STYLE=redemet:{camada.estilo}'
            '&LEGEND_OPTIONS=layout:horizontal;fontColor:0xcbd5e1;bgColor:0x1d1d1d;fontAntiAliasing:true')
