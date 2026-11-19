"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

A animação no mapa: prepara as imagens das últimas horas e troca a opacidade a cada passo.

A linha do tempo (passos de 10 min) é calculada em animacao.py. Quem usa esta classe:
`CamadasDaPagina` (cria e liga o relógio `tique`) e `tela/linha_tempo.py` (botões e controle deslizante).
"""
import logging
from typing import NamedTuple

from nicegui import background_tasks, ui

import animacao
import redemet

from .ajudantes import OPACIDADE_RADAR, OPACIDADE_RAIOS
from .observacao import CamadaRadar, CamadaRaios, CamadaSatelite

log = logging.getLogger(__name__)


class _Preparo(NamedTuple):
    """Resultado da preparação da animação, pronto para virar camadas no mapa.

    Atributos:
        passos: a linha do tempo (um `animacao.Passo` a cada 10 min).
        imagens: url -> (Imagem, dBZ mínimo) de satélite e radar, na ordem em que entram no mapa.
        quadros_raios: url -> QuadroRaios, desenhados por cima do radar.
    """

    passos: list[animacao.Passo]
    imagens: dict[str, tuple]
    quadros_raios: dict[str, redemet.QuadroRaios]


class CamadaAnimacao:
    """Anima radar (fonte API) e satélite nas últimas horas.

    Ao começar, o Python baixa e prepara todas as imagens (ficam no cache do servidor) e
    cria no mapa uma camada por imagem, invisível. Cada passo só muda a opacidade: liga as
    imagens daquele horário e desliga as do anterior. Nada pisca e nada é baixado de novo.
    Enquanto anima, as camadas ao vivo ficam escondidas e paradas (ver DECISOES 4.4).

    Atributos usados pela tela (tela/linha_tempo.py, tela/legenda.py):
        passos, indice: a linha do tempo e o passo mostrado agora.
        ativa: a animação está no mapa; tocando: está andando sozinha; preparando: baixando imagens.
        aviso: texto para a pessoa (erro ou observação), '' quando não há.
        ao_mudar: função que a linha do tempo põe aqui para repintar os controles.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict, camada_radar: CamadaRadar,
                 camada_satelite: CamadaSatelite, camada_raios: CamadaRaios) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.radar = camada_radar
        self.satelite = camada_satelite
        self.raios = camada_raios
        self.passos: list[animacao.Passo] = []
        self.indice = 0
        self.ativa = False
        self.tocando = False
        self.preparando = False
        self.aviso = ''
        self.camadas: dict[str, object] = {}      # url -> camada (invisível até ser a vez dela)
        self.visiveis: dict[str, float] = {}      # url -> opacidade, as que estão na tela
        self.ao_mudar = lambda: None              # a linha do tempo (tela/) atualiza os controles
        self._assinatura: tuple = ()
        self._pausa = 0

    def assinatura(self) -> tuple:
        """Junta as preferências que, se mudarem, invalidam a animação (ela para e volta ao vivo).

        Retorna:
            Tupla com os valores atuais dessas preferências (ver DECISOES 5.3).
        """
        prefs = self.prefs
        return (prefs['satelite_ligado'], prefs['satelite_tipo'], prefs['satelite_opacidade'],
                prefs['radar_ligado'], prefs['radar_fonte'], prefs['radar_produto_api'], tuple(prefs['radares']),
                prefs['dbz_min'], prefs['raios_ligado'], prefs['raios_fonte'], prefs['animacao_janela_min'])

    def _informar(self, aviso: str = '') -> None:
        """Guarda o aviso e pede à linha do tempo que se redesenhe."""
        self.aviso = aviso
        self.ao_mudar()

    # --- Iniciar: baixar, preparar e pôr no mapa ---------------------------------

    async def iniciar(self) -> None:
        """Prepara todas as imagens da janela e começa a tocar.

        Fluxo:
            1. Confere se há alguma camada que anima (satélite, ou radar/raios pela API).
            2. Baixa as listas de quadros e prepara as imagens (`_preparar`).
            3. Se a pessoa mexeu nas camadas enquanto isso, desiste.
            4. Pausa as camadas ao vivo e cria uma camada invisível por imagem.
            5. Mostra o primeiro passo.
        """
        if self.ativa or self.preparando:
            return
        prefs = self.prefs
        com_satelite = prefs['satelite_ligado']
        com_radar = prefs['radar_ligado'] and self.radar.pela_api()
        com_raios = prefs['raios_ligado'] and self.raios.pela_api()
        if not (com_satelite or com_radar or com_raios):
            self._informar('ligue o satélite, o radar ou os raios (fonte API) para animar')
            return

        self.preparando = True
        assinatura_no_inicio = self.assinatura()
        self._informar('buscando os horários…')
        preparo = await self._preparar(com_satelite, com_radar, com_raios)
        if preparo is None:
            return
        if self.assinatura() != assinatura_no_inicio:     # a pessoa mudou as camadas enquanto preparava
            self._informar('as camadas mudaram: tente de novo')
            return

        for camada in (self.radar, self.satelite, self.raios):
            camada.pausar_ao_vivo()
        self._criar_camadas_invisiveis(preparo)
        self.passos = preparo.passos
        self._assinatura = assinatura_no_inicio
        self.ativa = True
        self.tocando = True
        self._pausa = 0
        self.aviso = '; '.join(self._avisos_do_geoserver(com_radar, com_raios))
        self.mostrar(0)

    async def _preparar(self, com_satelite: bool, com_radar: bool, com_raios: bool) -> _Preparo | None:
        """Baixa as listas de quadros, monta a linha do tempo e prepara as imagens no servidor.

        Em qualquer saída, desliga `preparando` (o botão volta a funcionar).

        Parâmetros:
            com_satelite, com_radar, com_raios: quais camadas entram na animação.

        Retorna:
            O preparo pronto, ou None se não houve imagens ou deu erro (o aviso já foi dado).
        """
        try:
            janela_min = self.prefs['animacao_janela_min']
            dbz_min = self.prefs['dbz_min']
            satelites, quadros_radar, quadros_raios = await self._baixar_quadros(
                janela_min, com_satelite, com_radar, com_raios)
            passos = animacao.linha_do_tempo(satelites, quadros_radar, janela_min, quadros_raios)
            if not passos:
                self._informar('a REDEMET não devolveu imagens para animar')
                return None
            imagens = _imagens_de_satelite_e_radar(passos, dbz_min)
            raios_por_url = _quadros_de_raios(passos)
            self._informar(f'preparando {len(imagens) + len(raios_por_url)} imagens…')
            await redemet.preparar(list(imagens.values()))
            for quadro in raios_por_url.values():          # desenhadas aqui mesmo (rápido)
                redemet.imagem_raios(quadro.chave)
        except redemet.Indisponivel as erro:
            self._informar(str(erro))
            return None
        except Exception:
            # Qualquer outra falha (resposta inesperada da API, por exemplo): avisa e registra.
            # Sem isto o erro sumiria no log e o botão ficaria "preparando" para sempre.
            log.exception('animação: falha ao preparar')
            self._informar('falha ao preparar a animação (detalhes no log)')
            return None
        finally:
            self.preparando = False     # em qualquer saída: o botão volta a funcionar
        return _Preparo(passos, imagens, raios_por_url)

    async def _baixar_quadros(self, janela_min: int, com_satelite: bool, com_radar: bool,
                              com_raios: bool) -> tuple[list, list, list]:
        """Pede à REDEMET a lista de quadros de cada camada ligada.

        Parâmetros:
            janela_min: duração da animação, em min (decide quantas imagens de satélite pedir).
            com_satelite, com_radar, com_raios: quais listas pedir (as outras voltam vazias).

        Retorna:
            (imagens de satélite, quadros de radar só com os radares escolhidos, quadros de raios).
        """
        prefs = self.prefs
        satelites = []
        if com_satelite:
            quantas = animacao.quadros_de_satelite(janela_min)
            satelites = await redemet.satelite_quadros(prefs['satelite_tipo'], quantas)
        quadros_radar = []
        if com_radar:
            quadros_radar = await redemet.radar_quadros(prefs['radar_produto_api'])
        quadros_radar = _so_radares_escolhidos(quadros_radar, set(prefs['radares']))
        quadros_raios = []
        if com_raios:
            quadros_raios = await redemet.raios_quadros()
        return satelites, quadros_radar, quadros_raios

    def _criar_camadas_invisiveis(self, preparo: _Preparo) -> None:
        """Cria no mapa uma camada de opacidade 0 para cada imagem da animação.

        O satélite vai no painel de fundo (como a camada ao vivo); os raios por último, por cima do radar.
        """
        with self.mapa:
            for url, (imagem, _) in preparo.imagens.items():
                if imagem.caminho.startswith('satelite/'):   # por baixo, como a camada ao vivo
                    opcoes = {'opacity': 0, 'pane': 'tilePane', 'zIndex': 2, 'interactive': False}
                else:
                    opcoes = {'opacity': 0, 'interactive': False}
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=imagem.limites, options=opcoes)
            for url, quadro in preparo.quadros_raios.items():        # por último: ficam por cima do radar
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=quadro.limites, options={
                    'opacity': 0, 'interactive': False, 'className': 'pixelado'})

    def _avisos_do_geoserver(self, com_radar: bool, com_raios: bool) -> list[str]:
        """Lista as camadas ligadas que ficam de fora porque vêm do GeoServer (só a API anima).

        Parâmetros:
            com_radar, com_raios: se o radar e os raios entraram na animação.

        Retorna:
            Textos como 'o radar do GeoServer não anima'; lista vazia se nada ficou de fora.
        """
        avisos = []
        if self.prefs['radar_ligado'] and not com_radar:
            avisos.append('o radar do GeoServer não anima')
        if self.prefs['raios_ligado'] and not com_raios:
            avisos.append('os raios do GeoServer não anima')
        return avisos

    # --- Tocar: só a opacidade muda -------------------------------------------------

    def _alvo(self, passo: animacao.Passo) -> dict[str, float]:
        """Diz quais imagens devem estar visíveis num passo.

        Parâmetros:
            passo: o passo da linha do tempo.

        Retorna:
            url -> opacidade de cada imagem do passo (satélite, radares, raios).
        """
        alvo = {}
        if passo.satelite:
            alvo[passo.satelite.url()] = self.prefs['satelite_opacidade']
        if passo.radar:
            for imagem in passo.radar.imagens:
                alvo[imagem.url(self.prefs['dbz_min'])] = OPACIDADE_RADAR
        if passo.raios:
            alvo[passo.raios.url()] = OPACIDADE_RAIOS
        return alvo

    def mostrar(self, indice: int) -> None:
        """Liga as imagens do passo `indice` e desliga as que não fazem parte dele.

        Parâmetros:
            indice: posição do passo em `self.passos`.
        """
        alvo = self._alvo(self.passos[indice])
        for url in set(self.visiveis) - set(alvo):
            self.camadas[url].run_method('setOpacity', 0)
        for url, opacidade in alvo.items():
            if url not in self.visiveis:
                self.camadas[url].run_method('setOpacity', opacidade)
        self.visiveis = alvo
        self.indice = indice
        self.ao_mudar()

    def tique(self) -> None:
        """Avança um passo; chamado pelo relógio da página (e para de vez se as camadas mudaram).

        No último passo (o mais recente) fica parado `animacao.PAUSA_NO_FIM` tiques e recomeça.
        """
        if not self.ativa:
            return
        if self.assinatura() != self._assinatura:
            self.parar('as camadas mudaram: de volta ao vivo')
            return
        if not self.tocando:
            return
        if self.indice < len(self.passos) - 1:
            self.mostrar(self.indice + 1)
        elif self._pausa < animacao.PAUSA_NO_FIM:   # fica um pouco no mais recente
            self._pausa += 1
        else:
            self._pausa = 0
            self.mostrar(0)

    def alternar(self) -> None:
        """Botão tocar/pausar (a primeira vez, prepara e começa)."""
        if not self.ativa:
            background_tasks.create(self.iniciar(), name='animacao')
            return
        self.tocando = not self.tocando
        self.ao_mudar()

    def andar(self, passos: int) -> None:
        """Pausa e anda alguns passos (botões ◀ ▶); dá a volta nas pontas.

        Parâmetros:
            passos: quantos passos andar (negativo volta).
        """
        if self.ativa:
            self.tocando = False
            self.mostrar((self.indice + passos) % len(self.passos))

    def ir_para(self, indice: int) -> None:
        """Pausa e mostra o passo escolhido no controle deslizante (índice fora da faixa é ignorado).

        Parâmetros:
            indice: posição do passo em `self.passos`.
        """
        if self.ativa and indice != self.indice and 0 <= indice < len(self.passos):
            self.tocando = False
            self.mostrar(indice)

    def parar(self, aviso: str = '') -> None:
        """Volta ao vivo: tira as imagens da animação e reaparece com as camadas atualizadas.

        Parâmetros:
            aviso: texto a mostrar na linha do tempo ('' para nenhum).
        """
        for camada in self.camadas.values():
            self.mapa.remove_layer(camada)
        self.camadas.clear()
        self.visiveis = {}
        self.passos = []
        self.indice = 0
        self.ativa = False
        self.tocando = False
        for camada in (self.radar, self.satelite, self.raios):
            camada.retomar_ao_vivo()
        self._informar(aviso)


