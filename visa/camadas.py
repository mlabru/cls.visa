"""
Camadas do mapa: cada classe liga, desliga e atualiza uma camada do Leaflet.

Não monta painel nenhum: a interface (interface.py) cria os controles e chama estes métodos.

Padrões que se repetem nas classes (explicados em DECISOES.md):
  aplicar()      recria a camada a partir das preferências (liga, desliga, troca de fonte);
  atualizar()    troca só o que mudou (setUrl/setParams), para o mapa não piscar;
  _pedido        número do último pedido à rede: resposta de um pedido velho é ignorada;
  congelada      True durante a animação: a camada ao vivo não troca de imagem;
  self.prefs     é o MESMO dicionário de preferências da interface (não uma cópia).
Os atributos de tela (ui.label etc.) começam como None e são preenchidos pela interface.
"""
from datetime import UTC, datetime
from typing import ClassVar

import httpx
from nicegui import background_tasks, ui

import aerodromos
import aeronaves
import animacao
import config
import desenho
import espaco
import geo
import impacto
import previsao
import radar
import raios
import redemet
import rotas
import severo
import vento
import wifs

# Opacidades das imagens: o radar deixa ver o satélite e o mapa por baixo; os raios (pequenos
# quadrados coloridos) ficam quase opacos para não sumir sobre o radar.
OPACIDADE_RADAR = 0.75
OPACIDADE_RAIOS = 0.9


