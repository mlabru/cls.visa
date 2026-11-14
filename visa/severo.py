"""
Tempo severo previsto (WRF e outros modelos): feições do WFS `redemet:tempo_severo`.

Cada feição é uma área (polígono) ou um ponto, com as propriedades
    origem (modelo), resolucao_km, tipo ('area' ou 'ponto'), rodada e validade (datas ISO).

1. `catalogo()` traz só as propriedades de todas as feições (sem geometria) e monta as
   opções do painel: modelo → resolução → tipo → rodada → validade.
2. `feicoes()` traz as geometrias da combinação escolhida, que vão para o mapa.

O GeoServer é interno: quem consulta é o servidor Python, nunca o navegador.
"""
import logging
import re
import time
from datetime import UTC, datetime

import httpx

import config
import geo
import radar

log = logging.getLogger('severo')

TIPOS = {'area': 'Área', 'ponto': 'Ponto'}
CATALOGO_CACHE_S = 300
POR_PAGINA = 3000
MAX_PAGINAS = 3                  # no máximo 9000 feições por consulta (o original fazia igual)
CASAS = 4                        # ~10 m: suficiente para áreas de previsão e reduz o envio

_cliente = httpx.AsyncClient(timeout=httpx.Timeout(config.RADAR_TIMEOUT_S, connect=5))
_cache: tuple[float, list[dict]] = (0.0, [])


class Indisponivel(Exception):
    """GeoServer fora do ar ou resposta inválida. A mensagem já vem pronta para a tela."""


async def _wfs(**parametros) -> dict:
    if radar.geoserver_fora():
        raise Indisponivel('GeoServer inacessível')
    base = {'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature',
            'typeName': 'redemet:tempo_severo', 'outputFormat': 'application/json'}
    try:
        resposta = await _cliente.get(config.GEOSERVER_WFS, params=base | parametros)
        resposta.raise_for_status()
        return resposta.json()
    except (httpx.HTTPError, ValueError) as erro:
        raise Indisponivel(f'GeoServer inacessível ({type(erro).__name__})') from erro


# ---------------------------------------------------------------------------
# Catálogo e opções do painel
# ---------------------------------------------------------------------------

async def catalogo(forcar: bool = False) -> list[dict]:
    """Propriedades de todas as previsões disponíveis (cache de 5 min)."""
    global _cache
    if not forcar and _cache[1] and time.monotonic() - _cache[0] < CATALOGO_CACHE_S:
        return _cache[1]
    dados = await _wfs(propertyName='validade,rodada,origem,resolucao_km,tipo',
                       count='10000', sortBy='validade D')
    itens = [f.get('properties') or {} for f in dados.get('features', [])]
    _cache = (time.monotonic(), itens)
    return itens


def resolucao_texto(valor) -> str:
    """resolucao_km como texto estável: 3 -> '3', 3.0 -> '3', 1.5 -> '1.5'."""
    try:
        return f'{float(valor):g}'
    except (TypeError, ValueError):
        return ''


def modelos(cat: list[dict]) -> list[str]:
    return sorted({p['origem'] for p in cat if p.get('origem')})


def resolucoes(cat: list[dict], origem: str) -> list[str]:
    valores = {resolucao_texto(p.get('resolucao_km')) for p in cat if p.get('origem') == origem} - {''}
    return sorted(valores, key=float)


def tipos(cat: list[dict], origem: str, resolucao: str) -> list[str]:
    return sorted({p['tipo'] for p in cat if p.get('origem') == origem
                   and resolucao_texto(p.get('resolucao_km')) == resolucao and p.get('tipo')})


def rodadas(cat: list[dict], origem: str, resolucao: str, tipo: str) -> list[str]:
    """Mais recente primeiro."""
    return sorted({p['rodada'] for p in cat if p.get('origem') == origem and p.get('tipo') == tipo
                   and resolucao_texto(p.get('resolucao_km')) == resolucao and p.get('rodada')}, reverse=True)


