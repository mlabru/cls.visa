"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de observação: radar, satélite e raios (o que já aconteceu, medido).

As três podem ser pausadas pela animação (`pausar_ao_vivo` / `retomar_ao_vivo`).
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
        self.mapa, self.prefs, self.camada = mapa, prefs, None
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
        return self.prefs['radar_fonte'] == 'api'

    def raio_km(self) -> int:
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
        p = self.prefs
        if not p['radar_ligado'] or not p['radares']:
            return
        if self.pela_api():
            background_tasks.create(self.desenhar_api(), name='radar-api')
            return
        opcoes = {
            'layers': f"redemet:{p['radar_produto']}_{p['radar_alcance']}",
            'format': 'image/png', 'transparent': True, 'version': '1.1.1',
            'TIME': radar.janela_de_tempo(),
            'CQL_FILTER': radar.filtro_cql(p['radares']),
            'dbz_min': p['dbz_min'],
            'opacity': OPACIDADE_RADAR, 'zIndex': 3, 'attribution': 'REDEMET',
        }
        if p['radar_produto'] == 'cappi':
            opcoes['ELEVATION'] = p['radar_altura']
        with self.mapa:
            self.camada = self.mapa.wms_layer(url_template='/radar/wms', options=opcoes)

    async def desenhar_api(self) -> None:
        """Uma imagem por estação selecionada; mantém no mapa as que não mudaram."""
        if self.congelada:      # a animação está na tela; ao parar, ela chama esta função de novo
            return
        pedido, p = self._pedido.novo(), self.prefs
        try:
            lista = await redemet.radar_api(p['radar_produto_api'])
            self.erro = ''
        except redemet.Indisponivel as erro:
            self.erro = str(erro)
            return
        if not self._pedido.vale(pedido) or not p['radar_ligado'] or not self.pela_api():
            return
        desejadas = {i.codigo: i for i in lista if i.caminho and i.codigo in p['radares']}
        for codigo in list(self.imagens):
            if self.imagens[codigo][1] != desejadas.get(codigo):
                self.mapa.remove_layer(self.imagens.pop(codigo)[0])
        with self.mapa:
            for codigo, imagem in desejadas.items():
                if codigo not in self.imagens:
                    camada = self.mapa.image_overlay(url=imagem.url(p['dbz_min']), bounds=imagem.limites,
                                                     options={'opacity': OPACIDADE_RADAR, 'interactive': False})
                    self.imagens[codigo] = (camada, imagem)

    def mudar_limiar(self) -> None:
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
        if self.camada:
            self.camada.run_method('setParams', {'TIME': radar.janela_de_tempo()})
        elif self.pela_api() and self.prefs['radar_ligado']:
            await self.desenhar_api()

    # --- Status (horário da última imagem de cada radar) -------------------------

    def status_de(self, codigo: str) -> str:
        return radar.classificar_status(self.ultimas.get(codigo))

    async def consultar_status(self, aplicar_filtro: bool = False) -> None:
        """Horário da última imagem de cada radar (API ou WFS do GeoServer). Com `aplicar_filtro`,
        seleciona os radares conforme os filtros de status (ao abrir e ao trocar de produto)."""
        await self.mapa.initialized()   # antes disso, o que se manda ao mapa se perde
        p = self.prefs
        if self.pela_api():
            try:
                lista = await redemet.radar_api(p['radar_produto_api'])
                ultimas = {i.codigo: i.quando for i in lista if i.quando and i.caminho}
                self.erro = ''
            except redemet.Indisponivel as erro:
                self.erro = str(erro)
                ultimas = None
        else:
            ultimas = await radar.buscar_status(p['radar_produto'], p['radar_alcance'], p['radar_altura'])
        self.status_ok = ultimas is not None
        if ultimas is not None:
            self.ultimas = ultimas
            if aplicar_filtro:
                filtro = p['radar_status_filtro']
                p['radares'] = [r.codigo for r in radar.RADARES if filtro[self.status_de(r.codigo)]]
                self.aplicar()
        self.desenhar_circulos()
        self.ao_mudar_status()

    def alternar_filtro(self, status: str) -> None:
        """Liga/desliga um filtro de status: (des)seleciona os radares nesse status."""
        p = self.prefs
        ligado = p['radar_status_filtro'][status] = not p['radar_status_filtro'][status]
        afetados = {r.codigo for r in radar.RADARES if self.status_de(r.codigo) == status}
        escolhidos = set(p['radares']) | afetados if ligado else set(p['radares']) - afetados
        p['radares'] = [r.codigo for r in radar.RADARES if r.codigo in escolhidos]
        self.aplicar()
        self.ao_mudar_status()

    async def mudar_produto(self) -> None:
        self.aplicar()
        await self.consultar_status(aplicar_filtro=True)

    def desenhar_circulos(self) -> None:
        """Um L.circle por radar selecionado; o clique mostra o horário da última imagem."""
        if not self.mapa.is_initialized:   # o balão se perderia; consultar_status() redesenha
            return
        p = self.prefs
        lista = []
        if p['radar_ligado'] and p['radar_circulos']:
            raio_m = self.raio_km() * 1000
            agora = datetime.now(UTC)
            for r in radar.RADARES:
                if r.codigo not in p['radares']:
                    continue
                quando = self.ultimas.get(r.codigo)
                recente = bool(quando and (agora - quando).total_seconds() <= config.RADAR_VERMELHO_ATE_MIN * 60)
                if quando:
                    linha = (f'Última imagem: {quando:%d/%m %H:%MZ}', '#059669' if recente else '#ef4444')
                else:
                    linha = ('Dados indisponíveis', '#ef4444')
                nota = ('(desatualizado)', '#ef4444') if quando and not recente else None
                lista.append(([r.lat, r.lon], raio_m, desenho.balao(f'{r.nome}/{r.uf}', [linha], nota)))
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


