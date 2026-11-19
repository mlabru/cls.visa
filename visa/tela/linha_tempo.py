"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

LinhaDoTempo: a barra embaixo do mapa que controla a animação de satélite, radar e raios.
Criada por interface.pagina().

Só os controles. Quem prepara as imagens e troca os quadros é a CamadaAnimacao
(camadas/animacao_mapa.py); ela avisa esta barra pelo `ao_mudar` a cada passo.
"""
from nicegui import ui

import animacao
import camadas
from tela.mapa import AreaDoMapa

TEXTO_PARADA = 'Animar satélite, radar e raios'


class LinhaDoTempo:
    """Tocar/pausar, passo anterior/seguinte, régua, horário, janela e "ao vivo".

    Atributos:
        anim: a CamadaAnimacao da página (quem de fato anima).
    """

    def __init__(self, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        """Cria os controles na barra do tempo e se registra para ser avisada pela animação.

        Parâmetros:
            area: a área do mapa (onde fica a barra do tempo).
            prefs: preferências do usuário (a janela da animação, em min).
            camadas_: as camadas da página (a animação).
        """
        self.anim = anim = camadas_.animacao
        with area.barra_tempo:
            self.botao_tocar = ui.button(icon='play_arrow', on_click=anim.alternar) \
                .props('flat dense round color=white').tooltip('Animar / pausar').mark('animacao-tocar')
            self.botao_antes = ui.button(icon='skip_previous', on_click=lambda: anim.andar(-1)) \
                .props('flat dense round size=sm color=grey-4').tooltip('Passo anterior (10 min)')
            self.botao_depois = ui.button(icon='skip_next', on_click=lambda: anim.andar(1)) \
                .props('flat dense round size=sm color=grey-4').tooltip('Próximo passo (10 min)')
            self.regua = ui.slider(min=0, max=1, value=0, on_change=lambda evento: anim.ir_para(int(evento.value))) \
                .props('dense color=amber').classes('linha-tempo-regua')
            self.rotulo = ui.label(TEXTO_PARADA).classes('linha-tempo-rotulo')
            ui.select(animacao.JANELAS).bind_value(prefs, 'animacao_janela_min') \
                .props('dense borderless dark options-dense').classes('linha-tempo-janela') \
                .tooltip('Quanto tempo para trás')
            self.botao_vivo = ui.button('ao vivo', icon='sensors', on_click=lambda: anim.parar()) \
                .props('flat dense no-caps no-wrap size=sm color=green-4')
        anim.ao_mudar = self.atualizar
        self.atualizar()

    def atualizar(self) -> None:
        """Chamado pela animação a cada passo, ao tocar/pausar e ao começar/parar."""
        anim = self.anim
        self.botao_tocar.props(f"icon={'pause' if anim.tocando else 'play_arrow'}")
        for elemento in (self.botao_antes, self.botao_depois, self.regua, self.botao_vivo):
            elemento.set_visibility(anim.ativa)       # parada, só o "tocar" e a janela aparecem
        if not anim.ativa:
            self.rotulo.set_text(anim.aviso or TEXTO_PARADA)
            return
        self.regua.props(f'max={len(anim.passos) - 1}')
        if self.regua.value != anim.indice:        # sem isso, mover a régua viraria um novo pedido
            self.regua.set_value(anim.indice)
        texto = animacao.rotulo(anim.passos[anim.indice])
        if anim.aviso:
            texto += f' · {anim.aviso}'
        self.rotulo.set_text(texto)
