"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Cabecalho: a faixa do topo (criada por interface.pagina()).

  ☰  CLS Visa  [Convecção|Altitude|Terminal] [aeródromo]  personalizado   [FAB.ia ³] [Sonda]  BarraSaude

Os modos (modos.py) só mudam as preferências; os controles do painel, ligados a elas,
fazem o resto. Os botões FAB.ia (impacto) e Sonda chamam os painéis da gaveta da direita.
"""
from nicegui import ui

import aerodromos
import camadas
import impacto
import modos
import previsao
from tela.barra_saude import BarraSaude
from tela.gaveta import PainelImpacto, PainelSonda
from tela.mapa import ALTURA_CABECALHO, AreaDoMapa

# Cores dos botões do cabeçalho (cores do Quasar): ligado / desligado
COR_LIGADO = 'color=primary text-color=white'
COR_DESLIGADO = 'color=grey-9 text-color=grey-4'
COR_SONDA_LIGADA = 'color=amber-8 text-color=white'


class Cabecalho:
    """A faixa do topo: modos de trabalho, aeródromo do modo Terminal, botões FAB.ia e Sonda, saúde.

    Atributos:
        botoes_modo: um botão por modo de trabalho, pela chave do modo.
        seletor_terminal: o aeródromo do modo Terminal (só visível nesse modo).
        rotulo_modo: mostra "personalizado" quando nenhum modo corresponde às camadas ligadas.
        selo_impacto: o número no botão FAB.ia.
        botao_sonda: liga/desliga o modo em que o clique esquerdo sonda.
        saude: a BarraSaude, no canto direito.
    """

    def __init__(self, prefs: dict, area: AreaDoMapa, camadas_: camadas.CamadasDaPagina,
                 painel_esquerdo: ui.left_drawer, painel_impacto: PainelImpacto, painel_sonda: PainelSonda) -> None:
        """Cria a faixa do topo e os seus relógios.

        Parâmetros:
            prefs: preferências do usuário.
            area: a área do mapa (para aproximar no aeródromo do modo Terminal).
            camadas_: as camadas da página (usadas pela BarraSaude).
            painel_esquerdo: a gaveta da esquerda, que o botão ☰ mostra e esconde.
            painel_impacto: o painel FAB.ia, aberto pelo botão FAB.ia.
            painel_sonda: o painel da sonda, ligado pelo botão Sonda.
        """
        self.prefs = prefs
        self.mapa = area.mapa
        self.sonda = painel_sonda
        self.botoes_modo: dict[str, ui.button] = {}
        self._modo_mostrado: object = object()       # valor impossível: força a primeira pintura

        with ui.header().classes('items-center no-wrap gap-2 q-px-sm cabecalho') \
                .style(f'height: {ALTURA_CABECALHO}px'):
            ui.button(icon='menu', on_click=painel_esquerdo.toggle).props('flat dense round color=grey-4') \
                .tooltip('Mostrar / esconder o painel')
            ui.label('CLS Visa').classes('text-subtitle1 text-weight-bold q-mr-md')
            self._criar_botoes_modo()
            self._criar_seletor_terminal()
            self.rotulo_modo = ui.label().classes('text-caption text-grey-6')
            ui.space()
            self._criar_botao_fabia(painel_impacto)
            self.botao_sonda = ui.button('Sonda', icon='ads_click', on_click=self._alternar_sonda) \
                .props(f'dense no-caps size=sm unelevated {COR_DESLIGADO}') \
                .tooltip('Ligada: clique no mapa para ver tudo sobre o ponto. '
                         'Clique direito no mapa funciona sempre.')
            self.saude = BarraSaude(prefs, camadas_)

        # Relógios fora do ui.header (tela/__init__.py, regras comuns)
        self.saude.ligar_relogio()
        ui.timer(previsao.REDESENHO_S, self.contar_impacto)
        ui.timer(5, self._atualizar_terminal)
        ui.timer(0.5, self.pintar_modos)      # o modo muda também quando a pessoa mexe no painel

    def _criar_botoes_modo(self) -> None:
        """Um botão por modo de trabalho (Convecção, Altitude, Terminal), lado a lado."""
        with ui.button_group().props('unelevated'):
            for modo in modos.MODOS:
                botao = ui.button(modo.titulo, icon=modo.icone, on_click=lambda modo=modo: self.escolher_modo(modo))
                botao.props('dense no-caps size=sm').tooltip(modo.descricao).mark(f'modo-{modo.chave}')
                self.botoes_modo[modo.chave] = botao

    def _criar_seletor_terminal(self) -> None:
        """O seletor do aeródromo do modo Terminal (só aparece nesse modo; ver `pintar_modos`)."""
        self.seletor_terminal = ui.select(self._opcoes_terminal(), with_input=True) \
            .bind_value(self.prefs, 'terminal').props('dense outlined options-dense dark') \
            .classes('seletor-terminal')
        self.seletor_terminal.on_value_change(self._ao_trocar_terminal)

    def _criar_botao_fabia(self, painel_impacto: PainelImpacto) -> None:
        """O botão FAB.ia, com o selo do número de aeródromos e TMAs afetados.

        Parâmetros:
            painel_impacto: o painel que o botão abre e fecha.
        """
        with ui.button('FAB.ia', icon='warning', on_click=painel_impacto.alternar) \
                .props(f'dense no-caps size=sm unelevated {COR_DESLIGADO}') \
                .tooltip('Aeródromos, TMAs e tráfego afetados agora').mark('botao-fabia'):
            self.selo_impacto = ui.badge('', color='red').props('rounded').classes('q-ml-xs')

    # --- Modos de trabalho ---------------------------------------------------------

    def escolher_modo(self, modo: modos.Modo) -> None:
        """Aplica um modo de trabalho (clique no botão do modo).

        Parâmetros:
            modo: o modo escolhido.
        """
        modos.aplicar(modo, self.prefs)       # os controles ligados às preferências fazem o resto
        if modo.zoom_terminal:
            self.aproximar_terminal()

    def pintar_modos(self) -> None:
        """Destaca o modo cujas camadas são exatamente as ligadas (ou mostra "personalizado").

        A "assinatura" deste relógio é o modo ativo (DECISOES.md, 5.3).
        """
        atual = modos.ativo(self.prefs)
        if atual == self._modo_mostrado:      # roda a cada 0,5 s: só mexe na tela se mudou
            return
        self._modo_mostrado = atual
        for chave, botao in self.botoes_modo.items():
            botao.props(COR_LIGADO if chave == atual else COR_DESLIGADO)
        self.rotulo_modo.set_text('' if atual else 'personalizado')
        self.seletor_terminal.set_visibility(atual == 'terminal')

    # --- Aeródromo do modo Terminal --------------------------------------------------

    def _opcoes_terminal(self) -> dict[str, str]:
        """As opções do seletor: "ICAO – nome" por código ICAO.

        Retorna:
            As opções em ordem de ICAO; se a lista de aeródromos ainda não chegou, só o
            aeródromo já escolhido.
        """
        lista = sorted(aerodromos.situacao.aerodromos, key=lambda aerodromo: aerodromo.icao)
        opcoes = {aerodromo.icao: f'{aerodromo.icao} – {aerodromo.nome}' for aerodromo in lista}
        return opcoes or {self.prefs['terminal']: self.prefs['terminal']}

    def _atualizar_terminal(self) -> None:
        """A lista de aeródromos chega depois da página: completa o seletor quando ela vier."""
        if len(self.seletor_terminal.options) <= 1:
            self.seletor_terminal.set_options(self._opcoes_terminal(), value=self.prefs['terminal'])

    def _ao_trocar_terminal(self) -> None:
        """Aproxima o mapa só quando a pessoa escolhe outro aeródromo.

        O seletor só aparece no modo Terminal; a lista de aeródromos chegando depois também
        "muda" o valor (com o seletor escondido), e isso não deve mover o mapa.
        """
        if self.seletor_terminal.visible:
            self.aproximar_terminal()

    def aproximar_terminal(self) -> None:
        """Centra o mapa no aeródromo do modo Terminal (se ele estiver na lista)."""
        icao = self.prefs['terminal']
        alvo = next((aerodromo for aerodromo in aerodromos.situacao.aerodromos if aerodromo.icao == icao), None)
        if alvo:
            self.mapa.set_center((alvo.lat, alvo.lon))
            self.mapa.set_zoom(modos.ZOOM_TERMINAL)

    # --- Botões FAB.ia e Sonda -------------------------------------------------------

    def contar_impacto(self) -> None:
        """O número no botão FAB.ia: aeródromos e TMAs com impacto alto ou médio."""
        situacao = impacto.situacao
        afetados = situacao.aerodromos + situacao.tmas
        graves = sum(1 for item in afetados if item.nivel in (impacto.ALTO, impacto.MEDIO))
        self.selo_impacto.set_text(str(graves))
        self.selo_impacto.set_visibility(graves > 0)

    def _alternar_sonda(self) -> None:
        """Botão Sonda: liga/desliga o modo no painel e pinta o próprio botão."""
        ligada = self.sonda.alternar_modo()
        self.botao_sonda.props(COR_SONDA_LIGADA if ligada else COR_DESLIGADO)
