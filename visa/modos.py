"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Modos de trabalho: conjuntos de camadas pensados para uma tarefa do meteorologista.

Um modo só muda as preferências (o que fica ligado). A interface está ligada a essas
preferências e liga/desliga as camadas sozinha. Depois de escolher um modo, a pessoa
pode ajustar o que quiser; o modo deixa de aparecer como ativo ("personalizado").

Usado pelo cabeçalho da tela (botões de modo). Ver DECISOES 5.2.
"""
from dataclasses import dataclass, field

import config
import espaco
import wifs


@dataclass(frozen=True)
class Modo:
    """Um modo de trabalho: o que ele liga e desliga nas preferências.

    Atributos:
        chave: identificador do modo ('conveccao', 'altitude', 'terminal').
        titulo: nome no botão.
        icone: ícone do Material.
        descricao: dica do botão.
        ligados: preferências simples ligadas/desligadas (radar_ligado...).
        wifs: camadas WIFS ligadas (as outras desligam).
        espaco: camadas de espaço aéreo ligadas (as outras desligam).
        zoom_terminal: se True, aproxima o mapa no aeródromo escolhido.
        extras: ponto de partida (ex.: nível de voo); mudar depois não tira do modo.
    """
    chave: str
    titulo: str
    icone: str
    descricao: str
    ligados: dict[str, bool]
    wifs: frozenset[str] = frozenset()
    espaco: frozenset[str] = frozenset()
    zoom_terminal: bool = False
    extras: dict = field(default_factory=dict)


MODOS = [
    Modo('conveccao', 'Convecção', 'thunderstorm',
         'Onde há convecção agora e o que ela atinge: satélite, radar, raios, tempo severo e TMAs',
         {'satelite_ligado': True, 'radar_ligado': True, 'raios_ligado': True, 'severo_ligado': True,
          'vento_ligado': False, 'modelo_ligado': False, 'aeronaves_ligado': True, 'aerodromos_ligado': True},
         espaco=frozenset({'tma'})),
    Modo('altitude', 'Altitude', 'flight',
         'O que acontece em rota: vento, turbulência e gelo no nível de voo, com as aerovias altas',
         {'satelite_ligado': False, 'radar_ligado': False, 'raios_ligado': False, 'severo_ligado': False,
          'vento_ligado': True, 'modelo_ligado': False, 'aeronaves_ligado': True, 'aerodromos_ligado': False},
         wifs=frozenset({'turbulencia', 'gelo_severo'}), espaco=frozenset({'aerovias_altas'}),
         extras={'vento_fl': 340, 'wifs_fl': 340}),   # vento e WIFS no mesmo nível
    Modo('terminal', 'Terminal', 'location_city',
         'Uma área terminal de perto: radar, raios, aeródromos e setores da TMA',
         {'satelite_ligado': False, 'radar_ligado': True, 'raios_ligado': True, 'severo_ligado': False,
          'vento_ligado': False, 'modelo_ligado': False, 'aeronaves_ligado': True, 'aerodromos_ligado': True},
         espaco=frozenset({'tma', 'setores_tma'}), zoom_terminal=True),
]
POR_CHAVE = {modo.chave: modo for modo in MODOS}
ZOOM_TERMINAL = 9


def aplicar(modo: Modo, prefs: dict) -> None:
    """Muda as preferências para as do modo (a interface reage e liga/desliga as camadas).

    Parâmetros:
        modo: o modo escolhido.
        prefs: preferências do usuário (alterado no lugar).
    """
    for chave, valor in modo.ligados.items():
        prefs[chave] = valor
    for chave in prefs['wifs']:
        prefs['wifs'][chave] = chave in modo.wifs
    for chave in prefs['espaco']:
        prefs['espaco'][chave] = chave in modo.espaco
    prefs.update(modo.extras)


def _ligadas(grupo: dict[str, bool]) -> set[str]:
    """Chaves ligadas (valor verdadeiro) de um grupo de preferências, como prefs['wifs']."""
    return {chave for chave, ligada in grupo.items() if ligada}


def _corresponde(modo: Modo, prefs: dict) -> bool:
    """Diz se as camadas ligadas em `prefs` são exatamente as do modo (os `extras` não contam)."""
    simples_iguais = all(prefs.get(chave) == valor for chave, valor in modo.ligados.items())
    return (simples_iguais
            and _ligadas(prefs['wifs']) == modo.wifs
            and _ligadas(prefs['espaco']) == modo.espaco)


def ativo(prefs: dict) -> str | None:
    """Descobre qual modo está ativo agora.

    Parâmetros:
        prefs: preferências do usuário.

    Retorna:
        Chave do modo cujas camadas são exatamente as ligadas agora; None = personalizado.
    """
    for modo in MODOS:
        if _corresponde(modo, prefs):
            return modo.chave
    return None


def _conferir() -> None:
    """Confere que as camadas e níveis citados nos modos existem.

    Roda ao importar o módulo: um erro de digitação em MODOS quebra na hora, não na tela.
    """
    for modo in MODOS:
        assert modo.wifs <= set(wifs.POR_CHAVE), modo.chave
        assert modo.espaco <= set(espaco.POR_CHAVE), modo.chave
        assert modo.extras.get('wifs_fl', wifs.NIVEL_PADRAO) in wifs.NIVEIS, modo.chave
        assert modo.extras.get('vento_fl', 340) in config.VENTO_FL_PARA_PA, modo.chave


_conferir()
