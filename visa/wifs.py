"""
WIFS — previsão em altitude: camadas WMS do GeoServer da REDEMET, por nível de voo.

As imagens passam pelo proxy /radar/wms (o GeoServer é interno). O vento do WIFS
fica na seção "Vento em altitude" (partículas, a partir dos arquivos JSON).
"""
from dataclasses import dataclass

RECARREGA_S = 600   # TIME=current: a cada 10 min o mapa pede os tiles de novo (previsão vigente)


@dataclass(frozen=True)
class CamadaWifs:
    chave: str       # nome da camada no GeoServer (redemet:<chave>)
    titulo: str
    estilo: str      # SLD usado pelo original
    icone: str       # ícone do Material (painel)


CAMADAS = [
    CamadaWifs('temperatura', 'Temperatura', 'temperatura_novo', 'thermostat'),
    CamadaWifs('turbulencia', 'Turbulência', 'turbulencia', 'waves'),
    CamadaWifs('umidade', 'Umidade relativa', 'umidade_relativa', 'water_drop'),
    CamadaWifs('gelo_severo', 'Gelo severo', 'gelo_wifs', 'ac_unit'),
    CamadaWifs('cb_extensao', 'Extensão CB', 'cb_extensao', 'cloud'),
]
POR_CHAVE = {c.chave: c for c in CAMADAS}

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
    """Opções da camada Leaflet (o que não é do Leaflet vai como parâmetro do WMS)."""
    camada = POR_CHAVE[chave]
    return {
        'layers': f'redemet:{camada.chave}', 'styles': camada.estilo,
        'format': 'image/png', 'transparent': True, 'version': '1.1.1',
        'TIME': 'current', 'ELEVATION': str(nivel),
        'opacity': OPACIDADE, 'zIndex': 2, 'attribution': 'REDEMET / WIFS',
    }


def url_legenda(chave: str) -> str:
    camada = POR_CHAVE[chave]
    return ('/radar/wms?REQUEST=GetLegendGraphic&VERSION=1.1.1&FORMAT=image/png&WIDTH=20&HEIGHT=20'
            f'&LAYER=redemet:{camada.chave}&STYLE=redemet:{camada.estilo}'
            '&LEGEND_OPTIONS=layout:horizontal;fontColor:0xcbd5e1;bgColor:0x1d1d1d;fontAntiAliasing:true')
