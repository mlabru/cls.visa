"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Camadas de previsão: tempo severo, WIFS em altitude, vento e o modelo WRF.

Criadas por `CamadasDaPagina` (camadas/__init__.py); os controles ficam em tela/painel_camadas.py.
"""
import asyncio
from datetime import UTC, datetime

from nicegui import ui

import config
import desenho
import modelo
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


class CamadaModelo:
    """Modelo WRF (arquivos netCDF locais): um campo por vez, como imagem, num horário previsto.

    Sempre a rodada mais recente da pasta (procurada de novo a cada config.MODELO_PROCURA_S).
    O horário não segue a linha do tempo da tela, que anima a observação no passado em passos
    de 10 min: é previsão, de hora em hora, escolhida no painel. Ao ligar, o mais perto de agora.
    """

    def __init__(self, mapa: ui.leaflet, prefs: dict) -> None:
        self.mapa = mapa
        self.prefs = prefs
        self.camada = None
        self.particulas = None                           # vento em partículas (leaflet-velocity)
        self.erro_vento = ''
        self.rodada: modelo.Rodada | None = None
        self.horarios: tuple[datetime, ...] = ()
        self.horario: datetime | None = None             # o horário previsto no mapa (ou a mostrar)
        self.erro = ''
        self.rotulo_info: ui.label | None = None         # preenchidos por tela/painel_camadas.py
        self.seletor_horario: ui.select | None = None
        self._pedido = UltimoPedido()
        self._pedido_vento = UltimoPedido()

    def campo(self) -> modelo.Campo:
        """O campo escolhido no painel."""
        return modelo.POR_CHAVE[self.prefs['modelo_campo']]

    def opcoes_horario(self) -> dict[str, str]:
        """Os horários previstos da rodada para o seletor: ISO -> '24/11 18Z (+42 h)'."""
        if self.rodada is None:
            return {}
        return {horario.isoformat(): modelo.rotulo_horario(horario, self.rodada.inicio) for horario in self.horarios}

    async def aplicar(self) -> None:
        """Liga ou desliga; ao ligar, procura a rodada mais recente e desenha o campo escolhido."""
        if not self.prefs['modelo_ligado']:
            self._pedido.novo()                          # descarta um desenho ainda a caminho
            self._pedido_vento.novo()
            self._tirar()
            self._tirar_particulas()
            self._informar()
            return
        await self._procurar_rodada()
        await self.desenhar()

    async def verificar(self) -> None:
        """Relógio: se chegou uma rodada nova na pasta, passa para ela."""
        if self.prefs['modelo_ligado'] and await self._procurar_rodada():
            await self.desenhar()

    async def _procurar_rodada(self) -> bool:
        """Lê a rodada mais recente da pasta e os seus horários (numa thread: lê o disco).

        Retorna:
            True se a rodada mudou.
        """
        try:
            rodadas = await asyncio.to_thread(modelo.listar_rodadas)
            nova = rodadas[0] if rodadas else None
            if nova == self.rodada:
                return False
            horarios = await asyncio.to_thread(modelo.horarios_da_rodada, nova) if nova else ()
            self.erro = ''
        except modelo.Indisponivel as erro:
            nova, horarios = None, ()
            self.erro = str(erro)
        self.rodada = nova
        self.horarios = horarios
        if self.horario not in horarios:
            self.horario = modelo.horario_mais_proximo(horarios, datetime.now(UTC))
        if self.seletor_horario:
            self.seletor_horario.set_options(self.opcoes_horario(),
                                             value=self.horario.isoformat() if self.horario else None)
        return True

    async def desenhar(self) -> None:
        """Redesenha a imagem do campo e o vento em partículas (horário ou nível mudou)."""
        await self.desenhar_imagem()
        await self.desenhar_vento()

    async def desenhar_imagem(self) -> None:
        """Prepara a imagem do campo, nível e horário escolhidos (numa thread) e a põe no mapa.

        A imagem é preparada aqui antes de ir para o navegador: assim o erro (nível que o
        arquivo não tem, variável ausente) aparece no painel, e o navegador já a encontra pronta.
        """
        prefs = self.prefs
        pedido = self._pedido.novo()
        if not prefs['modelo_ligado'] or self.rodada is None or self.horario is None:
            self._tirar()
            self._informar()
            return
        campo = self.campo()
        nivel_hpa = modelo.nivel_do_fl(prefs['modelo_fl']) if campo.por_nivel else None
        rodada, horario = self.rodada, self.horario
        try:
            await asyncio.to_thread(modelo.imagem, rodada, campo, horario, nivel_hpa)
            limites = modelo.limites_da_rodada(rodada)
            self.erro = ''
        except modelo.Indisponivel as erro:
            if self._pedido.vale(pedido):
                self.erro = str(erro)
                self._tirar()
                self._informar()
            return
        if not self._pedido.vale(pedido) or not prefs['modelo_ligado']:
            return
        url = modelo.url_imagem(rodada, campo, horario, prefs['modelo_fl'])
        if self.camada:   # troca a imagem sem tirar a camada (não pisca)
            self.camada.run_method('setUrl', url)
            self.camada.run_method('setBounds', limites)
        else:             # acima do satélite (zIndex 2) e abaixo do radar e dos raios (overlayPane)
            with self.mapa:
                self.camada = self.mapa.image_overlay(url=url, bounds=limites, options={
                    'opacity': prefs['modelo_opacidade'], 'pane': 'tilePane', 'zIndex': 3,
                    'interactive': False, 'attribution': 'WRF'})
        self._informar()

    async def desenhar_vento(self) -> None:
        """Vento em partículas no nível e horário escolhidos (o d1, numa grade de 0,25°).

        Troca os dados da camada existente (`setData`) em vez de recriá-la: as partículas
        continuam de onde estavam e o mapa não pisca.
        """
        prefs = self.prefs
        pedido = self._pedido_vento.novo()
        if not (prefs['modelo_ligado'] and prefs['modelo_vento'] and self.rodada and self.horario):
            self._tirar_particulas()
            self.erro_vento = ''
            self._informar()
            return
        try:
            dados = await asyncio.to_thread(modelo.vento_velocity, self.rodada, self.horario,
                                            modelo.nivel_do_fl(prefs['modelo_fl']))
            self.erro_vento = ''
        except modelo.Indisponivel as erro:
            if self._pedido_vento.vale(pedido):
                self.erro_vento = str(erro)
                self._tirar_particulas()
                self._informar()
            return
        if not self._pedido_vento.vale(pedido) or not prefs['modelo_ligado'] or not prefs['modelo_vento']:
            return
        if self.particulas:
            self.particulas.run_method('setData', dados)
        else:
            with self.mapa:
                self.particulas = self.mapa.generic_layer(name='velocityLayer', args=[{
                    'data': dados,
                    'displayValues': True,
                    'displayOptions': {'velocityType': 'Vento WRF', 'position': 'bottomleft',
                                       'emptyString': 'Sem dados', 'speedUnit': 'kt',
                                       'directionString': 'Direção', 'speedString': 'Velocidade'},
                    'maxVelocity': 100,     # a mesma escala de cores do vento dos arquivos (CamadaVento)
                    'velocityScale': 0.005,
                }])
        self._informar()

    async def escolher_horario(self, valor: str | None) -> None:
        """O seletor de horário mudou.

        Parâmetros:
            valor: o horário em ISO (chave de opcoes_horario); None quando o seletor esvazia.
        """
        if not valor:
            return
        horario = datetime.fromisoformat(valor)
        if horario != self.horario:
            self.horario = horario
            await self.desenhar()

    async def andar(self, passo: int) -> None:
        """Botões ◀ ▶: o horário previsto anterior ou seguinte.

        Parâmetros:
            passo: -1 (anterior) ou +1 (seguinte).
        """
        if self.horario not in self.horarios:
            return
        posicao = self.horarios.index(self.horario) + passo
        if not 0 <= posicao < len(self.horarios):
            return
        horario = self.horarios[posicao]
        if self.seletor_horario:      # o seletor avisa escolher_horario, que desenha
            self.seletor_horario.set_value(horario.isoformat())
        else:
            await self.escolher_horario(horario.isoformat())

    def mudar_opacidade(self) -> None:
        """Aplica a opacidade escolhida no painel."""
        if self.camada:
            self.camada.run_method('setOpacity', self.prefs['modelo_opacidade'])

    def _tirar(self) -> None:
        """Tira a imagem do mapa (se houver)."""
        if self.camada:
            self.mapa.remove_layer(self.camada)
            self.camada = None

    def _tirar_particulas(self) -> None:
        """Tira o vento em partículas do mapa (se houver)."""
        if self.particulas:
            self.mapa.remove_layer(self.particulas)
            self.particulas = None

    def _informar(self) -> None:
        """Escreve no painel o erro, a falta de rodada ou a rodada mostrada."""
        if self.erro or self.erro_vento:
            texto, cor = ' · '.join(erro for erro in (self.erro, self.erro_vento) if erro), 'negative'
        elif not self.prefs['modelo_ligado']:
            texto, cor = '', 'grey'
        elif self.rodada is None:
            texto, cor = f'Nenhuma rodada em {config.MODELO_DIR}', 'warning'
        else:
            texto, cor = f'rodada {self.rodada.inicio:%d/%m %H}Z · previsão do modelo, não observação', 'grey'
        escrever(self.rotulo_info, texto, f'text-caption text-{cor}')