class CamadaRadar:
    """Radar de duas fontes: a API da REDEMET (uma imagem por estação, sem VPN) ou o
    GeoServer interno (uma camada WMS, precisa da VPN). Botões, status e círculos são comuns."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs, self.camada = mapa, prefs, None
        self.imagens: dict[str, tuple] = {}       # API: código -> (camada no mapa, Imagem)
        self.erro_api = ''
        self.congelada = False                    # durante a animação, não troca as imagens
        self._pedido = 0
        self.circulos: list = []                  # camadas L.circle dos círculos de alcance
        self._circulos_desenhados: list = []      # o que está no mapa (evita redesenhar igual)
        self.ultimas: dict[str, datetime] = {}   # código -> horário da última imagem
        self.status_ok = False                    # o GeoServer respondeu à consulta de status?
        self.ao_mudar_status = lambda: None       # a interface repinta os botões

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
        self._pedido += 1                 # descarta respostas da API pedidas antes
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
        self._pedido += 1
        pedido, p = self._pedido, self.prefs
        try:
            lista = await redemet.radar_api(p['radar_produto_api'])
            self.erro_api = ''
        except redemet.Indisponivel as erro:
            self.erro_api = str(erro)
            return
        if pedido != self._pedido or not p['radar_ligado'] or not self.pela_api():
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
                self.erro_api = ''
            except redemet.Indisponivel as erro:
                self.erro_api = str(erro)
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
                recente = bool(quando and (agora - quando).total_seconds() <= 3 * 3600)
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
        self.info: ui.label | None = None         # preenchido pela interface
        self.congelada = False                    # durante a animação, não troca a imagem
        self._pedido = 0

    async def aplicar(self) -> None:
        """Liga, desliga ou troca o tipo de imagem."""
        self._pedido += 1
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
        self._pedido += 1
        pedido = self._pedido
        try:
            imagem = await redemet.satelite(p['satelite_tipo'])
            self.erro = '' if imagem else 'a REDEMET não devolveu imagem'
        except redemet.Indisponivel as erro:
            imagem, self.erro = None, str(erro)
        if pedido != self._pedido or not p['satelite_ligado'] or imagem is None or imagem == self.imagem:
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

    def _informar(self) -> None:
        if not self.info:
            return
        if self.erro:
            self.info.set_text(self.erro)
        elif self.imagem and self.imagem.quando:
            self.info.set_text(f'imagem de {self.imagem.quando:%d/%m %H:%MZ}')
        else:
            self.info.set_text('')
        self.info.classes(replace='text-caption ' + ('text-negative' if self.erro else 'text-grey'))


class CamadaAnimacao:
    """Anima radar (fonte API) e satélite nas últimas horas.

    Ao começar, o Python baixa e prepara todas as imagens (ficam no cache do servidor) e
    cria no mapa uma camada por imagem, invisível. Cada passo só muda a opacidade: liga as
    imagens daquele horário e desliga as do anterior. Nada pisca e nada é baixado de novo.
    Enquanto anima, as camadas ao vivo ficam escondidas e paradas.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict, camada_radar: CamadaRadar,
                 camada_satelite: CamadaSatelite, camada_raios: 'CamadaRaios') -> None:
        self.mapa, self.prefs = mapa, prefs
        self.radar, self.satelite, self.raios = camada_radar, camada_satelite, camada_raios
        self.passos: list[animacao.Passo] = []
        self.indice = 0
        self.ativa = self.tocando = self.preparando = False
        self.aviso = ''
        self.camadas: dict[str, object] = {}      # url -> camada (invisível até ser a vez dela)
        self.visiveis: dict[str, float] = {}      # url -> opacidade, as que estão na tela
        self.ao_mudar = lambda: None              # a interface atualiza os controles
        self._assinatura: tuple = ()
        self._pausa = 0

    def assinatura(self) -> tuple:
        """O que, se mudar, invalida a animação (ela para e volta ao vivo)."""
        p = self.prefs
        return (p['satelite_ligado'], p['satelite_tipo'], p['satelite_opacidade'], p['radar_ligado'],
                p['radar_fonte'], p['radar_produto_api'], tuple(p['radares']), p['dbz_min'],
                p['raios_ligado'], p['raios_fonte'], p['animacao_janela_min'])

    def _informar(self, aviso: str = '') -> None:
        self.aviso = aviso
        self.ao_mudar()

    async def iniciar(self) -> None:
        if self.ativa or self.preparando:
            return
        p = self.prefs
        com_satelite = p['satelite_ligado']
        com_radar = p['radar_ligado'] and self.radar.pela_api()
        com_raios = p['raios_ligado'] and self.raios.pela_api()
        if not (com_satelite or com_radar or com_raios):
            self._informar('ligue o satélite, o radar ou os raios (fonte API) para animar')
            return
        self.preparando, inicio = True, self.assinatura()
        self._informar('buscando os horários…')
        try:
            janela, dbz = p['animacao_janela_min'], p['dbz_min']
            satelites = await redemet.satelite_quadros(
                p['satelite_tipo'], animacao.quadros_de_satelite(janela)) if com_satelite else []
            quadros = await redemet.radar_quadros(p['radar_produto_api']) if com_radar else []
            escolhidas = set(p['radares'])
            quadros = [redemet.QuadroRadar(q.quando, tuple(i for i in q.imagens if i.codigo in escolhidas))
                       for q in quadros]
            relampagos = await redemet.raios_quadros() if com_raios else []
            passos = animacao.linha_do_tempo(satelites, quadros, janela, relampagos)
            if not passos:
                self.preparando = False
                self._informar('a REDEMET não devolveu imagens para animar')
                return
            imagens = {passo.satelite.url(): (passo.satelite, 0) for passo in passos if passo.satelite}
            imagens.update({i.url(dbz): (i, dbz) for passo in passos if passo.radar for i in passo.radar.imagens})
            quadros_raios = {passo.raios.url(): passo.raios for passo in passos if passo.raios}
            self._informar(f'preparando {len(imagens) + len(quadros_raios)} imagens…')
            await redemet.preparar(list(imagens.values()))
            for quadro in quadros_raios.values():          # desenhadas aqui mesmo (rápido)
                redemet.imagem_raios(quadro.chave)
        except redemet.Indisponivel as erro:
            self.preparando = False
            self._informar(str(erro))
            return
        self.preparando = False
        if self.assinatura() != inicio:     # a pessoa mudou as camadas enquanto preparava
            self._informar('as camadas mudaram: tente de novo')
            return
        self.radar.congelada = self.satelite.congelada = self.raios.congelada = True
        self._esconder_ao_vivo(True)
        with self.mapa:
            for url, (imagem, _) in imagens.items():
                if imagem.caminho.startswith('satelite/'):   # por baixo, como a camada ao vivo
                    opcoes = {'opacity': 0, 'pane': 'tilePane', 'zIndex': 2, 'interactive': False}
                else:
                    opcoes = {'opacity': 0, 'interactive': False}
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=imagem.limites, options=opcoes)
            for url, quadro in quadros_raios.items():        # por último: ficam por cima do radar
                self.camadas[url] = self.mapa.image_overlay(url=url, bounds=quadro.limites, options={
                    'opacity': 0, 'interactive': False, 'className': 'pixelado'})
        self.passos, self._assinatura = passos, inicio
        self.ativa = self.tocando = True
        self._pausa = 0
        avisos = [f'{nome} do GeoServer não anima' for nome, ligado, api in
                  (('o radar', p['radar_ligado'], com_radar), ('os raios', p['raios_ligado'], com_raios))
                  if ligado and not api]
        self.aviso = '; '.join(avisos)
        self.mostrar(0)

    def _alvo(self, passo: animacao.Passo) -> dict[str, float]:
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
        """Liga as imagens do passo `indice` e desliga as que não fazem parte dele."""
        alvo = self._alvo(self.passos[indice])
        for url in set(self.visiveis) - set(alvo):
            self.camadas[url].run_method('setOpacity', 0)
        for url, opacidade in alvo.items():
            if url not in self.visiveis:
                self.camadas[url].run_method('setOpacity', opacidade)
        self.visiveis, self.indice = alvo, indice
        self.ao_mudar()

    def tique(self) -> None:
        """Chamado pelo relógio da página: avança um passo (e para de vez se as camadas mudaram)."""
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
        if self.ativa:
            self.tocando = False
            self.mostrar((self.indice + passos) % len(self.passos))

    def ir_para(self, indice: int) -> None:
        if self.ativa and indice != self.indice and 0 <= indice < len(self.passos):
            self.tocando = False
            self.mostrar(indice)

    def parar(self, aviso: str = '') -> None:
        """Volta ao vivo: tira as imagens da animação e reaparece com as camadas atualizadas."""
        for camada in self.camadas.values():
            self.mapa.remove_layer(camada)
        self.camadas.clear()
        self.visiveis = {}
        self.passos, self.indice = [], 0
        self.ativa = self.tocando = False
        self.radar.congelada = self.satelite.congelada = self.raios.congelada = False
        self._esconder_ao_vivo(False)
        if self.radar.pela_api() and self.prefs['radar_ligado']:
            background_tasks.create(self.radar.desenhar_api(), name='radar-api')
        if self.raios.pela_api() and self.prefs['raios_ligado']:
            background_tasks.create(self.raios.desenhar_api(), name='raios-api')
        background_tasks.create(self.satelite.atualizar(), name='satelite')
        self._informar(aviso)

    def _esconder_ao_vivo(self, esconder: bool) -> None:
        for camada, _ in self.radar.imagens.values():
            camada.run_method('setOpacity', 0 if esconder else OPACIDADE_RADAR)
        if self.satelite.camada:
            self.satelite.camada.run_method('setOpacity', 0 if esconder else self.prefs['satelite_opacidade'])
        if self.raios.imagem_api:
            self.raios.imagem_api.run_method('setOpacity', 0 if esconder else OPACIDADE_RAIOS)


