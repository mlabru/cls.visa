"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Animação de satélite, radar e raios: a linha do tempo comum aos três.

O satélite tem imagem a cada 10 min; o radar, a cada 20 min; os raios, a cada 6–7 min.
A linha do tempo anda de 10 em 10 min e, em cada passo, mostra o quadro mais recente de
cada um que já existia naquele horário (o radar repete o quadro quando não há um novo).

Funções puras: quem baixa as imagens e mexe no mapa é a CamadaAnimacao (camadas/animacao_mapa.py).
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redemet

PASSO_MIN = 10
JANELAS = {60: 'última 1 h', 120: 'últimas 2 h', 180: 'últimas 3 h'}
JANELA_PADRAO = 120
INTERVALO_S = 0.7          # tempo de cada quadro na tela
PAUSA_NO_FIM = 3           # quadros parados no último (o mais recente) antes de recomeçar
TOLERANCIA_RADAR_MIN = 3   # o radar de 16:38 já vale para o passo de 16:40 (e de 16:37 para 16:40)


@dataclass(frozen=True)
class Passo:
    quando: datetime
    satelite: redemet.Imagem | None
    radar: redemet.QuadroRadar | None
    raios: redemet.QuadroRaios | None = None


def _ultimo_ate(itens: list, quando: datetime, folga: timedelta = timedelta(0)):
    """O item mais recente com horário <= quando + folga (itens em ordem de tempo)."""
    escolhido = None
    for item in itens:
        if item.quando <= quando + folga:
            escolhido = item
        else:
            break
    return escolhido


def linha_do_tempo(satelites: list[redemet.Imagem], quadros: list[redemet.QuadroRadar],
                   janela_min: int, raios: list[redemet.QuadroRaios] = ()) -> list[Passo]:
    """Passos de 10 em 10 min cobrindo a janela, terminando no dado mais novo."""
    raios = list(raios)
    horarios = [i.quando for i in satelites] + [q.quando for q in quadros] + [r.quando for r in raios]
    if not horarios:
        return []
    fim = max(horarios)
    # Grade alinhada em múltiplos de 10 min (16:40, 16:50...) e um último passo no dado mais novo
    grade_fim = fim.replace(minute=fim.minute - fim.minute % PASSO_MIN, second=0, microsecond=0)
    inicio = grade_fim - timedelta(minutes=janela_min)
    momentos, t = [], inicio
    while t <= grade_fim:
        momentos.append(t)
        t += timedelta(minutes=PASSO_MIN)
    if momentos[-1] < fim:
        momentos.append(fim)
    folga = timedelta(minutes=TOLERANCIA_RADAR_MIN)
    passos = [Passo(m, _ultimo_ate(satelites, m), _ultimo_ate(quadros, m, folga), _ultimo_ate(raios, m))
              for m in momentos]
    # Começa no primeiro passo que tem alguma imagem
    while passos and passos[0].satelite is None and passos[0].radar is None and passos[0].raios is None:
        passos.pop(0)
    return passos


def rotulo(passo: Passo, agora: datetime | None = None) -> str:
    """'16:30Z · há 20 min'."""
    agora = agora or datetime.now(UTC)
    minutos = max(0, round((agora - passo.quando).total_seconds() / 60))
    idade = f'há {minutos} min' if minutos < 120 else f'há {minutos / 60:.1f} h'.replace('.', ',')
    return f'{passo.quando:%H:%M}Z · {idade}'


def quadros_de_satelite(janela_min: int) -> int:
    """Quantas imagens de satélite pedir para cobrir a janela (com uma de sobra)."""
    return janela_min // PASSO_MIN + 2
