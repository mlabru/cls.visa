"""
Legenda: uma só, flutuando sobre o mapa, com as camadas meteorológicas ligadas.

Ela depende de ~25 valores espalhados (preferências, a previsão de tempo severo escolhida,
o horário da imagem de satélite, a animação). Em vez de avisar a legenda em cada lugar que
muda um deles, um relógio monta a `assinatura()` a cada 0,5 s e só redesenha se ela mudou.
REGRA: algo novo na legenda precisa entrar também em `assinatura()` (DECISOES.md, 5.3).
"""
from nicegui import ui

import aerodromos
import camadas
import radar
import raios
import redemet
import severo
import wifs
from tela.mapa import AreaDoMapa
from tela.pecas import barra_dbz, bolinhas, imagem_legenda

# Como ler cada imagem de satélite
LEITURA_SATELITE = {
    'realcada': 'cinza = superfície e nuvens baixas; cores = topos frios de nuvens altas '
                '(vermelho e rosa são os mais frios: convecção profunda)',
    'ir': 'mais claro = mais frio (nuvens altas)',
    'vis': 'luz do sol refletida: só durante o dia',
}

# Legenda do estilo "intensidade" dos raios: só o GeoServer a conhece, então vem dele (pelo proxy)
LEGENDA_RAIOS_INTENSIDADE = ('/radar/wms?REQUEST=GetLegendGraphic&VERSION=1.1.1&FORMAT=image/png'
                             '&WIDTH=16&HEIGHT=16&LAYER=redemet:lightning_data'
                             '&STYLE=redemet:lightning_by_intensity'
                             '&LEGEND_OPTIONS=fontColor:0xcbd5e1;bgColor:0x111827;fontAntiAliasing:true')


def _item(titulo: str, subtitulo: str = '') -> None:
    with ui.row().classes('items-baseline gap-1 q-mt-xs no-wrap'):
        ui.label(titulo).classes('legenda-titulo')
        if subtitulo:
            ui.label(subtitulo).classes('legenda-texto')


class Legenda:

    INTERVALO_S = 0.5

    def __init__(self, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        self.doca, self.prefs, self.c = area.doca_legenda, prefs, camadas_
        self._mostrada: tuple = ()
        ui.timer(self.INTERVALO_S, self.desenhar)

    def assinatura(self) -> tuple:
        """Tudo o que, se mudar, muda o desenho da legenda."""
        p, e, imagem = self.prefs, self.c.severo.escolha, self.c.satelite.imagem
        return (p['legenda_aberta'], self.c.animacao.ativa, p['satelite_ligado'], p['satelite_tipo'],
                imagem.quando if imagem else None, p['radar_fonte'], p['radar_produto_api'],
                p['radar_ligado'], p['dbz_min'], p['radar_produto'], p['raios_fonte'],
                p['radar_alcance'], p['radar_altura'], p['raios_ligado'],
                p['raios_classificacao'], p['severo_ligado'], e['origem'], e['tipo'], e['validade'],
                p['severo_estilo']['cor'], tuple(p['wifs'].items()), p['wifs_fl'],
                p['vento_ligado'], p['vento_fl'], p['aerodromos_ligado'])

    def desenhar(self) -> None:
        assinatura = self.assinatura()
        if assinatura == self._mostrada:
            return
        self._mostrada = assinatura
        p = self.prefs
        self.doca.clear()
        with self.doca:
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Legenda').classes('legenda-titulo col')
                ui.button(icon='expand_more' if p['legenda_aberta'] else 'expand_less',
                          on_click=lambda: p.update(legenda_aberta=not p['legenda_aberta'])) \
                    .props('flat dense round size=xs color=grey-5')
            if not p['legenda_aberta']:
                return
            # Cada função desenha a sua parte e diz se desenhou algo
            partes = [self._satelite(), self._radar(), self._raios(), self._severo(), self._wifs(),
                      self._vento(), self._aerodromos()]
            if not any(partes):
                ui.label('Nenhuma camada meteorológica ligada').classes('legenda-texto')

    def _satelite(self) -> bool:
        p = self.prefs
        if not p['satelite_ligado']:
            return False
        imagem = self.c.satelite.imagem
        if self.c.animacao.ativa:      # o horário de cada quadro está na linha do tempo
            quando = ' · animação'
        else:
            quando = f' · {imagem.quando:%H:%MZ}' if imagem and imagem.quando else ''
        _item('Satélite', redemet.SATELITES[p['satelite_tipo']].lower() + quando)
        ui.label(LEITURA_SATELITE[p['satelite_tipo']]).classes('legenda-texto')
        return True

    def _radar(self) -> bool:
        p = self.prefs
        if not p['radar_ligado']:
            return False
        if self.c.radar.pela_api():
            rotulo, raio_km = redemet.PRODUTOS_RADAR[p['radar_produto_api']]
            detalhe = f'{rotulo} {raio_km} km · API'
        else:
            detalhe = f"{radar.PRODUTOS[p['radar_produto']]} {radar.ALCANCES[p['radar_alcance']]}"
            if p['radar_produto'] == 'cappi':
                detalhe += f" · {radar.ALTURAS_CAPPI[p['radar_altura']]}"
            detalhe += ' · GeoServer'
        _item('Radar', f'{detalhe} · dBZ')
        barra_dbz(p['dbz_min'])
        return True

    def _raios(self) -> bool:
        p = self.prefs
        if not p['raios_ligado']:
            return False
        if self.c.raios.pela_api():
            _item('Raios', 'células de 0,1° com descargas · API')
            bolinhas([(cor, texto) for _, cor, texto in redemet.RAIOS_CLASSES])
        else:
            _item('Raios', raios.CLASSIFICACOES[p['raios_classificacao']].lower())
            if p['raios_classificacao'] == 'tempo':
                bolinhas(raios.LEGENDA_TEMPO, linha=True)
            else:
                imagem_legenda(LEGENDA_RAIOS_INTENSIDADE)
        return True

    def _severo(self) -> bool:
        p = self.prefs
        if not p['severo_ligado']:
            return False
        e = self.c.severo.escolha
        texto = f"{(e['origem'] or '').upper()} · válido {severo.rotulo(e['validade'])}" if e['validade'] \
            else 'carregando…'
        _item('Tempo severo', texto)
        bolinhas([(p['severo_estilo']['cor'], 'pontos de risco' if e['tipo'] == 'ponto' else 'áreas de risco'),
                  ('#e2e8f0', 'tracejado: área coberta pelo modelo')])
        return True

    def _wifs(self) -> bool:
        p = self.prefs
        ligadas = [camada for camada in wifs.CAMADAS if p['wifs'][camada.chave]]
        for camada in ligadas:
            _item(camada.titulo, f"WIFS · FL{p['wifs_fl']:03d}")
            imagem_legenda(wifs.url_legenda(camada.chave))
        return bool(ligadas)

    def _vento(self) -> bool:
        p = self.prefs
        if p['vento_ligado']:
            _item('Vento', f"FL{p['vento_fl']:03d} · partículas, valor sob o cursor")
        return p['vento_ligado']

    def _aerodromos(self) -> bool:
        if not self.prefs['aerodromos_ligado']:
            return False
        _item('Aeródromos', 'condição pelo METAR')
        bolinhas(aerodromos.LEGENDA, linha=True)
        return True