class CamadasWifs:
    """WIFS: uma camada WMS por variável, todas no mesmo nível de voo."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.camadas: dict[str, object] = {}

    def aplicar(self, chave: str) -> None:
        if chave in self.camadas:
            self.mapa.remove_layer(self.camadas.pop(chave))
        if self.prefs['wifs'][chave]:
            with self.mapa:
                self.camadas[chave] = self.mapa.wms_layer(
                    url_template='/radar/wms', options=wifs.opcoes_wms(chave, self.prefs['wifs_fl']))

    def trocar_nivel(self) -> None:
        for camada in self.camadas.values():
            camada.run_method('setParams', {'ELEVATION': str(self.prefs['wifs_fl'])})

    def recarregar(self) -> None:
        """TIME=current: o GeoServer devolve a previsão vigente; basta pedir os tiles de novo."""
        for camada in self.camadas.values():
            camada.run_method('redraw')


class CamadaRaios:
    """Raios de duas fontes:
      - API da REDEMET (stsc, sem VPN): células de 0,1° com raios, cor = há quanto tempo a
        célula está ativa. O Python desenha a imagem (/redemet/raios).
      - GeoServer (VPN): janela de 60 min, ao vivo (fim = agora) ou num horário escolhido.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs, self.camada = mapa, prefs, None
        self.fim: datetime | None = None          # None = ao vivo (GeoServer)
        self.janela: ui.label | None = None       # preenchido pela interface
        self.imagem_api = None                    # camada da imagem (API)
        self.quadro: redemet.QuadroRaios | None = None
        self.erro_api = ''
        self.congelada = False                    # durante a animação, não troca a imagem
        self._pedido = 0

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
        self._pedido += 1
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
        self._pedido += 1
        pedido = self._pedido
        try:
            quadros = await redemet.raios_quadros()
            self.erro_api = '' if quadros else 'a REDEMET não devolveu raios'
        except redemet.Indisponivel as erro:
            self.erro_api = str(erro)
            return
        if pedido != self._pedido or not quadros or not self.prefs['raios_ligado'] or not self.pela_api():
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

    def mostrar_janela(self) -> None:
        if self.janela:
            texto = raios.rotulo_janela(self.fim or raios.agora())
            self.janela.set_text(texto + (' · ao vivo' if self.fim is None else ''))


