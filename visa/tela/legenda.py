"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Legenda: uma só, flutuando sobre o mapa, com as camadas meteorológicas ligadas.
Criada por interface.pagina().

Ela depende de ~25 valores espalhados (preferências, a previsão de tempo severo escolhida,
o horário da imagem de satélite, a animação). Em vez de avisar a legenda em cada lugar que
muda um deles, um relógio monta a `assinatura()` a cada 0,5 s e só redesenha se ela mudou.
REGRA: algo novo na legenda precisa entrar também em `assinatura()` (DECISOES.md, 5.3).
"""
from nicegui import ui

import aerodromos
import camadas
import modelo
import radar
import raios
import redemet
import severo
import wifs
from tela.mapa import AreaDoMapa
from tela.pecas import barra_cores, barra_dbz, bolinhas, imagem_legenda

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
    """Título de uma camada na legenda, com um subtítulo opcional ao lado.

    Parâmetros:
        titulo: nome da camada (Radar, Raios...).
        subtitulo: detalhe (produto, nível, horário); vazio para não mostrar.
    """
    with ui.row().classes('items-baseline gap-1 q-mt-xs no-wrap'):
        ui.label(titulo).classes('legenda-titulo')
        if subtitulo:
            ui.label(subtitulo).classes('legenda-texto')


class Legenda:
    """A legenda flutuante sobre o mapa; redesenha só quando a `assinatura()` muda.

    Atributos:
        doca: o lugar da legenda sobre o mapa (AreaDoMapa.doca_legenda).
        prefs: preferências do usuário.
        camadas: as camadas da página.
    """

    INTERVALO_S = 0.5

    def __init__(self, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        """Guarda onde desenhar e começa o relógio.

        Parâmetros:
            area: a área do mapa (onde fica a doca da legenda).
            prefs: preferências do usuário.
            camadas_: as camadas da página.
        """
        self.doca = area.doca_legenda
        self.prefs = prefs
        self.camadas = camadas_
        self._mostrada: tuple = ()
        ui.timer(self.INTERVALO_S, self.desenhar)

    def assinatura(self) -> tuple:
        """Tudo o que, se mudar, muda o desenho da legenda.

        REGRA: algo novo na legenda entra também aqui (DECISOES.md, 5.3).

        Retorna:
            Uma tupla, comparada com a do último desenho.
        """
        prefs = self.prefs
        escolha = self.camadas.severo.escolha
        imagem = self.camadas.satelite.imagem
        quando_satelite = imagem.quando if imagem else None
        return (prefs['legenda_aberta'], self.camadas.animacao.ativa,
                prefs['satelite_ligado'], prefs['satelite_tipo'], quando_satelite,
                prefs['radar_fonte'], prefs['radar_produto_api'],
                prefs['radar_ligado'], prefs['dbz_min'], prefs['radar_produto'], prefs['raios_fonte'],
                prefs['radar_alcance'], prefs['radar_altura'], prefs['raios_ligado'],
                prefs['raios_classificacao'], prefs['severo_ligado'],
                escolha['origem'], escolha['tipo'], escolha['validade'],
                prefs['severo_estilo']['cor'], tuple(prefs['wifs'].items()), prefs['wifs_fl'],
                prefs['vento_ligado'], prefs['vento_fl'], prefs['aerodromos_ligado'],
                prefs['modelo_ligado'], prefs['modelo_campo'], prefs['modelo_fl'], prefs['modelo_vento'],
                self.camadas.modelo.horario, self.camadas.modelo.rodada)

    def desenhar(self) -> None:
        """Redesenha a legenda (só se a assinatura mudou): título, botão de abrir/recolher e as partes."""
        assinatura = self.assinatura()
        if assinatura == self._mostrada:
            return
        self._mostrada = assinatura
        aberta = self.prefs['legenda_aberta']
        self.doca.clear()
        with self.doca:
            with ui.row().classes('w-full items-center no-wrap'):
                ui.label('Legenda').classes('legenda-titulo col')
                ui.button(icon='expand_more' if aberta else 'expand_less', on_click=self._abrir_ou_recolher) \
                    .props('flat dense round size=xs color=grey-5')
            if not aberta:
                return
            # Cada função desenha a sua parte e diz se desenhou algo
            partes = [self._satelite(), self._radar(), self._raios(), self._severo(), self._wifs(),
                      self._vento(), self._modelo(), self._aerodromos()]
            if not any(partes):
                ui.label('Nenhuma camada meteorológica ligada').classes('legenda-texto')

    def _abrir_ou_recolher(self) -> None:
        """Botão da legenda: abre ou recolhe (o relógio redesenha na próxima volta)."""
        self.prefs['legenda_aberta'] = not self.prefs['legenda_aberta']

    # --- Uma parte por camada: desenha e diz se desenhou algo -------------------------

    def _satelite(self) -> bool:
        """Satélite: o tipo de imagem, o horário e como ler as cores."""
        prefs = self.prefs
        if not prefs['satelite_ligado']:
            return False
        imagem = self.camadas.satelite.imagem
        if self.camadas.animacao.ativa:      # o horário de cada quadro está na linha do tempo
            quando = ' · animação'
        elif imagem and imagem.quando:
            quando = f' · {imagem.quando:%H:%MZ}'
        else:
            quando = ''
        _item('Satélite', redemet.SATELITES[prefs['satelite_tipo']].lower() + quando)
        ui.label(LEITURA_SATELITE[prefs['satelite_tipo']]).classes('legenda-texto')
        return True

    def _radar(self) -> bool:
        """Radar: o produto (API ou GeoServer) e a escala de dBZ."""
        prefs = self.prefs
        if not prefs['radar_ligado']:
            return False
        if self.camadas.radar.pela_api():
            rotulo, raio_km = redemet.PRODUTOS_RADAR[prefs['radar_produto_api']]
            detalhe = f'{rotulo} {raio_km} km · API'
        else:
            detalhe = f"{radar.PRODUTOS[prefs['radar_produto']]} {radar.ALCANCES[prefs['radar_alcance']]}"
            if prefs['radar_produto'] == 'cappi':
                detalhe += f" · {radar.ALTURAS_CAPPI[prefs['radar_altura']]}"
            detalhe += ' · GeoServer'
        _item('Radar', f'{detalhe} · dBZ')
        barra_dbz(prefs['dbz_min'])
        return True

    def _raios(self) -> bool:
        """Raios: as cores das células (API) ou a classificação escolhida (GeoServer)."""
        prefs = self.prefs
        if not prefs['raios_ligado']:
            return False
        if self.camadas.raios.pela_api():
            _item('Raios', 'células de 0,1° com descargas · API')
            bolinhas([(cor, texto) for _, cor, texto in redemet.RAIOS_CLASSES])
        else:
            _item('Raios', raios.CLASSIFICACOES[prefs['raios_classificacao']].lower())
            if prefs['raios_classificacao'] == 'tempo':
                bolinhas(raios.LEGENDA_TEMPO, linha=True)
            else:
                imagem_legenda(LEGENDA_RAIOS_INTENSIDADE)
        return True

    def _severo(self) -> bool:
        """Tempo severo: modelo e validade escolhidos, e a cor dos pontos ou áreas de risco."""
        prefs = self.prefs
        if not prefs['severo_ligado']:
            return False
        escolha = self.camadas.severo.escolha
        if escolha['validade']:
            texto = f"{(escolha['origem'] or '').upper()} · válido {severo.rotulo(escolha['validade'])}"
        else:
            texto = 'carregando…'
        _item('Tempo severo', texto)
        risco = 'pontos de risco' if escolha['tipo'] == 'ponto' else 'áreas de risco'
        bolinhas([(prefs['severo_estilo']['cor'], risco),
                  ('#e2e8f0', 'tracejado: área coberta pelo modelo')])
        return True

    def _wifs(self) -> bool:
        """WIFS: uma entrada (com a imagem de legenda do GeoServer) por variável ligada."""
        prefs = self.prefs
        ligadas = [camada for camada in wifs.CAMADAS if prefs['wifs'][camada.chave]]
        for camada in ligadas:
            _item(camada.titulo, f"WIFS · FL{prefs['wifs_fl']:03d}")
            imagem_legenda(wifs.url_legenda(camada.chave))
        return bool(ligadas)

    def _vento(self) -> bool:
        """Vento: o nível de voo mostrado."""
        prefs = self.prefs
        if not prefs['vento_ligado']:
            return False
        _item('Vento', f"FL{prefs['vento_fl']:03d} · partículas, valor sob o cursor")
        return True

    def _modelo(self) -> bool:
        """Modelo WRF: o campo, o nível, o horário previsto (com a antecedência) e a escala de cores."""
        prefs = self.prefs
        camada = self.camadas.modelo
        if not prefs['modelo_ligado']:
            return False
        campo = camada.campo()
        partes = []
        if campo.por_nivel:
            partes.append(f"FL{prefs['modelo_fl']:03d}")
        if camada.rodada and camada.horario:
            partes.append(modelo.rotulo_horario(camada.horario, camada.rodada.inicio))
        _item(f'WRF · {campo.titulo}', ' · '.join(partes))
        if campo.rotulos:        # índices: faixas com nome (leve, moderada, forte)
            bolinhas([(cor, f'{nome} (≥ {limite:g} {campo.unidade})'.replace('.', ','))
                      for (limite, cor), nome in zip(campo.paleta, campo.rotulos, strict=True)], linha=True)
        else:
            barra_cores(campo.paleta, campo.unidade)
        if campo.experimental:
            ui.label('experimental: limiares não validados').classes('legenda-texto text-amber')
        if campo.chave == 'refletividade':
            ui.label('simulada pelo modelo: não é o radar').classes('legenda-texto')
        if prefs['modelo_vento']:
            ui.label(f"vento em partículas · FL{prefs['modelo_fl']:03d} · valor sob o cursor").classes('legenda-texto')
        return True

    def _aerodromos(self) -> bool:
        """Aeródromos: as cores da condição pelo METAR."""
        if not self.prefs['aerodromos_ligado']:
            return False
        _item('Aeródromos', 'condição pelo METAR')
        bolinhas(aerodromos.LEGENDA, linha=True)
        return True