class CamadaSatelite:
    """Imagem de satélite da REDEMET (continente inteiro), por baixo do radar e do WIFS."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
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
            self.camada, self.imagem = None, None
        await self.atualizar()

    async def atualizar(self) -> None:
        """Busca a imagem mais recente; só troca no mapa se ela mudou."""
        p = self.prefs
        if not p['satelite_ligado'] or self.congelada:
            self._informar()
            return
        pedido = self._pedido.novo()
        try:
            imagem = await redemet.satelite(p['satelite_tipo'])
            self.erro = '' if imagem else 'a REDEMET não devolveu imagem'
        except redemet.Indisponivel as erro:
            imagem, self.erro = None, str(erro)
        if not self._pedido.vale(pedido) or not p['satelite_ligado'] or imagem is None or imagem == self.imagem:
            self._informar()
            return
        if self.camada:   # troca a imagem sem tirar a camada (não pisca)
            self.camada.run_method('setUrl', imagem.url())
            self.camada.run_method('setBounds', imagem.limites)
        else:             # no painel das imagens de fundo, acima do mapa e abaixo do radar/WIFS
            with self.mapa:
                self.camada = self.mapa.image_overlay(url=imagem.url(), bounds=imagem.limites, options={
                    'opacity': p['satelite_opacidade'], 'pane': 'tilePane', 'zIndex': 2,
                    'interactive': False, 'attribution': 'REDEMET / satélite'})
        self.imagem = imagem
        self._informar()

    def mudar_opacidade(self) -> None:
        if self.camada:
            self.camada.run_method('setOpacity', self.prefs['satelite_opacidade'])

    def pausar_ao_vivo(self) -> None:
        self.congelada = True
        if self.camada:
            self.camada.run_method('setOpacity', 0)

    def retomar_ao_vivo(self) -> None:
        self.congelada = False
        self.mudar_opacidade()
        background_tasks.create(self.atualizar(), name='satelite')

    def _informar(self) -> None:
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
        self.mapa, self.prefs, self.camada = mapa, prefs, None
        self.fim: datetime | None = None          # None = ao vivo (GeoServer)
        self.rotulo_janela: ui.label | None = None       # preenchido por tela/painel_camadas.py
        self.imagem_api = None                    # camada da imagem (API)
        self.quadro: redemet.QuadroRaios | None = None
        self.erro = ''
        self.congelada = False                    # durante a animação, não troca a imagem
        self._pedido = UltimoPedido()

    def pela_api(self) -> bool:
        return self.prefs['raios_fonte'] == 'api'

    def _parametros(self) -> dict:
        p = self.prefs
        return raios.parametros(self.fim or raios.agora(), p['raios_classificacao'], p['raios_raio_km'])

    def aplicar(self) -> None:
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
        if self.imagem_api:
            self.mapa.remove_layer(self.imagem_api)
            self.imagem_api, self.quadro = None, None
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
        self.fim = raios.deslocar(self.fim, minutos)
        self.atualizar()

    def ao_vivo(self) -> None:
        self.fim = None
        self.atualizar()

    async def no_relogio(self) -> None:
        if self.pela_api():
            if self.prefs['raios_ligado']:
                await self.desenhar_api()
        elif self.fim is None:     # ao vivo: a janela anda com o relógio
            self.atualizar()

    async def desenhar_api(self) -> None:
        """Imagem do quadro mais recente do stsc; só troca no mapa se chegou um quadro novo."""
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
        self.congelada = True
        if self.imagem_api:
            self.imagem_api.run_method('setOpacity', 0)

    def retomar_ao_vivo(self) -> None:
        self.congelada = False
        if self.imagem_api:
            self.imagem_api.run_method('setOpacity', OPACIDADE_RAIOS)
        if self.pela_api() and self.prefs['raios_ligado']:
            background_tasks.create(self.desenhar_api(), name='raios-api')

    def mostrar_janela(self) -> None:
        texto = raios.rotulo_janela(self.fim or raios.agora())
        escrever(self.rotulo_janela, texto + (' · ao vivo' if self.fim is None else ''))
