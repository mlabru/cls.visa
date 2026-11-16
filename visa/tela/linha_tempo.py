# -*- coding: utf-8 -*-
"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

LinhaDoTempo: a barra embaixo do mapa que controla a animação de satélite, radar e raios.

Só os controles. Quem prepara as imagens e troca os quadros é a CamadaAnimacao
(camadas/animacao_mapa.py); ela avisa esta barra pelo `ao_mudar` a cada passo.
"""
from nicegui import ui

import animacao
import camadas
from tela.mapa import AreaDoMapa

TEXTO_PARADA = 'Animar satélite, radar e raios'


class LinhaDoTempo:

    def __init__(self, area: AreaDoMapa, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        self.anim = a = camadas_.animacao
        with area.barra_tempo:
            self.botao_tocar = ui.button(icon='play_arrow', on_click=a.alternar) \
                .props('flat dense round color=white').tooltip('Animar / pausar')
            self.botao_antes = ui.button(icon='skip_previous', on_click=lambda: a.andar(-1)) \
                .props('flat dense round size=sm color=grey-4').tooltip('Passo anterior (10 min)')
            self.botao_depois = ui.button(icon='skip_next', on_click=lambda: a.andar(1)) \
                .props('flat dense round size=sm color=grey-4').tooltip('Próximo passo (10 min)')
            self.regua = ui.slider(min=0, max=1, value=0, on_change=lambda e: a.ir_para(int(e.value))) \
                .props('dense color=amber').classes('linha-tempo-regua')
            self.rotulo = ui.label(TEXTO_PARADA).classes('linha-tempo-rotulo')
            ui.select(animacao.JANELAS).bind_value(prefs, 'animacao_janela_min') \
                .props('dense borderless dark options-dense').classes('linha-tempo-janela') \
                .tooltip('Quanto tempo para trás')
            self.botao_vivo = ui.button('ao vivo', icon='sensors', on_click=lambda: a.parar()) \
                .props('flat dense no-caps no-wrap size=sm color=green-4')
        a.ao_mudar = self.atualizar
        self.atualizar()

    def atualizar(self) -> None:
        """Chamado pela animação a cada passo, ao tocar/pausar e ao começar/parar."""
        a = self.anim
        self.botao_tocar.props(f"icon={'pause' if a.tocando else 'play_arrow'}")
        for elemento in (self.botao_antes, self.botao_depois, self.regua, self.botao_vivo):
            elemento.set_visibility(a.ativa)       # parada, só o "tocar" e a janela aparecem
        if a.ativa:
            self.regua.props(f'max={len(a.passos) - 1}')
            if self.regua.value != a.indice:        # sem isso, mover a régua viraria um novo pedido
                self.regua.set_value(a.indice)
            texto = animacao.rotulo(a.passos[a.indice])
            self.rotulo.set_text(texto + (f' · {a.aviso}' if a.aviso else ''))
        else:
            self.rotulo.set_text(a.aviso or TEXTO_PARADA)
