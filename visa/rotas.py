"""
Trajetória percorrida por um voo, vinda do ciop-tracker do DECEA.

    GET <TRACKER_BASE>/montreal/<indicativo>/<AAAA-MM-DD>?apikey=...
    -> {'callsign', 'segments': [{'adep', 'ades', 'positions': [{'latitude', 'longitude',
                                    'received_at', 'flight_level', ...}, ...]}, ...]}

O servidor Python consulta (a apikey fica aqui) e manda ao mapa só as linhas, já reduzidas.
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
    origem: str
    destino: str
    pontos: list[list[float]]       # [[lat, lon], ...] em ordem de tempo
    inicio: str                     # received_at da primeira posição (ordena os trechos)


@dataclass
class Rota:
    indicativo: str
    trechos: list[Trecho] = field(default_factory=list)   # do mais antigo ao mais recente (o último é o voo atual)


def simplificar(pontos: list[list[float]], minimo: float = DISTANCIA_MIN_GRAUS) -> list[list[float]]:
    """Mantém o primeiro, o último e cada ponto a mais de `minimo` graus do último mantido."""
    if len(pontos) <= 2:
        return pontos
    saida = [pontos[0]]
    for p in pontos[1:-1]:
        u = saida[-1]
        if abs(p[0] - u[0]) > minimo or abs(p[1] - u[1]) > minimo:
            saida.append(p)
    saida.append(pontos[-1])
    return saida


def converter(dados: dict, indicativo: str) -> Rota | None:
    """Resposta do tracker -> Rota (um trecho por segmento/voo). None se não houver posições."""
    rota = Rota(indicativo)
    for seg in dados.get('segments') or []:
        posicoes = [p for p in seg.get('positions') or []
                    if isinstance(p.get('latitude'), (int, float)) and isinstance(p.get('longitude'), (int, float))]
        if not posicoes:
            continue
        posicoes.sort(key=lambda p: p.get('received_at') or '')
        linha = [[round(p['latitude'], 4), round(p['longitude'], 4)] for p in posicoes]
        rota.trechos.append(Trecho(seg.get('adep') or '?', seg.get('ades') or '?',
                                   simplificar(linha), posicoes[0].get('received_at') or ''))
    rota.trechos.sort(key=lambda t: t.inicio)
    return rota if rota.trechos else None


async def _consultar(indicativo: str, dia: date) -> Rota | None:
    resposta = await _cliente.get(f'{config.TRACKER_BASE}/montreal/{indicativo}/{dia.isoformat()}',
                                  params={'apikey': config.TRACKER_APIKEY})
    if resposta.status_code == 404:
        return None
    resposta.raise_for_status()
    return converter(resposta.json(), indicativo)


async def buscar(indicativo: str, hoje: date | None = None) -> Rota | None:
    """Rota do voo hoje (UTC) ou, se não houver, ontem (voos que passaram da meia-noite).

    Levanta ValueError para indicativo inválido e httpx.HTTPError se o tracker falhar.
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
