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
    """Um passo da linha do tempo: o que mostrar de cada camada naquele horário.

    Atributos:
        quando: horário do passo (UTC).
        satelite: imagem de satélite do passo, ou None se ainda não havia nenhuma.
        radar: quadro de radar (várias estações), ou None.
        raios: quadro de raios, ou None.
    """

    quando: datetime
    satelite: redemet.Imagem | None
    radar: redemet.QuadroRadar | None
    raios: redemet.QuadroRaios | None = None


def _ultimo_ate(itens: list, quando: datetime, folga: timedelta = timedelta(0)):
    """Acha o item mais recente com horário <= quando + folga.

    Parâmetros:
        itens: objetos com atributo `quando`, em ordem de tempo.
        quando: o horário do passo.
        folga: tolerância para aceitar um item um pouco depois do passo.

    Retorna:
        O item, ou None se todos são posteriores.
    """
    escolhido = None
    for item in itens:
        if item.quando <= quando + folga:
            escolhido = item
        else:
            break
    return escolhido


def _momentos(fim: datetime, janela_min: int) -> list[datetime]:
    """Calcula os horários dos passos: grade de 10 em 10 min e um último no dado mais novo.

    Parâmetros:
        fim: horário do dado mais novo.
        janela_min: duração da janela, em min.

    Retorna:
        Horários em ordem crescente, alinhados em múltiplos de 10 min (16:40, 16:50...), mais `fim`
        se ele não cair na grade.
    """
    grade_fim = fim.replace(minute=fim.minute - fim.minute % PASSO_MIN, second=0, microsecond=0)
    momento = grade_fim - timedelta(minutes=janela_min)
    momentos = []
    while momento <= grade_fim:
        momentos.append(momento)
        momento += timedelta(minutes=PASSO_MIN)
    if momentos[-1] < fim:
        momentos.append(fim)
    return momentos


def linha_do_tempo(satelites: list[redemet.Imagem], quadros: list[redemet.QuadroRadar],
                   janela_min: int, raios: list[redemet.QuadroRaios] = ()) -> list[Passo]:
    """Monta os passos de 10 em 10 min cobrindo a janela, terminando no dado mais novo.

    Parâmetros:
        satelites: imagens de satélite, em ordem de tempo.
        quadros: quadros de radar, em ordem de tempo.
        janela_min: duração da animação, em min.
        raios: quadros de raios, em ordem de tempo.

    Retorna:
        Os passos, começando no primeiro que tem alguma imagem; vazio se não há dado nenhum.
    """
    raios = list(raios)
    horarios = [imagem.quando for imagem in satelites]
    horarios += [quadro.quando for quadro in quadros]
    horarios += [quadro.quando for quadro in raios]
    if not horarios:
        return []
    folga_radar = timedelta(minutes=TOLERANCIA_RADAR_MIN)
    passos = []
    for momento in _momentos(max(horarios), janela_min):
        passos.append(Passo(momento, _ultimo_ate(satelites, momento), _ultimo_ate(quadros, momento, folga_radar),
                            _ultimo_ate(raios, momento)))
    # Começa no primeiro passo que tem alguma imagem
    while passos and passos[0].satelite is None and passos[0].radar is None and passos[0].raios is None:
        passos.pop(0)
    return passos


def rotulo(passo: Passo, agora: datetime | None = None) -> str:
    """Monta o texto do passo para a linha do tempo, ex.: '16:30Z · há 20 min'.

    Parâmetros:
        passo: o passo mostrado.
        agora: horário atual (UTC); None usa o relógio (os testes passam um fixo).

    Retorna:
        Horário e idade; acima de 2 h, a idade vai em horas ('há 3,0 h').
    """
    agora = agora or datetime.now(UTC)
    minutos = max(0, round((agora - passo.quando).total_seconds() / 60))
    idade = f'há {minutos} min' if minutos < 120 else f'há {minutos / 60:.1f} h'.replace('.', ',')
    return f'{passo.quando:%H:%M}Z · {idade}'


def quadros_de_satelite(janela_min: int) -> int:
    """Calcula quantas imagens de satélite pedir para cobrir a janela (com uma de sobra).

    Parâmetros:
        janela_min: duração da animação, em min.

    Retorna:
        O número de imagens.
    """
    return janela_min // PASSO_MIN + 2