def validades(cat: list[dict], origem: str, resolucao: str, tipo: str, rodada: str) -> list[str]:
    """Em ordem cronológica."""
    return sorted({p['validade'] for p in cat if p.get('origem') == origem and p.get('tipo') == tipo
                   and resolucao_texto(p.get('resolucao_km')) == resolucao and p.get('rodada') == rodada
                   and p.get('validade')})


def mais_recente(cat: list[dict]) -> dict | None:
    """A previsão de validade mais adiantada (o painel abre nela)."""
    validos = [p for p in cat if p.get('validade') and p.get('origem') and p.get('rodada')]
    return max(validos, key=lambda p: p['validade']) if validos else None


# ---------------------------------------------------------------------------
# Consulta das geometrias
# ---------------------------------------------------------------------------

_SEGURO = re.compile(r'[\w.\-]+')
_DATA = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?')


def filtro_cql(origem: str, resolucao: str, tipo: str, rodada: str, validade: str) -> str:
    """Filtro CQL da combinação escolhida. Valores fora do formato esperado são recusados
    (eles vão para o GeoServer: nada de aspas ou expressões vindas de fora)."""
    if not _SEGURO.fullmatch(origem or '') or tipo not in TIPOS:
        raise ValueError('modelo ou tipo inválido')
    if not _DATA.fullmatch(rodada or '') or not _DATA.fullmatch(validade or ''):
        raise ValueError('data inválida')
    float(resolucao)  # ValueError se não for número
    return ' AND '.join([
        f"strToLowerCase(origem)='{origem.lower()}'",
        f'resolucao_km={resolucao}',
        f"strToLowerCase(tipo)='{tipo}'",
        f"rodada='{rodada}'",
        f"validade='{validade}'",
    ])


async def feicoes(cql: str) -> list[dict]:
    """Geometrias da previsão escolhida (até MAX_PAGINAS × POR_PAGINA)."""
    saida = []
    for pagina in range(MAX_PAGINAS):
        dados = await _wfs(propertyName='geometry,tipo', srsName='EPSG:4326', sortBy='validade D',
                           count=str(POR_PAGINA), startIndex=str(pagina * POR_PAGINA), CQL_FILTER=cql)
        lote = dados.get('features') or []
        for f in lote:
            g = f.get('geometry')
            if g and 'coordinates' in g:
                saida.append({'type': 'Feature', 'properties': {},
                              'geometry': {'type': g['type'], 'coordinates': geo.arredondar(g['coordinates'], CASAS)}})
        total = dados.get('numberMatched')
        if len(lote) < POR_PAGINA or (isinstance(total, int) and len(saida) >= total):
            break
    return saida


def _pontos(coordenadas):
    if coordenadas and isinstance(coordenadas[0], (int, float)):
        yield coordenadas
    else:
        for c in coordenadas or []:
            yield from _pontos(c)


def limites(lista: list[dict]) -> list[list[float]] | None:
    """[[sul, oeste], [norte, leste]] de todas as feições — o retângulo de cobertura."""
    return geo.limites([[p[1], p[0]] for f in lista for p in _pontos(f['geometry']['coordinates'])])


# ---------------------------------------------------------------------------
# Textos do painel
# ---------------------------------------------------------------------------

def _data(texto: str) -> datetime:
    return datetime.fromisoformat(texto).astimezone(UTC)     # Python 3.11+ aceita o 'Z'


def rotulo(texto: str) -> str:
    """'2026-11-13T06:00:00Z' -> '13/11 06:00Z'."""
    try:
        return f'{_data(texto):%d/%m %H:%MZ}'
    except ValueError:
        return texto


def situacao(validade: str, referencia: datetime | None = None) -> tuple[str, str]:
    """(texto, cor) como no original: Expirado, Próximo (< 6 h) ou Válido."""
    referencia = referencia or datetime.now(UTC)
    horas = (_data(validade) - referencia).total_seconds() / 3600
    if horas < 0:
        return 'expirada', 'negative'
    if horas < 6:
        return 'próxima (< 6 h)', 'warning'
    return 'válida', 'positive'