def _so_radares_escolhidos(quadros: list[redemet.QuadroRadar], escolhidos: set[str]) -> list[redemet.QuadroRadar]:
    """Tira de cada quadro de radar as estações que a pessoa não escolheu.

    Parâmetros:
        quadros: quadros de radar da REDEMET (um horário, várias estações cada).
        escolhidos: códigos das estações marcadas no painel.

    Retorna:
        Os mesmos quadros, na mesma ordem, só com as imagens das estações escolhidas.
    """
    filtrados = []
    for quadro in quadros:
        imagens = tuple(imagem for imagem in quadro.imagens if imagem.codigo in escolhidos)
        filtrados.append(redemet.QuadroRadar(quadro.quando, imagens))
    return filtrados


def _imagens_de_satelite_e_radar(passos: list[animacao.Passo], dbz_min: int) -> dict[str, tuple]:
    """Junta, sem repetir, as imagens de satélite e de radar usadas na linha do tempo.

    A ordem importa: satélite antes do radar, para o radar ficar por cima no mapa.

    Parâmetros:
        passos: a linha do tempo.
        dbz_min: limiar de dBZ do radar (entra no endereço da imagem).

    Retorna:
        url -> (Imagem, dBZ mínimo); o satélite leva 0 como dBZ.
    """
    imagens = {}
    for passo in passos:
        if passo.satelite:
            imagens[passo.satelite.url()] = (passo.satelite, 0)
    for passo in passos:
        if passo.radar:
            for imagem in passo.radar.imagens:
                imagens[imagem.url(dbz_min)] = (imagem, dbz_min)
    return imagens


def _quadros_de_raios(passos: list[animacao.Passo]) -> dict[str, redemet.QuadroRaios]:
    """Junta, sem repetir, os quadros de raios usados na linha do tempo.

    Parâmetros:
        passos: a linha do tempo.

    Retorna:
        url -> QuadroRaios.
    """
    quadros = {}
    for passo in passos:
        if passo.raios:
            quadros[passo.raios.url()] = passo.raios
    return quadros