class CamadaRotas:
    """Trajetórias pedidas no popup das aeronaves (botão "Ver / ocultar rota")."""

    def __init__(self, mapa: ui.leaflet) -> None:
        self.mapa = mapa
        self.camadas: dict[str, list] = {}         # indicativo -> linhas (L.polyline) no mapa
        self.lista: ui.label | None = None        # preenchidos pela interface
        self.botao_limpar: ui.button | None = None

    async def alternar(self, indicativo: str) -> None:
        indicativo = str(indicativo or '').upper()
        if indicativo in self.camadas:
            self.remover(indicativo)
            self.mostrar_lista()
            return
        try:
            rota = await rotas.buscar(indicativo)
        except ValueError:
            ui.notify(f'{indicativo}: indicativo sem rota consultável', type='warning')
            return
        except httpx.HTTPError as erro:
            ui.notify(f'O tracker do DECEA não respondeu ({type(erro).__name__})', type='negative')
            return
        if rota is None:
            ui.notify(f'{indicativo}: nenhuma posição registrada hoje', type='warning')
            return
        self.camadas[indicativo] = self.desenhar(rota)
        self.mostrar_lista()

    def desenhar(self, rota: rotas.Rota) -> list:
        """Uma linha por trecho: o voo atual em laranja, os anteriores do dia em cinza."""
        linhas, n = [], len(rota.trechos)
        with self.mapa:
            for i, t in enumerate(rota.trechos):
                atual = i == n - 1
                linha = self.mapa.generic_layer(name='polyline', args=[t.pontos, {
                    'color': '#f97316' if atual else '#94a3b8', 'weight': 2.5 if atual else 1.5,
                    'opacity': 0.85, 'dashArray': '6 8'}])
                trecho = f"trecho {i + 1} de {n}{' (atual)' if atual else ''}" if n > 1 else ''
                linha.run_method('bindPopup', desenho.balao(
                    rota.indicativo, [(f'{t.origem} → {t.destino}', None), (trecho, None)]))
                linhas.append(linha)
        caixa = geo.limites([p for t in rota.trechos for p in t.pontos])
        if caixa:
            self.mapa.run_map_method('fitBounds', caixa, {'padding': [30, 30], 'maxZoom': 9})
        return linhas

    def remover(self, indicativo: str) -> None:
        for linha in self.camadas.pop(indicativo):
            self.mapa.remove_layer(linha)

    def limpar(self) -> None:
        for indicativo in list(self.camadas):
            self.remover(indicativo)
        self.mostrar_lista()

    def mostrar_lista(self) -> None:
        if self.lista:
            self.lista.set_text('Rotas no mapa: ' + ', '.join(self.camadas) if self.camadas else '')
        if self.botao_limpar:
            self.botao_limpar.set_visibility(bool(self.camadas))


