"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Peças pequenas da tela, usadas por várias áreas.
"""
from contextlib import contextmanager

from nicegui import ui

import radar


def grupo(titulo: str) -> None:
    """Título de um grupo de cartões no painel (Observação, Previsão...)."""
    ui.label(titulo).classes('text-overline text-grey-6 q-mt-md q-mb-xs q-px-sm')


@contextmanager
def cartao(titulo: str, icone: str, prefs: dict | None = None, chave: str | None = None, on_change=None):
    """Cartão recolhível: ícone, título e (opcional) a chave liga/desliga no cabeçalho.

    Uso:  with cartao('Radar', 'radar', prefs, 'radar_ligado', camada.aplicar): ...controles...

    Parâmetros:
        titulo: o nome do cartão.
        icone: nome do ícone (Material Icons).
        prefs: preferências do usuário (só com `chave`).
        chave: a preferência ligada à chave liga/desliga; None para cartão sem chave.
        on_change: chamado quando a chave muda.

    Retorna (no `with`):
        O ui.expansion; o que for criado dentro do `with` vai para o corpo do cartão.
    """
    with ui.expansion().classes('w-full cartao').props('dense') as expansao:
        with expansao.add_slot('header'), ui.row().classes('w-full items-center no-wrap gap-2'):
            ui.icon(icone, size='sm').classes('text-grey-5')
            ui.label(titulo).classes('col text-body2 text-weight-medium')
            if chave:
                # O clique na chave não deve abrir/fechar o cartão
                ui.switch(on_change=on_change).bind_value(prefs, chave).props('dense') \
                    .on('click', js_handler='(e) => e.stopPropagation()')
        with ui.column().classes('w-full gap-1 q-px-sm q-pb-sm'):
            yield expansao


def secao(titulo: str, icone: str) -> None:
    """Título de uma seção dentro da gaveta da direita (Radar, Raios, Aeródromos...)."""
    with ui.row().classes('items-center gap-1 q-mt-sm'):
        ui.icon(icone, size='xs').classes('text-grey-5')
        ui.label(titulo).classes('legenda-titulo')


def bolinhas(itens: list[tuple[str, str]], linha: bool = False) -> None:
    """Legenda de cores: bolinha + texto, em coluna ou (linha=True) lado a lado."""
    conteiner = ui.row().classes('gap-x-3 gap-y-0') if linha else ui.column().classes('gap-0')
    with conteiner:
        for cor, texto in itens:
            with ui.row().classes('items-center gap-1 no-wrap'):
                ui.icon('circle', size='10px').style(f'color: {cor}')
                ui.label(texto).classes('legenda-texto')


def imagem_legenda(url: str) -> None:
    """Imagem de legenda no tamanho natural (o ui.image reserva uma área fixa, grande demais)."""
    ui.element('img').props(f'src="{url}" alt=""').style('max-width: 100%; display: block')


def barra_cores(paleta: tuple[tuple[float, str], ...], unidade: str) -> None:
    """Escala de cores de uma paleta (limite inferior de cada faixa, cor), com os limites embaixo.

    Parâmetros:
        paleta: (limite, cor) em ordem crescente, como em modelo.Campo.
        unidade: vai depois do último número.
    """
    with ui.row().classes('w-full no-wrap gap-0'):
        for posicao, (limite, cor) in enumerate(paleta):
            rotulo = f'{limite:g}' if posicao % 2 == 0 else ''     # um número sim, outro não: cabe
            if posicao == len(paleta) - 1:
                rotulo = f'{limite:g} {unidade}'
            with ui.column().classes('col items-center gap-0'):
                ui.element('div').style(f'background:{cor}; height:8px; width:100%')
                ui.label(rotulo).classes('legenda-texto').style('font-size:9px; white-space:nowrap')


def barra_dbz(dbz_min: int) -> None:
    """Escala de cores do radar; as cores abaixo do limiar ficam apagadas (o filtro as esconde)."""
    with ui.row().classes('w-full no-wrap gap-0'):
        for dbz, cor in radar.LEGENDA:
            opacidade = 0.15 if dbz < dbz_min else 1
            rotulo = str(dbz) if dbz % 10 == 0 else ''      # números só de 10 em 10 dBZ
            with ui.column().classes('col items-center gap-0'):
                ui.element('div').style(f'background:{cor}; height:8px; width:100%;'
                                        f'opacity:{opacidade}')
                ui.label(rotulo).classes('legenda-texto').style('font-size:9px')
