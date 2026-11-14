"""
Cache em memória com tamanho máximo, usado por vários módulos (redemet, impacto, rotas).

Um dict do Python lembra a ordem de inserção, então `next(iter(cache))` é o item mais antigo.
Descartá-lo quando o cache passa do limite dá um cache FIFO, sem biblioteca. Não é LRU
(que descartaria o menos usado), mas para dados que envelhecem em minutos dá no mesmo.
Ver DECISOES 4.3.

`ainda_vale` responde se um valor guardado com `time.monotonic()` ainda está dentro da validade.
"""
import time


def ainda_vale(guardado_em: float | None, validade_s: float) -> bool:
    """Guardado (em time.monotonic()) há menos de `validade_s` segundos?"""
    return guardado_em is not None and time.monotonic() - guardado_em < validade_s


def guardar(cache: dict, chave, valor, limite: int) -> None:
    """Guarda no cache; se passou do limite, descarta o item mais antigo."""
    cache.pop(chave, None)      # regravar uma chave a põe no fim (é o item mais novo)
    cache[chave] = valor
    while len(cache) > limite:
        cache.pop(next(iter(cache)))
