"""
Modos de trabalho: conjuntos de camadas pensados para uma tarefa do meteorologista.

Um modo só muda as preferências (o que fica ligado). A interface está ligada a essas
preferências e liga/desliga as camadas sozinha. Depois de escolher um modo, a pessoa
pode ajustar o que quiser; o modo deixa de aparecer como ativo ("personalizado").
"""
from dataclasses import dataclass, field

import config
import espaco
import wifs


@dataclass(frozen=True)
class Modo:
    chave: str
    titulo: str
    icone: str          # ícone do Material
    descricao: str      # dica do botão
    ligados: dict[str, bool]                              # preferências simples (radar_ligado...)
    wifs: frozenset[str] = frozenset()                    # camadas WIFS ligadas (as outras desligam)
    espaco: frozenset[str] = frozenset()                  # camadas de espaço aéreo ligadas
    zoom_terminal: bool = False                           # aproxima no aeródromo escolhido
    extras: dict = field(default_factory=dict)            # ponto de partida (ex.: nível); mudar não
                                                          # tira do modo


MODOS = [
    Modo('conveccao', 'Convecção', 'thunderstorm',
         'Onde há convecção agora e o que ela atinge: satélite, radar, raios, tempo severo e TMAs',
         {'satelite_ligado': True, 'radar_ligado': True, 'raios_ligado': True, 'severo_ligado': True,
          'vento_ligado': False, 'aeronaves_ligado': True, 'aerodromos_ligado': True},
         espaco=frozenset({'tma'})),
    Modo('altitude', 'Altitude', 'flight',
         'O que acontece em rota: vento, turbulência e gelo no nível de voo, com as aerovias altas',
         {'satelite_ligado': False, 'radar_ligado': False, 'raios_ligado': False, 'severo_ligado': False,
          'vento_ligado': True, 'aeronaves_ligado': True, 'aerodromos_ligado': False},
         wifs=frozenset({'turbulencia', 'gelo_severo'}), espaco=frozenset({'aerovias_altas'}),
         extras={'vento_fl': 340, 'wifs_fl': 340}),   # vento e WIFS no mesmo nível
    Modo('terminal', 'Terminal', 'location_city',
         'Uma área terminal de perto: radar, raios, aeródromos e setores da TMA',
         {'satelite_ligado': False, 'radar_ligado': True, 'raios_ligado': True, 'severo_ligado': False,
          'vento_ligado': False, 'aeronaves_ligado': True, 'aerodromos_ligado': True},
         espaco=frozenset({'tma', 'setores_tma'}), zoom_terminal=True),
]
POR_CHAVE = {m.chave: m for m in MODOS}
ZOOM_TERMINAL = 9


def aplicar(modo: Modo, prefs: dict) -> None:
    """Muda as preferências para as do modo (a interface reage e liga/desliga as camadas)."""
    for chave, valor in modo.ligados.items():
        prefs[chave] = valor
    for chave in prefs['wifs']:
        prefs['wifs'][chave] = chave in modo.wifs
    for chave in prefs['espaco']:
        prefs['espaco'][chave] = chave in modo.espaco
    prefs.update(modo.extras)


def ativo(prefs: dict) -> str | None:
    """Chave do modo cujas camadas são exatamente as ligadas agora (None = personalizado)."""
    for modo in MODOS:
        if all(prefs.get(c) == v for c, v in modo.ligados.items()) \
                and {c for c, v in prefs['wifs'].items() if v} == modo.wifs \
                and {c for c, v in prefs['espaco'].items() if v} == modo.espaco:
            return modo.chave
    return None


def _conferir() -> None:
    """As camadas citadas nos modos existem (erro de digitação quebra na hora de importar)."""
    for modo in MODOS:
        assert modo.wifs <= set(wifs.POR_CHAVE), modo.chave
        assert modo.espaco <= set(espaco.POR_CHAVE), modo.chave
        assert modo.extras.get('wifs_fl', wifs.NIVEL_PADRAO) in wifs.NIVEIS, modo.chave
        assert modo.extras.get('vento_fl', 340) in config.VENTO_FL_PARA_PA, modo.chave


_conferir()
