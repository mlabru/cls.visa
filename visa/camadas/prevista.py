"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de previsão: tempo severo, WIFS em altitude e vento.

Criadas por `CamadasDaPagina` (camadas/__init__.py); os controles ficam em tela/painel_camadas.py.
"""
from datetime import datetime

from nicegui import ui

import config
import desenho
import severo
import vento
import wifs

from .ajudantes import UltimoPedido, escrever


class CamadaSevero:
    """Tempo severo previsto: escolhas em cascata (modelo → resolução → tipo → rodada → validade)."""

    NIVEIS = ('origem', 'resolucao', 'tipo', 'rodada', 'validade')

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camadas: list = []                          # L.geoJSON (feições) e L.rectangle (cobertura)
        self.feicoes: list[dict] = []                    # última resposta do WFS
        self.tipo = 'area'
        self.erro = ''                                   # última falha do GeoServer (barra de saúde)
        self.catalogo: list[dict] = []                   # previsões disponíveis no GeoServer
        self.escolha = dict.fromkeys(self.NIVEIS)        # valores atuais (o painel se liga aqui)
        self.seletores: dict[str, ui.select] = {}          # preenchidos por tela/painel_camadas.py
        self.rotulo_situacao: ui.label | None = None
        self._pedido = UltimoPedido()

    def _opcoes(self, nivel: str) -> dict[str, str]:
        """Lista as opções de um nível da cascata, dadas as escolhas dos níveis anteriores.

        Parâmetros:
            nivel: um de `NIVEIS`.

        Retorna:
            valor -> texto mostrado no seletor.
        """
        escolha = self.escolha
        catalogo = self.catalogo
        if nivel == 'origem':
            return {modelo: modelo.upper() for modelo in severo.modelos(catalogo)}
        if nivel == 'resolucao':
            return {resolucao: f'{resolucao} km' for resolucao in severo.resolucoes(catalogo, escolha['origem'])}
        if nivel == 'tipo':
            tipos = severo.tipos(catalogo, escolha['origem'], escolha['resolucao'])
            return {tipo: severo.TIPOS.get(tipo, tipo) for tipo in tipos}
        if nivel == 'rodada':
            rodadas = severo.rodadas(catalogo, escolha['origem'], escolha['resolucao'], escolha['tipo'])
            return {rodada: severo.rotulo(rodada) for rodada in rodadas}
        validades = severo.validades(catalogo, escolha['origem'], escolha['resolucao'], escolha['tipo'],
                                     escolha['rodada'])
        return {validade: severo.rotulo(validade) for validade in validades}

    def _cascata(self, a_partir_de: str, preferidos: dict | None = None) -> None:
        """Recalcula as opções a partir de um nível, mantendo o valor atual quando ainda existe.

        Parâmetros:
            a_partir_de: o primeiro nível a recalcular (os seguintes também são).
            preferidos: nível -> valor a escolher se existir (ex.: a previsão mais recente).
        """
        preferidos = preferidos or {}
        for nivel in self.NIVEIS[self.NIVEIS.index(a_partir_de):]:
            opcoes = self._opcoes(nivel)
            atual = preferidos.get(nivel, self.escolha[nivel])
            if atual not in opcoes:
                atual = _valor_padrao(nivel, list(opcoes))
            self.escolha[nivel] = atual          # antes do select: assim mudou() reconhece e ignora
            if nivel in self.seletores:
                self.seletores[nivel].set_options(opcoes, value=atual)

    async def ligar(self) -> None:
        """Liga (carrega o catálogo e desenha) ou desliga a camada, conforme a preferência."""
        if not self.prefs['severo_ligado']:
            self._pedido.novo()
            self.feicoes = []
            self.redesenhar()
            self._informar('')
            return
        await self.carregar_catalogo()

    async def carregar_catalogo(self, forcar: bool = False) -> None:
        """Consulta as previsões disponíveis, refaz a cascata e desenha a escolhida.

        Na primeira vez, abre na previsão mais recente.

        Parâmetros:
            forcar: se True, ignora o cache do catálogo (relógio periódico).
        """
        if not self.prefs['severo_ligado']:
            return
        self._informar('consultando o GeoServer...')
        try:
            self.catalogo = await severo.catalogo(forcar)
            self.erro = ''
        except severo.Indisponivel as erro:
            self.erro = str(erro)
            self._informar(self.erro, 'negative')
            return
        if not self.catalogo:
            self._informar('nenhuma previsão disponível', 'warning')
            return
        preferidos = {}
        if self.escolha['origem'] is None:   # primeira vez: abre na previsão mais recente
            recente = severo.mais_recente(self.catalogo)
            if recente:
                preferidos = {'origem': recente['origem'],
                              'resolucao': severo.resolucao_texto(recente.get('resolucao_km')),
                              'tipo': recente.get('tipo'), 'rodada': recente['rodada'],
                              'validade': recente['validade']}
        self._cascata('origem', preferidos)
        await self.desenhar()

    async def mudou(self, nivel: str) -> None:
        """Trata a troca de um seletor pela pessoa: refaz os níveis seguintes e redesenha.

        Parâmetros:
            nivel: o nível cujo seletor mudou.
        """
        valor = self.seletores[nivel].value
        if valor == self.escolha[nivel]:   # mudança feita pelo próprio Python (_cascata/andar)
            return
        self.escolha[nivel] = valor
        if nivel != 'validade':
            self._cascata(self.NIVEIS[self.NIVEIS.index(nivel) + 1])
        await self.desenhar()

    async def andar(self, passo: int) -> None:
        """Passa para a validade anterior ou seguinte (botões ◀ ▶); para nas pontas.

        Parâmetros:
            passo: -1 (anterior) ou +1 (seguinte).
        """
        validades = list(self._opcoes('validade'))
        if self.escolha['validade'] not in validades:
            return
        posicao = validades.index(self.escolha['validade']) + passo
        if 0 <= posicao < len(validades):
            self.escolha['validade'] = validades[posicao]
            self.seletores['validade'].set_value(validades[posicao])
            await self.desenhar()

    async def desenhar(self) -> None:
        """Busca no WFS as feições da escolha atual e as desenha."""
        escolha = self.escolha
        try:
            cql = severo.filtro_cql(escolha['origem'], escolha['resolucao'], escolha['tipo'], escolha['rodada'],
                                    escolha['validade'])
        except (TypeError, ValueError):
            self._informar('escolha incompleta', 'warning')
            return
        pedido = self._pedido.novo()
        self._informar('carregando...')
        try:
            feicoes = await severo.feicoes(cql)
            self.erro = ''
        except severo.Indisponivel as erro:
            self.erro = str(erro)
            self._informar(self.erro, 'negative')
            return
        if not self._pedido.vale(pedido):   # o usuário já pediu outra coisa enquanto esta chegava
            return
        self.feicoes = feicoes
        self.tipo = escolha['tipo']
        self.redesenhar()
        texto, cor = severo.situacao(escolha['validade'])
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
        """Aplica cor, opacidade e raio escolhidos no painel."""
        if self.tipo == 'ponto':   # o raio muda a forma dos círculos: refaz
            self.redesenhar()
        elif self.camadas:         # áreas: só troca cor e opacidade
            self.camadas[0].run_method('setStyle', desenho.severo_estilo(self.prefs['severo_estilo'], self.tipo))

    def _informar(self, texto: str, cor: str = 'grey') -> None:
        """Escreve a situação no painel (cor: nome de cor do Quasar, ex.: 'negative')."""
        escrever(self.rotulo_situacao, texto, f'text-caption text-{cor}')


def _valor_padrao(nivel: str, valores: list[str]) -> str | None:
    """Escolhe o valor de um nível quando o atual não existe mais.

    Como no original: a validade mais recente (a última) e, nos outros níveis, a primeira opção
    (as rodadas já vêm da mais recente para a mais antiga).

    Parâmetros:
        nivel: um de `CamadaSevero.NIVEIS`.
        valores: as opções do nível, na ordem do seletor.

    Retorna:
        O valor escolhido, ou None se não há opções.
    """
    if not valores:
        return None
    if nivel == 'validade':
        return valores[-1]
    return valores[0]


class CamadasWifs:
    """WIFS: uma camada WMS por variável, todas no mesmo nível de voo."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camadas: dict[str, object] = {}

    def aplicar(self, chave: str) -> None:
        """Liga ou desliga a camada de uma variável do WIFS.

        Parâmetros:
            chave: a variável (`wifs.CAMADAS[...].chave`).
        """
        if chave in self.camadas:
            self.mapa.remove_layer(self.camadas.pop(chave))
        if self.prefs['wifs'][chave]:
            with self.mapa:
                self.camadas[chave] = self.mapa.wms_layer(
                    url_template='/radar/wms', options=wifs.opcoes_wms(chave, self.prefs['wifs_fl']))

    def trocar_nivel(self) -> None:
        """Aplica o nível de voo escolhido em todas as camadas ligadas, sem recriá-las."""
        for camada in self.camadas.values():
            camada.run_method('setParams', {'ELEVATION': str(self.prefs['wifs_fl'])})

    def recarregar(self) -> None:
        """TIME=current: o GeoServer devolve a previsão vigente; basta pedir os tiles de novo."""
        for camada in self.camadas.values():
            camada.run_method('redraw')


