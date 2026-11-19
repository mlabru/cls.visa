"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de observação: radar, satélite e raios (o que já aconteceu, medido).

As três podem ser pausadas pela animação (`pausar_ao_vivo` / `retomar_ao_vivo`).
Criadas por `CamadasDaPagina` (camadas/__init__.py); os controles ficam em tela/painel_camadas.py.
Não há classe-base para as três (DECISOES 8.1): o comum vem de ajudantes.py.
"""
from datetime import UTC, datetime

from nicegui import background_tasks, ui

import config
import desenho
import radar
import raios
import redemet

from .ajudantes import OPACIDADE_RADAR, OPACIDADE_RAIOS, UltimoPedido, escrever


class CamadaRadar:
    """Radar de duas fontes: a API da REDEMET (uma imagem por estação, sem VPN) ou o
    GeoServer interno (uma camada WMS, precisa da VPN). Botões, status e círculos são comuns."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camada = None                        # GeoServer: a camada WMS
        self.imagens: dict[str, tuple] = {}       # API: código -> (camada no mapa, Imagem)
        self.erro = ''
        self.congelada = False                    # durante a animação, não troca as imagens
        self._pedido = UltimoPedido()
        self.circulos: list = []                  # camadas L.circle dos círculos de alcance
        self._circulos_desenhados: list = []      # o que está no mapa (evita redesenhar igual)
        self.ultimas: dict[str, datetime] = {}   # código -> horário da última imagem
        self.status_ok = False                    # o GeoServer respondeu à consulta de status?
        self.ao_mudar_status = lambda: None       # o painel (tela/) repinta os botões

    def pela_api(self) -> bool:
        """Diz se a fonte escolhida é a API da REDEMET (e não o GeoServer)."""
        return self.prefs['radar_fonte'] == 'api'

    def raio_km(self) -> int:
        """Alcance do produto de radar escolhido, em km (raio dos círculos de alcance)."""
        if self.pela_api():
            return redemet.PRODUTOS_RADAR[self.prefs['radar_produto_api']][1]
        return int(self.prefs['radar_alcance'].removesuffix('km'))

    def aplicar(self) -> None:
        """Recria a camada com as escolhas atuais (fonte, produto, alcance, radares...)."""
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
        for camada, _ in self.imagens.values():
            self.mapa.remove_layer(camada)
        self.imagens.clear()
        self._pedido.novo()               # descarta respostas da API pedidas antes
        self.desenhar_circulos()
        prefs = self.prefs
        if not prefs['radar_ligado'] or not prefs['radares']:
            return
        if self.pela_api():
            background_tasks.create(self.desenhar_api(), name='radar-api')
            return
        opcoes = {
            'layers': f"redemet:{prefs['radar_produto']}_{prefs['radar_alcance']}",
            'format': 'image/png', 'transparent': True, 'version': '1.1.1',
            'TIME': radar.janela_de_tempo(),
            'CQL_FILTER': radar.filtro_cql(prefs['radares']),
            'dbz_min': prefs['dbz_min'],
            'opacity': OPACIDADE_RADAR, 'zIndex': 3, 'attribution': 'REDEMET',
        }
        if prefs['radar_produto'] == 'cappi':
            opcoes['ELEVATION'] = prefs['radar_altura']
        with self.mapa:
            self.camada = self.mapa.wms_layer(url_template='/radar/wms', options=opcoes)

    async def desenhar_api(self) -> None:
        """Desenha uma imagem por estação selecionada (fonte API); mantém no mapa as que não mudaram.

        Fluxo:
            1. Pede à REDEMET a imagem mais recente de cada estação.
            2. Descarta a resposta se já houve pedido mais novo ou a camada mudou.
            3. Tira do mapa as imagens que mudaram ou saíram; põe as novas.
        """
        if self.congelada:      # a animação está na tela; ao parar, ela chama esta função de novo
            return
        pedido = self._pedido.novo()
        prefs = self.prefs
        try:
            lista = await redemet.radar_api(prefs['radar_produto_api'])
            self.erro = ''
        except redemet.Indisponivel as erro:
            self.erro = str(erro)
            return
        if not self._pedido.vale(pedido) or not prefs['radar_ligado'] or not self.pela_api():
            return
        desejadas = {imagem.codigo: imagem for imagem in lista
                     if imagem.caminho and imagem.codigo in prefs['radares']}
        for codigo in list(self.imagens):
            if self.imagens[codigo][1] != desejadas.get(codigo):
                self.mapa.remove_layer(self.imagens.pop(codigo)[0])
        with self.mapa:
            for codigo, imagem in desejadas.items():
                if codigo not in self.imagens:
                    camada = self.mapa.image_overlay(url=imagem.url(prefs['dbz_min']), bounds=imagem.limites,
                                                     options={'opacity': OPACIDADE_RADAR, 'interactive': False})
                    self.imagens[codigo] = (camada, imagem)

    def mudar_limiar(self) -> None:
        """Aplica o novo dBZ mínimo sem recriar a camada."""
        if self.camada:  # só troca o parâmetro: o Leaflet recarrega os tiles sem piscar
            self.camada.run_method('setParams', {'dbz_min': self.prefs['dbz_min']})
        for camada, imagem in self.imagens.values():
            camada.run_method('setUrl', imagem.url(self.prefs['dbz_min']))

    def pausar_ao_vivo(self) -> None:
        """A animação vai começar: esconde as imagens ao vivo e para de trocá-las."""
        self.congelada = True
        for camada, _ in self.imagens.values():
            camada.run_method('setOpacity', 0)

    def retomar_ao_vivo(self) -> None:
        """A animação parou: mostra as imagens ao vivo de novo e busca as mais novas."""
        self.congelada = False
        for camada, _ in self.imagens.values():
            camada.run_method('setOpacity', OPACIDADE_RADAR)
        if self.pela_api() and self.prefs['radar_ligado']:
            background_tasks.create(self.desenhar_api(), name='radar-api')

    async def atualizar_horario(self) -> None:
        """Chamado pelo relógio: no GeoServer anda a janela de tempo; na API busca as imagens novas."""
        if self.camada:
            self.camada.run_method('setParams', {'TIME': radar.janela_de_tempo()})
        elif self.pela_api() and self.prefs['radar_ligado']:
            await self.desenhar_api()

    # --- Status (horário da última imagem de cada radar) -------------------------

    def status_de(self, codigo: str) -> str:
        """Classifica um radar pelo horário da última imagem.

        Parâmetros:
            codigo: código da estação.

        Retorna:
            Uma chave de `radar.STATUS`.
        """
        return radar.classificar_status(self.ultimas.get(codigo))

    async def consultar_status(self, aplicar_filtro: bool = False) -> None:
        """Atualiza o horário da última imagem de cada radar (API ou WFS do GeoServer).

        Parâmetros:
            aplicar_filtro: se True, seleciona os radares conforme os filtros de status
                (ao abrir e ao trocar de produto).
        """
        await self.mapa.initialized()   # antes disso, o que se manda ao mapa se perde
        ultimas = await self._buscar_ultimas()
        self.status_ok = ultimas is not None
        if ultimas is not None:
            self.ultimas = ultimas
            if aplicar_filtro:
                filtro = self.prefs['radar_status_filtro']
                self.prefs['radares'] = [estacao.codigo for estacao in radar.RADARES
                                         if filtro[self.status_de(estacao.codigo)]]
                self.aplicar()
        self.desenhar_circulos()
        self.ao_mudar_status()

    async def _buscar_ultimas(self) -> dict[str, datetime] | None:
        """Busca, na fonte escolhida, o horário da última imagem de cada radar.

        Retorna:
            código -> horário (UTC); None se a fonte não respondeu.
        """
        prefs = self.prefs
        if not self.pela_api():
            return await radar.buscar_status(prefs['radar_produto'], prefs['radar_alcance'], prefs['radar_altura'])
        try:
            lista = await redemet.radar_api(prefs['radar_produto_api'])
        except redemet.Indisponivel as erro:
            self.erro = str(erro)
            return None
        self.erro = ''
        return {imagem.codigo: imagem.quando for imagem in lista if imagem.quando and imagem.caminho}

    def alternar_filtro(self, status: str) -> None:
        """Liga/desliga um filtro de status: (des)seleciona os radares nesse status.

        Parâmetros:
            status: chave de `radar.STATUS` (o botão clicado).
        """
        prefs = self.prefs
        ligado = not prefs['radar_status_filtro'][status]
        prefs['radar_status_filtro'][status] = ligado
        afetados = {estacao.codigo for estacao in radar.RADARES if self.status_de(estacao.codigo) == status}
        if ligado:
            escolhidos = set(prefs['radares']) | afetados
        else:
            escolhidos = set(prefs['radares']) - afetados
        prefs['radares'] = [estacao.codigo for estacao in radar.RADARES if estacao.codigo in escolhidos]
        self.aplicar()
        self.ao_mudar_status()

    async def mudar_produto(self) -> None:
        """Troca de fonte, produto ou alcance: recria a camada e reaplica os filtros de status."""
        self.aplicar()
        await self.consultar_status(aplicar_filtro=True)

    def desenhar_circulos(self) -> None:
        """Desenha um L.circle por radar selecionado; o clique mostra o horário da última imagem.

        Só mexe no mapa se a lista de círculos mudou desde o último desenho.
        """
        if not self.mapa.is_initialized:   # o balão se perderia; consultar_status() redesenha
            return
        lista = self._circulos_desejados()
        if lista == self._circulos_desenhados:
            return
        for camada in self.circulos:
            self.mapa.remove_layer(camada)
        self.circulos = []
        with self.mapa:
            for centro, raio_m, balao in lista:
                camada = self.mapa.generic_layer(name='circle', args=[centro, {
                    'radius': raio_m, 'color': 'rgba(255, 255, 255, 0.4)', 'weight': 0.8, 'fill': False}])
                camada.run_method('bindPopup', balao)
                self.circulos.append(camada)
        self._circulos_desenhados = lista

    def _circulos_desejados(self) -> list[tuple]:
        """Monta os círculos que deveriam estar no mapa agora.

        Retorna:
            Lista de ([lat, lon], raio em m, HTML do balão); vazia se o radar ou os círculos estão desligados.
        """
        prefs = self.prefs
        if not (prefs['radar_ligado'] and prefs['radar_circulos']):
            return []
        raio_m = self.raio_km() * 1000
        agora = datetime.now(UTC)
        lista = []
        for estacao in radar.RADARES:
            if estacao.codigo not in prefs['radares']:
                continue
            balao = self._balao_do_radar(estacao, agora)
            lista.append(([estacao.lat, estacao.lon], raio_m, balao))
        return lista

    def _balao_do_radar(self, estacao: radar.Radar, agora: datetime) -> str:
        """Monta o balão de um radar: horário da última imagem em verde (recente) ou vermelho.

        Parâmetros:
            estacao: o radar (de `radar.RADARES`).
            agora: o horário atual (UTC).

        Retorna:
            HTML pronto para o bindPopup.
        """
        quando = self.ultimas.get(estacao.codigo)
        recente = bool(quando and (agora - quando).total_seconds() <= config.RADAR_VERMELHO_ATE_MIN * 60)
        if quando:
            cor = '#059669' if recente else '#ef4444'
            linha = (f'Última imagem: {quando:%d/%m %H:%MZ}', cor)
        else:
            linha = ('Dados indisponíveis', '#ef4444')
        nota = ('(desatualizado)', '#ef4444') if quando and not recente else None
        return desenho.balao(f'{estacao.nome}/{estacao.uf}', [linha], nota)


