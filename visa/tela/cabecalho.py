"""
Cabecalho: a faixa do topo.

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


class Cabecalho:

    def __init__(self, prefs: dict, area: AreaDoMapa, camadas_: camadas.CamadasDaPagina,
                 painel_esquerdo: ui.left_drawer, painel_impacto: PainelImpacto, painel_sonda: PainelSonda) -> None:
        self.prefs, self.mapa, self.sonda = prefs, area.mapa, painel_sonda
        self.botoes_modo: dict[str, ui.button] = {}
        self._modo_mostrado: object = object()       # valor impossível: força a primeira pintura

        with ui.header().classes('items-center no-wrap gap-2 q-px-sm cabecalho') \
                .style(f'height: {ALTURA_CABECALHO}px'):
            ui.button(icon='menu', on_click=painel_esquerdo.toggle).props('flat dense round color=grey-4') \
                .tooltip('Mostrar / esconder o painel')
            ui.label('CLS Visa').classes('text-subtitle1 text-weight-bold q-mr-md')
            with ui.button_group().props('unelevated'):
                for modo in modos.MODOS:
                    self.botoes_modo[modo.chave] = ui.button(modo.titulo, icon=modo.icone,
                                                             on_click=lambda m=modo: self.escolher_modo(m)) \
                        .props('dense no-caps size=sm').tooltip(modo.descricao)
            # Só aproxima quando a pessoa escolhe (o seletor só aparece no modo Terminal);
            # a lista de aeródromos chegando depois também "muda" o valor, e isso não deve mover o mapa
            self.seletor_terminal = ui.select(self._opcoes_terminal(), with_input=True) \
                .bind_value(prefs, 'terminal').props('dense outlined options-dense dark') \
                .classes('seletor-terminal')
            self.seletor_terminal.on_value_change(
                lambda: self.aproximar_terminal() if self.seletor_terminal.visible else None)
            self.rotulo_modo = ui.label().classes('text-caption text-grey-6')
            ui.space()
            with ui.button('FAB.ia', icon='warning', on_click=painel_impacto.alternar) \
                    .props(f'dense no-caps size=sm unelevated {COR_DESLIGADO}') \
                    .tooltip('Aeródromos, TMAs e tráfego afetados agora'):
                self.selo_impacto = ui.badge('', color='red').props('rounded').classes('q-ml-xs')
            self.botao_sonda = ui.button('Sonda', icon='ads_click', on_click=self._alternar_sonda) \
                .props(f'dense no-caps size=sm unelevated {COR_DESLIGADO}') \
                .tooltip('Ligada: clique no mapa para ver tudo sobre o ponto. '
                         'Clique direito no mapa funciona sempre.')
            self.saude = BarraSaude(prefs, camadas_)

        self.saude.ligar_relogio()
        ui.timer(previsao.REDESENHO_S, self.contar_impacto)
        ui.timer(5, self._atualizar_terminal)
        ui.timer(0.5, self.pintar_modos)      # o modo muda também quando a pessoa mexe no painel

    # --- Modos de trabalho ---------------------------------------------------------

    def escolher_modo(self, modo: modos.Modo) -> None:
        modos.aplicar(modo, self.prefs)       # os controles ligados às preferências fazem o resto
        if modo.zoom_terminal:
            self.aproximar_terminal()

    def pintar_modos(self) -> None:
        """Destaca o modo cujas camadas são exatamente as ligadas (ou mostra "personalizado")."""
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
        lista = sorted(aerodromos.situacao.aerodromos, key=lambda a: a.icao)
        opcoes = {a.icao: f'{a.icao} – {a.nome}' for a in lista}
        return opcoes or {self.prefs['terminal']: self.prefs['terminal']}

    def _atualizar_terminal(self) -> None:
        """A lista de aeródromos chega depois da página: completa o seletor quando ela vier."""
        if len(self.seletor_terminal.options) <= 1:
            self.seletor_terminal.set_options(self._opcoes_terminal(), value=self.prefs['terminal'])

    def aproximar_terminal(self) -> None:
        alvo = next((a for a in aerodromos.situacao.aerodromos if a.icao == self.prefs['terminal']), None)
        if alvo:
            self.mapa.set_center((alvo.lat, alvo.lon))
            self.mapa.set_zoom(modos.ZOOM_TERMINAL)

    # --- Botões FAB.ia e Sonda -------------------------------------------------------

    def contar_impacto(self) -> None:
        """O número no botão FAB.ia: aeródromos e TMAs com impacto alto ou médio."""
        s = impacto.situacao
        graves = sum(1 for i in s.aerodromos + s.tmas if i.nivel in (impacto.ALTO, impacto.MEDIO))
        self.selo_impacto.set_text(str(graves))
        self.selo_impacto.set_visibility(graves > 0)

    def _alternar_sonda(self) -> None:
        ligada = self.sonda.alternar_modo()
        self.botao_sonda.props('color=amber-8 text-color=white' if ligada else COR_DESLIGADO)
