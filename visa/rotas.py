"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Trajetória percorrida por um voo, vinda do ciop-tracker do DECEA.

    GET <TRACKER_BASE>/montreal/<indicativo>/<AAAA-MM-DD>?apikey=...
    -> {'callsign', 'segments': [{'adep', 'ades', 'positions': [{'latitude', 'longitude',
                                    'received_at', 'flight_level', ...}, ...]}, ...]}

O servidor Python consulta (a apikey fica aqui) e manda ao mapa só as linhas, já reduzidas.
Usado pela camada de rotas (`camadas/contexto.py`), ao clicar "Ver / ocultar rota".
"""
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

import cache
import config

INDICATIVO = re.compile(r'[A-Z0-9]{2,10}')   # vai no caminho da URL: nada além disso
DISTANCIA_MIN_GRAUS = 0.01                  # ~1 km: pontos mais próximos que isso são descartados
CACHE_S = 60
CACHE_MAX = 200                             # indicativos guardados (os mais antigos saem)

_cliente = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
_cache: dict[str, tuple[float, 'Rota | None']] = {}


@dataclass
class Trecho:
    """Um voo (segmento) da trajetória.

    Atributos:
        origem: aeródromo de partida (ICAO; '?' se desconhecido).
        destino: aeródromo de destino (ICAO; '?' se desconhecido).
        pontos: [[lat, lon], ...] em ordem de tempo, já reduzidos por `simplificar`.
        inicio: received_at da primeira posição (texto ISO; ordena os trechos).
    """
    origem: str
    destino: str
    pontos: list[list[float]]
    inicio: str


@dataclass
class Rota:
    """Trajetória de um indicativo.

    Atributos:
        indicativo: indicativo do voo, em maiúsculas.
        trechos: do mais antigo ao mais recente (o último é o voo atual).
    """
    indicativo: str
    trechos: list[Trecho] = field(default_factory=list)


def simplificar(pontos: list[list[float]], minimo: float = DISTANCIA_MIN_GRAUS) -> list[list[float]]:
    """Reduz uma linha, descartando pontos muito próximos do anterior.

    Mantém o primeiro, o último e cada ponto a mais de `minimo` graus (em lat ou em lon)
    do último mantido.

    Parâmetros:
        pontos: [[lat, lon], ...] em ordem.
        minimo: distância mínima, em graus (padrão ~1 km).

    Retorna:
        A lista reduzida (a mesma lista, se tiver 2 pontos ou menos).
    """
    if len(pontos) <= 2:
        return pontos
    mantidos = [pontos[0]]
    for ponto in pontos[1:-1]:
        ultimo = mantidos[-1]
        if abs(ponto[0] - ultimo[0]) > minimo or abs(ponto[1] - ultimo[1]) > minimo:
            mantidos.append(ponto)
    mantidos.append(pontos[-1])
    return mantidos


def _tem_coordenadas(posicao: dict) -> bool:
    """Diz se a posição do tracker traz latitude e longitude numéricas."""
    return isinstance(posicao.get('latitude'), (int, float)) and isinstance(posicao.get('longitude'), (int, float))


def _trecho(segmento: dict) -> Trecho | None:
    """Converte um segmento do tracker num Trecho.

    Parâmetros:
        segmento: item de 'segments' ({'adep', 'ades', 'positions': [...]}).

    Retorna:
        O Trecho, ou None se o segmento não tiver posição válida.
    """
    posicoes = [posicao for posicao in segmento.get('positions') or [] if _tem_coordenadas(posicao)]
    if not posicoes:
        return None
    posicoes.sort(key=lambda posicao: posicao.get('received_at') or '')
    linha = [[round(posicao['latitude'], 4), round(posicao['longitude'], 4)] for posicao in posicoes]
    return Trecho(segmento.get('adep') or '?', segmento.get('ades') or '?',
                  simplificar(linha), posicoes[0].get('received_at') or '')


def converter(dados: dict, indicativo: str) -> Rota | None:
    """Converte a resposta do tracker numa Rota (um trecho por segmento/voo).

    Parâmetros:
        dados: JSON da resposta (formato no topo do módulo).
        indicativo: indicativo pedido.

    Retorna:
        A Rota com os trechos em ordem de tempo; None se não houver posições.
    """
    rota = Rota(indicativo)
    for segmento in dados.get('segments') or []:
        trecho = _trecho(segmento)
        if trecho:
            rota.trechos.append(trecho)
    rota.trechos.sort(key=lambda trecho: trecho.inicio)
    if not rota.trechos:
        return None
    return rota


async def _consultar(indicativo: str, dia: date) -> Rota | None:
    """Consulta o tracker para um indicativo num dia (UTC).

    Retorna:
        A Rota; None se o tracker responder 404 ou não houver posições.
        Levanta httpx.HTTPError em outras falhas.
    """
    resposta = await _cliente.get(f'{config.TRACKER_BASE}/montreal/{indicativo}/{dia.isoformat()}',
                                  params={'apikey': config.TRACKER_APIKEY})
    if resposta.status_code == 404:
        return None
    resposta.raise_for_status()
    return converter(resposta.json(), indicativo)


async def buscar(indicativo: str, hoje: date | None = None) -> Rota | None:
    """Busca a rota do voo hoje (UTC) ou, se não houver, ontem (voos que passaram da meia-noite).

    Cada indicativo fica em cache por CACHE_S segundos (inclusive a resposta "sem rota").
    Levanta ValueError para indicativo inválido e httpx.HTTPError se o tracker falhar.

    Parâmetros:
        indicativo: indicativo do voo (espaços e minúsculas são tolerados).
        hoje: data de referência (testes); None = hoje em UTC.

    Retorna:
        A Rota, ou None se o tracker não tiver o voo nem hoje nem ontem.
    """
    indicativo = (indicativo or '').strip().upper()
    if not INDICATIVO.fullmatch(indicativo):
        raise ValueError(f'indicativo inválido: {indicativo!r}')
    guardado = _cache.get(indicativo)
    if guardado and cache.ainda_vale(guardado[0], CACHE_S):
        return guardado[1]
    hoje = hoje or datetime.now(UTC).date()
    rota = await _consultar(indicativo, hoje) or await _consultar(indicativo, hoje - timedelta(days=1))
    cache.guardar(_cache, indicativo, (time.monotonic(), rota), CACHE_MAX)
    return rota