class CamadaSevero:
    """Tempo severo previsto: escolhas em cascata (modelo → resolução → tipo → rodada → validade)."""

    NIVEIS = ('origem', 'resolucao', 'tipo', 'rodada', 'validade')

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.camadas: list = []                          # L.geoJSON (feições) e L.rectangle (cobertura)
        self.feicoes: list[dict] = []                    # última resposta do WFS
        self.tipo = 'area'
        self.erro = ''                                   # última falha do GeoServer (barra de saúde)
        self.cat: list[dict] = []
        self.escolha = dict.fromkeys(self.NIVEIS)        # valores atuais (a interface se liga aqui)
        self.selects: dict[str, ui.select] = {}          # preenchidos pela interface
        self.situacao: ui.label | None = None
        self._pedido = 0

    def _opcoes(self, nivel: str) -> dict[str, str]:
        e, c = self.escolha, self.cat
        if nivel == 'origem':
            return {m: m.upper() for m in severo.modelos(c)}
        if nivel == 'resolucao':
            return {r: f'{r} km' for r in severo.resolucoes(c, e['origem'])}
        if nivel == 'tipo':
            return {t: severo.TIPOS.get(t, t) for t in severo.tipos(c, e['origem'], e['resolucao'])}
        if nivel == 'rodada':
            return {r: severo.rotulo(r) for r in severo.rodadas(c, e['origem'], e['resolucao'], e['tipo'])}
        return {v: severo.rotulo(v) for v in severo.validades(c, e['origem'], e['resolucao'], e['tipo'], e['rodada'])}

    def _cascata(self, a_partir_de: str, preferidos: dict | None = None) -> None:
        """Recalcula as opções a partir de um nível, mantendo o valor atual quando ainda existe."""
        preferidos = preferidos or {}
        for nivel in self.NIVEIS[self.NIVEIS.index(a_partir_de):]:
            opcoes = self._opcoes(nivel)
            atual = preferidos.get(nivel, self.escolha[nivel])
            if atual not in opcoes:  # padrão (como no original): rodada e validade mais recentes
                valores = list(opcoes)
                atual = (valores[-1] if nivel == 'validade' else valores[0]) if valores else None
            self.escolha[nivel] = atual          # antes do select: assim mudou() reconhece e ignora
            if nivel in self.selects:
                self.selects[nivel].set_options(opcoes, value=atual)

    async def ligar(self) -> None:
        if not self.prefs['severo_ligado']:
            self._pedido += 1
            self.feicoes = []
            self.redesenhar()
            self._informar('')
            return
        await self.carregar_catalogo()

    async def carregar_catalogo(self, forcar: bool = False) -> None:
        if not self.prefs['severo_ligado']:
            return
        self._informar('consultando o GeoServer...')
        try:
            self.cat = await severo.catalogo(forcar)
            self.erro = ''
        except severo.Indisponivel as erro:
            self.erro = f'GeoServer inacessível ({erro})'
            self._informar(self.erro, 'negative')
            return
        if not self.cat:
            self._informar('nenhuma previsão disponível', 'warning')
            return
        recente = severo.mais_recente(self.cat) if self.escolha['origem'] is None else None
        preferidos = {}
        if recente:   # primeira vez: abre na previsão mais recente
            preferidos = {'origem': recente['origem'], 'resolucao': severo.resolucao_texto(recente.get('resolucao_km')),
                          'tipo': recente.get('tipo'), 'rodada': recente['rodada'], 'validade': recente['validade']}
        self._cascata('origem', preferidos)
        await self.desenhar()

    async def mudou(self, nivel: str) -> None:
        valor = self.selects[nivel].value
        if valor == self.escolha[nivel]:   # mudança feita pelo próprio Python (_cascata/andar)
            return
        self.escolha[nivel] = valor
        if nivel != 'validade':
            self._cascata(self.NIVEIS[self.NIVEIS.index(nivel) + 1])
        await self.desenhar()

    async def andar(self, passo: int) -> None:
        validades = list(self._opcoes('validade'))
        if self.escolha['validade'] not in validades:
            return
        i = validades.index(self.escolha['validade']) + passo
        if 0 <= i < len(validades):
            self.escolha['validade'] = validades[i]
            self.selects['validade'].set_value(validades[i])
            await self.desenhar()

    async def desenhar(self) -> None:
        e = self.escolha
        try:
            cql = severo.filtro_cql(e['origem'], e['resolucao'], e['tipo'], e['rodada'], e['validade'])
        except (TypeError, ValueError):
            self._informar('escolha incompleta', 'warning')
            return
        self._pedido += 1
        pedido = self._pedido
        self._informar('carregando...')
        try:
            feicoes = await severo.feicoes(cql)
            self.erro = ''
        except severo.Indisponivel as erro:
            self.erro = f'GeoServer inacessível ({erro})'
            self._informar(self.erro, 'negative')
            return
        if pedido != self._pedido:   # o usuário já pediu outra coisa enquanto esta chegava
            return
        self.feicoes, self.tipo = feicoes, e['tipo']
        self.redesenhar()
        texto, cor = severo.situacao(e['validade'])
        self._informar(f'{len(feicoes)} feições · previsão {texto}', cor)

    def redesenhar(self) -> None:
        """Áreas (ou círculos dos pontos) num L.geoJSON com estilo fixo + retângulo da cobertura."""
        for camada in self.camadas:
            self.mapa.remove_layer(camada)
        self.camadas = []
        if not self.feicoes:
            return
        estilo = self.prefs['severo_estilo']
        dados = desenho.severo_geojson(self.feicoes, self.tipo, estilo['raio_km'])
        with self.mapa:
            if dados['features']:
                self.camadas.append(self.mapa.generic_layer(
                    name='geoJSON', args=[dados, {'style': desenho.severo_estilo(estilo, self.tipo)}]))
            caixa = severo.limites(self.feicoes)
            if caixa:
                self.camadas.append(self.mapa.generic_layer(name='rectangle', args=[caixa, desenho.COBERTURA]))

    def mudar_estilo(self) -> None:
        if self.tipo == 'ponto':   # o raio muda a forma dos círculos: refaz
            self.redesenhar()
        elif self.camadas:         # áreas: só troca cor e opacidade
            self.camadas[0].run_method('setStyle', desenho.severo_estilo(self.prefs['severo_estilo'], self.tipo))

    def _informar(self, texto: str, cor: str = 'grey') -> None:
        if self.situacao:
            self.situacao.set_text(texto)
            self.situacao.classes(replace=f'text-caption text-{cor}')


