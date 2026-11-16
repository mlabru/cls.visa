"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de previsão: tempo severo, WIFS em altitude e vento.
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
        self.mapa, self.prefs = mapa, prefs
        self.camadas: list = []                          # L.geoJSON (feições) e L.rectangle (cobertura)
        self.feicoes: list[dict] = []                    # última resposta do WFS
        self.tipo = 'area'
        self.erro = ''                                   # última falha do GeoServer (barra de saúde)
        self.cat: list[dict] = []
        self.escolha = dict.fromkeys(self.NIVEIS)        # valores atuais (o painel se liga aqui)
        self.seletores: dict[str, ui.select] = {}          # preenchidos por tela/painel_camadas.py
        self.rotulo_situacao: ui.label | None = None
        self._pedido = UltimoPedido()

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
            if nivel in self.seletores:
                self.seletores[nivel].set_options(opcoes, value=atual)

    async def ligar(self) -> None:
        if not self.prefs['severo_ligado']:
            self._pedido.novo()
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
            self.erro = str(erro)
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
        valor = self.seletores[nivel].value
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
            self.seletores['validade'].set_value(validades[i])
            await self.desenhar()

    async def desenhar(self) -> None:
        e = self.escolha
        try:
            cql = severo.filtro_cql(e['origem'], e['resolucao'], e['tipo'], e['rodada'], e['validade'])
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
        escrever(self.rotulo_situacao, texto, f'text-caption text-{cor}')


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


class CamadaVento:
    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa, self.prefs, self.camada = mapa, prefs, None
        self.referencia: datetime | None = None
        self.rotulo_aviso: ui.label | None = None       # preenchidos por tela/painel_camadas.py
        self.seletor_horario: ui.select | None = None
        if prefs['vento_horario'] not in self.opcoes_horario():
            prefs['vento_horario'] = 'recente'

    def opcoes_horario(self) -> dict[str, str]:
        disponiveis = vento.horarios_disponiveis(vento.listar_arquivos(), self.prefs['vento_fl'])
        opcoes = {'recente': 'Mais recente'}
        opcoes.update({h.isoformat(): h.strftime('%d/%m %H:%MZ') for h in disponiveis})
        return opcoes

    def trocar_nivel(self) -> None:
        if self.seletor_horario:
            self.seletor_horario.set_options(self.opcoes_horario(), value='recente')
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
        if not self.rotulo_aviso:
            return
        if self.referencia is None:
            self.rotulo_aviso.set_visibility(False)
            return
        texto, cor = vento.idade(self.referencia)
        pa = config.VENTO_FL_PARA_PA[self.prefs['vento_fl']]
        escrever(self.rotulo_aviso, f"FL{self.prefs['vento_fl']:03d} / {pa // 100} hPa · "
                                    f"ref. {self.referencia:%d/%m/%Y %H:%MZ} · {texto}", f'text-{cor} text-caption')
        self.rotulo_aviso.set_visibility(True)