class CamadaSatelite:
    """Imagem de satélite da REDEMET (continente inteiro), por baixo do radar e do WIFS."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camada = None
        self.imagem: redemet.Imagem | None = None
        self.erro = ''
        self.rotulo_info: ui.label | None = None         # preenchido por tela/painel_camadas.py
        self.congelada = False                    # durante a animação, não troca a imagem
        self._pedido = UltimoPedido()

    async def aplicar(self) -> None:
        """Liga, desliga ou troca o tipo de imagem."""
        self._pedido.novo()
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
            self.imagem = None
        await self.atualizar()

    async def atualizar(self) -> None:
        """Busca a imagem mais recente; só troca no mapa se ela mudou."""
        prefs = self.prefs
        if not prefs['satelite_ligado'] or self.congelada:
            self._informar()
            return
        pedido = self._pedido.novo()
        try:
            imagem = await redemet.satelite(prefs['satelite_tipo'])
            self.erro = '' if imagem else 'a REDEMET não devolveu imagem'
        except redemet.Indisponivel as erro:
            imagem = None
            self.erro = str(erro)
        resposta_velha = not self._pedido.vale(pedido) or not prefs['satelite_ligado']
        if resposta_velha or imagem is None or imagem == self.imagem:
            self._informar()
            return
        if self.camada:   # troca a imagem sem tirar a camada (não pisca)
            self.camada.run_method('setUrl', imagem.url())
            self.camada.run_method('setBounds', imagem.limites)
        else:             # no painel das imagens de fundo, acima do mapa e abaixo do radar/WIFS
            with self.mapa:
                self.camada = self.mapa.image_overlay(url=imagem.url(), bounds=imagem.limites, options={
                    'opacity': prefs['satelite_opacidade'], 'pane': 'tilePane', 'zIndex': 2,
                    'interactive': False, 'attribution': 'REDEMET / satélite'})
        self.imagem = imagem
        self._informar()

    def mudar_opacidade(self) -> None:
        """Aplica a opacidade escolhida no painel."""
        if self.camada:
            self.camada.run_method('setOpacity', self.prefs['satelite_opacidade'])

    def pausar_ao_vivo(self) -> None:
        """A animação vai começar: esconde a imagem ao vivo e para de trocá-la."""
        self.congelada = True
        if self.camada:
            self.camada.run_method('setOpacity', 0)

    def retomar_ao_vivo(self) -> None:
        """A animação parou: mostra a imagem ao vivo de novo e busca a mais nova."""
        self.congelada = False
        self.mudar_opacidade()
        background_tasks.create(self.atualizar(), name='satelite')

    def _informar(self) -> None:
        """Escreve no painel o erro ou o horário da imagem."""
        if self.erro:
            texto = self.erro
        elif self.imagem and self.imagem.quando:
            texto = f'imagem de {self.imagem.quando:%d/%m %H:%MZ}'
        else:
            texto = ''
        escrever(self.rotulo_info, texto, 'text-caption ' + ('text-negative' if self.erro else 'text-grey'))


class CamadaRaios:
    """Raios de duas fontes:
      - API da REDEMET (stsc, sem VPN): células de 0,1° com raios, cor = há quanto tempo a
        célula está ativa. O Python desenha a imagem (/redemet/raios).
      - GeoServer (VPN): janela de 60 min, ao vivo (fim = agora) ou num horário escolhido.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camada = None                        # GeoServer: a camada WMS
        self.fim: datetime | None = None          # None = ao vivo (GeoServer)
        self.rotulo_janela: ui.label | None = None       # preenchido por tela/painel_camadas.py
        self.imagem_api = None                    # camada da imagem (API)
        self.quadro: redemet.QuadroRaios | None = None
        self.erro = ''
        self.congelada = False                    # durante a animação, não troca a imagem
        self._pedido = UltimoPedido()

    def pela_api(self) -> bool:
        """Diz se a fonte escolhida é a API da REDEMET (e não o GeoServer)."""
        return self.prefs['raios_fonte'] == 'api'

    def _parametros(self) -> dict:
        """Parâmetros WMS do GeoServer: janela de tempo, classificação e raio dos pontos."""
        fim = self.fim or raios.agora()
        return raios.parametros(fim, self.prefs['raios_classificacao'], self.prefs['raios_raio_km'])

    def aplicar(self) -> None:
        """Recria a camada com as escolhas atuais (liga, desliga, troca de fonte)."""
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
        if self.imagem_api:
            self.mapa.remove_layer(self.imagem_api)
            self.imagem_api = None
            self.quadro = None
        self._pedido.novo()
        if self.prefs['raios_ligado'] and self.pela_api():
            background_tasks.create(self.desenhar_api(), name='raios-api')
        elif self.prefs['raios_ligado']:
            opcoes = {'layers': 'redemet:lightning_data', 'format': 'image/png', 'transparent': True,
                      'version': '1.1.1', 'opacity': 0.85, 'zIndex': 4, 'attribution': 'REDEMET',
                      **self._parametros()}
            with self.mapa:
                self.camada = self.mapa.wms_layer(url_template='/radar/wms', options=opcoes)
        self.mostrar_janela()

    def atualizar(self) -> None:
        """Troca só os parâmetros (horário, estilo, raio): o Leaflet recarrega sem piscar."""
        if self.camada:
            self.camada.run_method('setParams', self._parametros())
        self.mostrar_janela()

    def andar(self, minutos: int) -> None:
        """Anda a janela do GeoServer no tempo (botões do painel).

        Parâmetros:
            minutos: quanto andar (negativo volta).
        """
        self.fim = raios.deslocar(self.fim, minutos)
        self.atualizar()

    def ao_vivo(self) -> None:
        """Volta a janela do GeoServer para o horário atual (botão "agora")."""
        self.fim = None
        self.atualizar()

    async def no_relogio(self) -> None:
        """Chamado pelo relógio: na API busca o quadro novo; no GeoServer ao vivo anda a janela."""
        if self.pela_api():
            if self.prefs['raios_ligado']:
                await self.desenhar_api()
        elif self.fim is None:     # ao vivo: a janela anda com o relógio
            self.atualizar()

    async def desenhar_api(self) -> None:
        """Mostra a imagem do quadro mais recente do stsc; só troca no mapa se chegou um quadro novo."""
        if self.congelada:      # a animação está na tela; ao parar, ela chama esta função de novo
            return
        pedido = self._pedido.novo()
        try:
            quadros = await redemet.raios_quadros()
            self.erro = '' if quadros else 'a REDEMET não devolveu raios'
        except redemet.Indisponivel as erro:
            self.erro = str(erro)
            return
        if not self._pedido.vale(pedido) or not quadros or not self.prefs['raios_ligado'] or not self.pela_api():
            return
        ultimo = quadros[-1]
        if self.quadro and self.quadro.chave == ultimo.chave:
            return
        if self.imagem_api:
            self.imagem_api.run_method('setUrl', ultimo.url())
        else:
            with self.mapa:
                self.imagem_api = self.mapa.image_overlay(url=ultimo.url(), bounds=ultimo.limites, options={
                    'opacity': OPACIDADE_RAIOS, 'interactive': False, 'className': 'pixelado'})
        self.quadro = ultimo

    def pausar_ao_vivo(self) -> None:
        """A animação vai começar: esconde a imagem ao vivo e para de trocá-la."""
        self.congelada = True
        if self.imagem_api:
            self.imagem_api.run_method('setOpacity', 0)

    def retomar_ao_vivo(self) -> None:
        """A animação parou: mostra a imagem ao vivo de novo e busca a mais nova."""
        self.congelada = False
        if self.imagem_api:
            self.imagem_api.run_method('setOpacity', OPACIDADE_RAIOS)
        if self.pela_api() and self.prefs['raios_ligado']:
            background_tasks.create(self.desenhar_api(), name='raios-api')

    def mostrar_janela(self) -> None:
        """Escreve no painel a janela de tempo dos raios (e "ao vivo" quando ela anda com o relógio)."""
        texto = raios.rotulo_janela(self.fim or raios.agora())
        if self.fim is None:
            texto += ' · ao vivo'
        escrever(self.rotulo_janela, texto)