class CamadaVento:
    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs, self.camada = mapa, prefs, None
        self.referencia: datetime | None = None
        self.aviso: ui.label | None = None       # preenchidos pela interface
        self.horarios: ui.select | None = None
        if prefs['vento_horario'] not in self.opcoes_horario():
            prefs['vento_horario'] = 'recente'

    def opcoes_horario(self) -> dict[str, str]:
        disponiveis = vento.horarios_disponiveis(vento.listar_arquivos(), self.prefs['vento_fl'])
        opcoes = {'recente': 'Mais recente'}
        opcoes.update({h.isoformat(): h.strftime('%d/%m %H:%MZ') for h in disponiveis})
        return opcoes

    def trocar_nivel(self) -> None:
        if self.horarios:
            self.horarios.set_options(self.opcoes_horario(), value='recente')
        self.aplicar()

    def aplicar(self) -> None:
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
        self.referencia = None
        p = self.prefs
        if p['vento_ligado']:
            escolhido = None if p['vento_horario'] == 'recente' else datetime.fromisoformat(p['vento_horario'])
            arquivo = vento.escolher(vento.listar_arquivos(), p['vento_fl'], escolhido)
            if arquivo is None:
                ui.notify(f"Sem arquivo de vento para FL{p['vento_fl']:03d}", type='warning')
            else:
                dados = vento.carregar_recortado(arquivo.caminho)
                self.referencia = vento.horario_referencia(dados, arquivo)
                with self.mapa:
                    self.camada = self.mapa.generic_layer(name='velocityLayer', args=[{
                        'data': dados,
                        'displayValues': True,
                        'displayOptions': {'velocityType': 'Vento', 'position': 'bottomleft',
                                           'emptyString': 'Sem dados', 'speedUnit': 'kt',
                                           'directionString': 'Direção', 'speedString': 'Velocidade'},
                        'maxVelocity': 100,
                        'velocityScale': 0.005,
                    }])
        self.mostrar_idade()

    def mostrar_idade(self) -> None:
        if not self.aviso:
            return
        if self.referencia is None:
            self.aviso.set_visibility(False)
            return
        texto, cor = vento.idade(self.referencia)
        pa = config.VENTO_FL_PARA_PA[self.prefs['vento_fl']]
        self.aviso.set_text(f"FL{self.prefs['vento_fl']:03d} / {pa // 100} hPa · "
                            f"ref. {self.referencia:%d/%m/%Y %H:%MZ} · {texto}")
        self.aviso.classes(replace=f'text-{cor} text-caption')
        self.aviso.set_visibility(True)


class CamadaAeronaves:
    # Tráfego é contexto para o meteorologista: desenho discreto, para o tempo dominar a tela.
    # A exceção é a aeronave em área de raios ou eco forte: âmbar e sempre com o indicativo.
    ESTILO: ClassVar[dict] = {'cor': '#94a3b8', 'corIndicativo': '#cbd5e1', 'corNivel': '#94a3b8', 'tamanho': 12,
              'corAlerta': '#f59e0b'}

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.prefs = prefs
        self.versao_enviada = -1
        with mapa:
            self.camada = mapa.generic_layer(name='aeronavesLayer',
                                             args=[{**self.ESTILO, 'rotulos': prefs['aeronaves_rotulos']}])
        self.contador: ui.label | None = None

    def prefixos(self) -> list[str]:
        texto = self.prefs['aeronaves_prefixos'].upper()
        return [p.strip() for p in texto.split(',') if p.strip()]

    def enviar(self, forcar: bool = False) -> None:
        """Manda ao navegador as aeronaves filtradas (só se algo mudou)."""
        if not forcar and aeronaves.trafego.versao == self.versao_enviada:
            return
        self.versao_enviada = aeronaves.trafego.versao
        if self.prefs['aeronaves_ligado']:
            faixa = self.prefs['aeronaves_fl']
            lista = aeronaves.filtrar(self.prefixos(), faixa['min'], faixa['max'])
            for a in lista:          # em área de raios ou eco forte (calculado em impacto.py)
                a['alerta'] = impacto.alerta_da_aeronave(a['lat'], a['lon'])
        else:
            lista = []
        self.camada.run_method('setDados', lista)
        if self.contador:
            self.contador.set_text(f'{len(lista)} exibidas de {len(aeronaves.trafego.aeronaves)}')

    def mudar_rotulos(self) -> None:
        self.camada.run_method('setOpcoes', {'rotulos': self.prefs['aeronaves_rotulos']})