class CamadaVento:
    """Vento em altitude (partículas animadas do leaflet-velocity), lido dos arquivos locais."""

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camada = None
        self.referencia: datetime | None = None
        self.rotulo_aviso: ui.label | None = None       # preenchidos por tela/painel_camadas.py
        self.seletor_horario: ui.select | None = None
        if prefs['vento_horario'] not in self.opcoes_horario():
            prefs['vento_horario'] = 'recente'

    def opcoes_horario(self) -> dict[str, str]:
        """Lista os horários com arquivo de vento no nível escolhido.

        Retorna:
            valor -> texto do seletor; sempre começa por 'recente' ('Mais recente').
        """
        disponiveis = vento.horarios_disponiveis(vento.listar_arquivos(), self.prefs['vento_fl'])
        opcoes = {'recente': 'Mais recente'}
        for horario in disponiveis:
            opcoes[horario.isoformat()] = horario.strftime('%d/%m %H:%MZ')
        return opcoes

    def trocar_nivel(self) -> None:
        """Troca o nível de voo: volta o horário para 'recente' e recria a camada."""
        if self.seletor_horario:
            self.seletor_horario.set_options(self.opcoes_horario(), value='recente')
        self.aplicar()

    def aplicar(self) -> None:
        """Recria a camada com o nível e o horário escolhidos (ou só a tira, se desligada)."""
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None
        self.referencia = None
        if self.prefs['vento_ligado']:
            self._desenhar()
        self.mostrar_idade()

    def _desenhar(self) -> None:
        """Escolhe o arquivo de vento, carrega o recorte e cria a camada de partículas."""
        prefs = self.prefs
        escolhido = None
        if prefs['vento_horario'] != 'recente':
            escolhido = datetime.fromisoformat(prefs['vento_horario'])
        arquivo = vento.escolher(vento.listar_arquivos(), prefs['vento_fl'], escolhido)
        if arquivo is None:
            ui.notify(f"Sem arquivo de vento para FL{prefs['vento_fl']:03d}", type='warning')
            return
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

    def mostrar_idade(self) -> None:
        """Mostra no painel o nível, o horário de referência e a idade dos dados (esconde sem camada)."""
        if not self.rotulo_aviso:
            return
        if self.referencia is None:
            self.rotulo_aviso.set_visibility(False)
            return
        texto, cor = vento.idade(self.referencia)
        nivel = self.prefs['vento_fl']
        pressao_pa = config.VENTO_FL_PARA_PA[nivel]
        escrever(self.rotulo_aviso, f'FL{nivel:03d} / {pressao_pa // 100} hPa · '
                                    f'ref. {self.referencia:%d/%m/%Y %H:%MZ} · {texto}', f'text-{cor} text-caption')
        self.rotulo_aviso.set_visibility(True)
