"""
Peças comuns às camadas (por composição: não há classe-base, ver DECISOES 8.1).
"""
from nicegui import ui

# Opacidades das imagens: o radar deixa ver o satélite e o mapa por baixo; os raios (pequenos
# quadrados coloridos) ficam quase opacos para não sumir sobre o radar.
OPACIDADE_RADAR = 0.75
OPACIDADE_RAIOS = 0.9


class UltimoPedido:
    """Só a resposta do pedido mais recente à rede vale (ver DECISOES 5.4).

    Uso: `n = self._pedido.novo()` antes do `await`; depois dele, `if not self._pedido.vale(n): return`.
    Ao desligar ou trocar a camada, `novo()` sozinho descarta as respostas ainda a caminho.
    """

    def __init__(self) -> None:
        self._numero = 0

    def novo(self) -> int:
        self._numero += 1
        return self._numero

    def vale(self, numero: int) -> bool:
        return numero == self._numero


def escrever(rotulo: ui.label | None, texto: str, classes: str | None = None) -> None:
    """Escreve num rótulo do painel. Antes de tela/painel_camadas.py criá-lo, ele é None: não faz nada."""
    if rotulo is None:
        return
    rotulo.set_text(texto)
    if classes is not None:
        rotulo.classes(replace=classes)