class CamadaConflitos:
    """FAB.ia: linha tracejada de cada aeronave até onde ela encontra o núcleo, e um círculo ali.

    Segue o filtro da camada de tráfego (ligada, prefixos, faixa de níveis): linha de uma
    aeronave escondida confundiria. A linha começa na posição ATUAL da aeronave (redesenho a
    cada REDESENHO_S), não na do cálculo, que tem até 1 min.
    """

    REDESENHO_S = 15
    MAX = 30

    def __init__(self, mapa: ui.leaflet, camada_aeronaves: CamadaAeronaves) -> None:
        self.mapa, self.aeronaves = mapa, camada_aeronaves
        self.camadas: list = []

    def desenhar(self) -> None:
        for camada in self.camadas:
            self.mapa.remove_layer(camada)
        self.camadas = []
        p = self.aeronaves.prefs
        if not p['aeronaves_ligado'] or not previsao.situacao.conflitos:
            return
        faixa = p['aeronaves_fl']
        visiveis = {a['indicativo'] for a in aeronaves.filtrar(self.aeronaves.prefixos(), faixa['min'], faixa['max'])}
        with self.mapa:
            for c in [c for c in previsao.situacao.conflitos if c.indicativo in visiveis][:self.MAX]:
                agora = aeronaves.trafego.aeronaves.get(c.chave)
                inicio = [agora.lat, agora.lon] if agora else [c.lat0, c.lon0]
                cor = previsao.COR_CONFIANCA[c.confianca]
                linha = self.mapa.generic_layer(name='polyline', args=[[inicio, [c.lat, c.lon]], {
                    'color': cor, 'weight': 2, 'opacity': 0.9, 'dashArray': '4 6'}])
                ponto = self.mapa.generic_layer(name='circleMarker', args=[[c.lat, c.lon], {
                    'radius': 7, 'color': cor, 'weight': 2, 'fill': True, 'fillOpacity': 0.15}])
                ponto.run_method('bindPopup', desenho.balao(
                    f'{c.indicativo} em ~{c.minutos} min',
                    [(c.nucleo.descricao(), None), (previsao.TEXTO_CONFIANCA[c.confianca], cor)],
                    ('FAB.ia: projeção em linha reta; consultivo', '#94a3b8')))
                self.camadas += [linha, ponto]


