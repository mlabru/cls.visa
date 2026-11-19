"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Raios (descargas atmosféricas): camada WMS `lightning_data` do GeoServer da REDEMET.

As imagens passam pelo mesmo proxy do radar (/radar/wms). O GeoServer desenha os raios
de uma janela de 60 min com um dos dois estilos (SLD) abaixo; os estilos recebem, pelo
parâmetro `env`, o raio do círculo e os instantes que separam as classes de tempo.

Usado pela camada de raios do GeoServer (`camadas/observacao.py`), pelo painel e pela legenda.
"""
from datetime import UTC, datetime, timedelta

JANELA_MIN = 60
PASSO_MIN = 15          # botões ◀ ▶ andam de 15 em 15 min

CLASSIFICACOES = {'tempo': 'Tempo (última hora)', 'intensidade': 'Intensidade'}
ESTILOS = {'tempo': 'redemet:lightning_time_classification',
           'intensidade': 'redemet:lightning_by_intensity'}

# Cores do estilo "tempo" (definidas no SLD do GeoServer)
LEGENDA_TEMPO = [('#ef4444', '0 a 15 min'), ('#fff000', '15 a 30 min'),
                 ('#22c55e', '30 a 45 min'), ('#1d4ed8', '45 a 60 min')]
# Cores do estilo "intensidade": só o GeoServer conhece (sem VPN não há como conferir)


def agora() -> datetime:
    """Devolve o instante atual em UTC, arredondado para baixo ao minuto."""
    return datetime.now(UTC).replace(second=0, microsecond=0)


def _iso(quando: datetime) -> str:
    """Formata a data como o parâmetro TIME do WMS espera: '2026-11-13T16:08:00Z'."""
    return quando.strftime('%Y-%m-%dT%H:%M:%SZ')


def _compacto(quando: datetime) -> str:
    """Formata a data sem '-' e ':' ('20261113T160800Z'), como o SLD lê no `env`.

    O SLD lê as datas com dateParse yyyyMMdd'T'HHmmss'Z'; '-' e ':' quebrariam o `env`.
    """
    return quando.strftime('%Y%m%dT%H%M%SZ')


def parametros(fim: datetime, classificacao: str, raio_km: int) -> dict:
    """Monta os parâmetros do WMS para a janela de 60 min que termina em `fim`.

    Parâmetros:
        fim: fim da janela (UTC).
        classificacao: 'tempo' ou 'intensidade' (chave de ESTILOS; outra vale 'tempo').
        raio_km: raio do círculo de cada raio, em km.

    Retorna:
        Dict com 'TIME' (intervalo ISO), 'env' (raio e marcos t0..t60) e 'styles'.
    """
    inicio = fim - timedelta(minutes=JANELA_MIN)
    marcos = {f't{minutos}': _compacto(fim - timedelta(minutes=minutos)) for minutos in (0, 15, 30, 45, 60)}
    partes_env = [f'radius:{int(raio_km) * 1000}']
    partes_env += [f'{nome}:{instante}' for nome, instante in marcos.items()]
    env = ';'.join(partes_env)
    return {
        'TIME': f'{_iso(inicio)}/{_iso(fim)}',
        'env': env,
        'styles': ESTILOS.get(classificacao, ESTILOS['tempo']),
    }


def deslocar(fim: datetime | None, minutos: int, referencia: datetime | None = None) -> datetime | None:
    """Anda a janela de raios para trás ou para a frente.

    Parâmetros:
        fim: fim atual da janela; None = ao vivo (agora).
        minutos: quanto andar, em min (negativo = para trás).
        referencia: o "agora" a usar (testes); None = `agora()`.

    Retorna:
        O novo fim da janela, ou None ("ao vivo") se ele chegar ao horário atual.
    """
    referencia = referencia or agora()
    novo_fim = (fim or referencia) + timedelta(minutes=minutos)
    if novo_fim >= referencia:
        return None
    return novo_fim


def rotulo_janela(fim: datetime) -> str:
    """Texto da janela para a tela, ex.: '15:00 – 16:00 UTC · 13/11'.

    Parâmetros:
        fim: fim da janela (UTC).

    Retorna:
        O rótulo com início, fim e dia.
    """
    inicio = fim - timedelta(minutes=JANELA_MIN)
    return f'{inicio:%H:%M} – {fim:%H:%M} UTC · {fim:%d/%m}'
