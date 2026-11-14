"""AreaDoMapa: o mapa Leaflet, o fundo apagado e os lugares da legenda e da linha do tempo."""
from nicegui import ui

import config

ALTURA_CABECALHO = 48   # px; o mapa ocupa a altura da janela menos o cabeçalho

# Arquivos carregados junto com o Leaflet (o JS próprio e o plugin de vento)
RECURSOS = ['/static/vendor/leaflet-velocity.css',
            '/static/vendor/leaflet-velocity.min.js',
            '/static/aeronaves.js',
            '/static/camadas.css',
            '/static/camadas.js']


class AreaDoMapa:
    """Cria o mapa primeiro: as camadas precisam dele.

    `doca_legenda` e `barra_tempo` são só os lugares (flutuando sobre o mapa); quem os
    preenche são a Legenda e a LinhaDoTempo. `area` recebe a classe CSS `sonda-ligada`
    (cursor em cruz) quando a sonda está ligada.
    """

    def __init__(self) -> None:
        with ui.element('div').classes('relative w-full') \
                .style(f'height: calc(100vh - {ALTURA_CABECALHO}px)') as self.area:
            self.mapa = ui.leaflet(
                center=config.MAPA_CENTRO, zoom=config.MAPA_ZOOM,
                options={'zoomSnap': 0.5, 'zoomDelta': 0.5, 'attributionControl': True},
                additional_resources=RECURSOS,
            ).style('height: 100%; width: 100%')
            self.mapa.clear_layers()  # tira o OpenStreetMap padrão
            self.mapa.tile_layer(url_template=config.MAPA_FUNDO_URL,   # fundo apagado: as cores do tempo dominam
                                 options={'attribution': config.MAPA_FUNDO_ATRIBUICAO, 'maxZoom': 16,
                                          'className': 'mapa-fundo'})
            self.doca_legenda = ui.element('div').classes('legenda-doca')
            self.barra_tempo = ui.row().classes('linha-tempo items-center no-wrap gap-1')