class CamadasEspaco:
    """Aerovias, cartas, FIR, TMA: vetores do arquivo local ou, na falta dele, imagens do DECEA."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.camadas: dict[str, object] = {}
        self.vetorial: set[str] = set()          # chaves desenhadas a partir de arquivo local
        self.fontes: dict[str, ui.label] = {}    # preenchido pela interface

    def estilo_vetor(self, chave: str) -> dict:
        definicao = espaco.POR_CHAVE[chave]
        estilo = self.prefs['estilos'].get(chave, espaco.ESTILO_PADRAO)
        cor = definicao.cor_padrao if estilo['cor'] == 'original' else estilo['cor']
        return {'cor': cor, 'traco': estilo['traco'], 'opacidade': estilo['opacidade'],
                'preenchimento': definicao.preenchimento}

    def aplicar(self, chave: str) -> None:
        if chave in self.camadas:
            self.mapa.remove_layer(self.camadas.pop(chave))
        self.vetorial.discard(chave)
        self.mostrar_fonte(chave)
        if not self.prefs['espaco'][chave]:
            return
        if espaco.arquivo_local(chave):
            with self.mapa:
                self.camadas[chave] = self.mapa.generic_layer(
                    name='espacoVetorLayer',
                    args=[f'/espaco/{chave}.geojson', {'estilo': self.estilo_vetor(chave)}])
            self.vetorial.add(chave)
            return
        definicao = espaco.POR_CHAVE[chave]
        estilo = self.prefs['estilos'].get(chave)
        opcoes = {
            'layers': definicao.camadas_wms, 'format': 'image/png', 'transparent': True,
            'version': '1.1.1', 'zIndex': 5, 'attribution': 'DECEA',
            'opacity': estilo['opacidade'] if estilo else 0.8,
        }
        if estilo:
            opcoes['estilo'] = {'cor': estilo['cor'], 'traco': estilo['traco']}
        with self.mapa:
            self.camadas[chave] = self.mapa.wms_layer(url_template=config.DECEA_WMS, options=opcoes)

    def mostrar_fonte(self, chave: str) -> None:
        rotulo = self.fontes.get(chave)
        if not rotulo:
            return
        caminho = espaco.arquivo_local(chave)
        if caminho is None:
            rotulo.set_text('fonte: imagens do DECEA')
            rotulo.classes(replace='text-caption text-grey q-pl-lg')
            return
        emenda, proxima = espaco.validade(caminho)
        if proxima:  # o próprio arquivo diz até quando vale (próxima emenda AIRAC)
            hoje = datetime.now(UTC).date().isoformat()
            velho = hoje >= proxima
            fim = f'{proxima[8:10]}/{proxima[5:7]}'
            rotulo.set_text(f'fonte: arquivo local, AIRAC de {emenda[8:10]}/{emenda[5:7]}' +
                            (f' · VENCIDO em {fim}, baixe de novo' if velho else f', válido até {fim}'))
        else:
            dias = espaco.idade_dias(caminho)
            velho = dias > espaco.CICLO_AIRAC_DIAS
            rotulo.set_text(f'fonte: arquivo local, há {dias:.0f} dias' + (' · ciclo AIRAC vencido?' if velho else ''))
        rotulo.classes(replace='text-caption q-pl-lg ' + ('text-warning' if velho else 'text-grey'))

    def mudar_estilo(self, chave: str) -> None:
        camada = self.camadas.get(chave)
        if camada and chave in self.vetorial:
            camada.run_method('setEstilo', self.estilo_vetor(chave))
        elif camada:  # imagens: muda só a aparência, sem baixar de novo
            estilo = self.prefs['estilos'][chave]
            camada.run_method('setEstilo', {'cor': estilo['cor'], 'traco': estilo['traco']})
            camada.run_method('setOpacity', estilo['opacidade'])


class CamadaAerodromos:
    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        with mapa:
            self.camada = mapa.generic_layer(name='aerodromosLayer', args=[{}])
        self.situacao: ui.label | None = None

    async def enviar(self) -> None:
        await self.mapa.initialized()   # antes disso o mapa ainda não existe no navegador
        lista = await aerodromos.obter() if self.prefs['aerodromos_ligado'] else []
        self.camada.run_method('setDados', [a.para_mapa() for a in lista])
        if self.situacao:
            erro = aerodromos.situacao.erro
            self.situacao.set_text(erro or f'{len(lista)} aeródromos' if self.prefs['aerodromos_ligado'] else '')
            self.situacao.classes(replace='text-caption ' + ('text-negative' if erro else ''))


# ---------------------------------------------------------------------------
# Todas as camadas de uma página
# ---------------------------------------------------------------------------

class CamadasDaPagina:
    """As camadas de UMA página (uma aba do navegador), criadas juntas.

    Existe para que cada área da tela (pacote `tela/`) receba um objeto só, e não onze
    parâmetros. Cada aba aberta tem o seu conjunto: as preferências e o que está no mapa
    são de cada pessoa. O que é comum a todos (aeronaves, impacto, caches) fica nos módulos.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs = mapa, prefs
        self.satelite = CamadaSatelite(mapa, prefs)
        self.radar = CamadaRadar(mapa, prefs)
        self.vento = CamadaVento(mapa, prefs)
        self.espaco = CamadasEspaco(mapa, prefs)
        self.aerodromos = CamadaAerodromos(mapa, prefs)
        self.aeronaves = CamadaAeronaves(mapa, prefs)
        self.conflitos = CamadaConflitos(mapa, self.aeronaves)
        self.raios = CamadaRaios(mapa, prefs)
        self.wifs = CamadasWifs(mapa, prefs)
        self.severo = CamadaSevero(mapa, prefs)
        self.rotas = CamadaRotas(mapa)
        self.animacao = CamadaAnimacao(mapa, prefs, self.radar, self.satelite, self.raios)

    def iniciar(self) -> None:
        """Liga as camadas conforme as preferências salvas.

        Chamar DEPOIS de montar o painel: algumas camadas escrevem em rótulos que o painel
        cria (ex.: a idade do vento, a janela dos raios). As que consultam a rede vão num
        ui.timer(0, ...) para não atrasar a abertura da página.
        """
        ui.timer(0, self.satelite.aplicar, once=True)
        self.radar.aplicar()
        self.vento.aplicar()
        self.raios.aplicar()
        for camada in wifs.CAMADAS:
            self.wifs.aplicar(camada.chave)
        ui.timer(0, lambda: self.radar.consultar_status(aplicar_filtro=True), once=True)
        ui.timer(0, self.severo.ligar, once=True)
        for definicao in espaco.CAMADAS:
            self.espaco.aplicar(definicao.chave)
        ui.timer(0, self.aerodromos.enviar, once=True)

    def ligar_relogios(self) -> None:
        """Atualizações periódicas de cada camada (os intervalos estão em config.py e nos módulos)."""
        ui.timer(config.AERONAVE_ENVIO_S, self.aeronaves.enviar)
        ui.timer(CamadaConflitos.REDESENHO_S, self.conflitos.desenhar)
        ui.timer(config.RADAR_ATUALIZA_S, self.radar.atualizar_horario)
        ui.timer(config.RADAR_ATUALIZA_S, self.raios.no_relogio)
        ui.timer(config.RADAR_ATUALIZA_S, self.radar.consultar_status)
        ui.timer(600, self.wifs.recarregar)
        ui.timer(config.SATELITE_ATUALIZA_S, self.satelite.atualizar)
        ui.timer(severo.CATALOGO_CACHE_S, lambda: self.severo.carregar_catalogo(forcar=True))
        ui.timer(60, self.vento.mostrar_idade)
        ui.timer(config.AERODROMOS_ATUALIZA_S, self.aerodromos.enviar)
        ui.timer(animacao.INTERVALO_S, self.animacao.tique)
