"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Tempo severo previsto (WRF e outros modelos): feições do WFS `redemet:tempo_severo`.

Cada feição é uma área (polígono) ou um ponto, com as propriedades
    origem (modelo), resolucao_km, tipo ('area' ou 'ponto'), rodada e validade (datas ISO).

1. `catalogo()` traz só as propriedades de todas as feições (sem geometria) e monta as
   opções do painel: modelo → resolução → tipo → rodada → validade.
2. `feicoes()` traz as geometrias da combinação escolhida, que vão para o mapa.

O GeoServer é interno: quem consulta é o servidor Python, nunca o navegador.
Usado pela camada de tempo severo (`camadas/prevista.py`), pela legenda e pela gaveta (sonda).
"""
import re
import time
from datetime import UTC, datetime

import httpx

import cache
import config
import datas
import geo
import radar

TIPOS = {'area': 'Área', 'ponto': 'Ponto'}
CATALOGO_CACHE_S = 300
POR_PAGINA = 3000
MAX_PAGINAS = 3                  # no máximo 9000 feições por consulta (o original fazia igual)
CASAS = 4                        # ~10 m: suficiente para áreas de previsão e reduz o envio

_cliente = httpx.AsyncClient(timeout=httpx.Timeout(config.RADAR_TIMEOUT_S, connect=5))
_cache: tuple[float, list[dict]] = (0.0, [])     # (guardado em time.monotonic(), catálogo)


class Indisponivel(Exception):
    """GeoServer fora do ar ou resposta inválida. A mensagem já vem pronta para a tela."""


async def _wfs(**parametros) -> dict:
    """Faz um GetFeature no WFS `redemet:tempo_severo`.

    Parâmetros:
        parametros: parâmetros extras do WFS (propertyName, CQL_FILTER, count...).

    Retorna:
        O GeoJSON da resposta. Levanta Indisponivel se o GeoServer estiver fora ou falhar.
    """
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
# As funções de opções recebem `itens`: a lista de propriedades devolvida por `catalogo()`.

async def catalogo(forcar: bool = False) -> list[dict]:
    """Busca as propriedades de todas as previsões disponíveis (cache de 5 min).

    Parâmetros:
        forcar: se True, ignora o cache e consulta o GeoServer.

    Retorna:
        Uma lista de dicts com origem, resolucao_km, tipo, rodada e validade.
        Levanta Indisponivel se o GeoServer falhar.
    """
    global _cache
    guardado_em, itens_guardados = _cache
    if not forcar and itens_guardados and cache.ainda_vale(guardado_em, CATALOGO_CACHE_S):
        return itens_guardados
    dados = await _wfs(propertyName='validade,rodada,origem,resolucao_km,tipo',
                       count='10000', sortBy='validade D')
    itens = [feature.get('properties') or {} for feature in dados.get('features', [])]
    _cache = (time.monotonic(), itens)
    return itens


def resolucao_texto(valor) -> str:
    """Converte resolucao_km num texto estável: 3 -> '3', 3.0 -> '3', 1.5 -> '1.5'.

    Parâmetros:
        valor: resolução em km, como veio do WFS (número, texto ou None).

    Retorna:
        O texto; '' se o valor não for um número.
    """
    try:
        return f'{float(valor):g}'
    except (TypeError, ValueError):
        return ''


def _da_combinacao(itens: list[dict], origem: str, resolucao: str,
                   tipo: str | None = None, rodada: str | None = None) -> list[dict]:
    """Filtra os itens do catálogo pela parte da combinação já escolhida no painel.

    Parâmetros:
        itens: propriedades do catálogo.
        origem: modelo escolhido.
        resolucao: resolução escolhida, como em `resolucao_texto`.
        tipo: tipo escolhido; None = ainda não escolhido (não filtra).
        rodada: rodada escolhida; None = ainda não escolhida (não filtra).

    Retorna:
        Os itens que batem com tudo o que foi informado.
    """
    selecionados = []
    for propriedades in itens:
        if propriedades.get('origem') != origem:
            continue
        if resolucao_texto(propriedades.get('resolucao_km')) != resolucao:
            continue
        if tipo is not None and propriedades.get('tipo') != tipo:
            continue
        if rodada is not None and propriedades.get('rodada') != rodada:
            continue
        selecionados.append(propriedades)
    return selecionados


def modelos(itens: list[dict]) -> list[str]:
    """Lista os modelos (origem) do catálogo, em ordem alfabética."""
    return sorted({propriedades['origem'] for propriedades in itens if propriedades.get('origem')})


def resolucoes(itens: list[dict], origem: str) -> list[str]:
    """Lista as resoluções do modelo (como texto), da menor para a maior."""
    valores = {resolucao_texto(propriedades.get('resolucao_km'))
               for propriedades in itens if propriedades.get('origem') == origem}
    valores.discard('')
    return sorted(valores, key=float)


def tipos(itens: list[dict], origem: str, resolucao: str) -> list[str]:
    """Lista os tipos ('area', 'ponto') do modelo e resolução, em ordem alfabética."""
    escolhidos = _da_combinacao(itens, origem, resolucao)
    return sorted({propriedades['tipo'] for propriedades in escolhidos if propriedades.get('tipo')})


def rodadas(itens: list[dict], origem: str, resolucao: str, tipo: str) -> list[str]:
    """Lista as rodadas da combinação, a mais recente primeiro."""
    escolhidos = _da_combinacao(itens, origem, resolucao, tipo)
    return sorted({propriedades['rodada'] for propriedades in escolhidos if propriedades.get('rodada')},
                  reverse=True)


def validades(itens: list[dict], origem: str, resolucao: str, tipo: str, rodada: str) -> list[str]:
    """Lista as validades da combinação, em ordem cronológica."""
    escolhidos = _da_combinacao(itens, origem, resolucao, tipo, rodada)
    return sorted({propriedades['validade'] for propriedades in escolhidos if propriedades.get('validade')})


def mais_recente(itens: list[dict]) -> dict | None:
    """Acha a previsão de validade mais adiantada (o painel abre nela).

    Parâmetros:
        itens: propriedades do catálogo.

    Retorna:
        As propriedades dessa previsão; None se nenhuma tiver validade, origem e rodada.
    """
    completos = [propriedades for propriedades in itens
                 if propriedades.get('validade') and propriedades.get('origem') and propriedades.get('rodada')]
    if not completos:
        return None
    return max(completos, key=lambda propriedades: propriedades['validade'])


# ---------------------------------------------------------------------------
# Consulta das geometrias
# ---------------------------------------------------------------------------

_SEGURO = re.compile(r'[\w.\-]+')
_DATA = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?')


def filtro_cql(origem: str, resolucao: str, tipo: str, rodada: str, validade: str) -> str:
    """Monta o filtro CQL da combinação escolhida, recusando valores fora do formato.

    Os valores vão para o GeoServer: nada de aspas ou expressões vindas de fora
    (proteção contra injeção, DECISOES 1.4). Levanta ValueError se algum for inválido.

    Parâmetros:
        origem: modelo (letras, dígitos, '_', '.', '-').
        resolucao: resolução em km (tem de ser um número).
        tipo: chave de TIPOS.
        rodada: data ISO (ex.: '2026-11-13T00:00:00Z').
        validade: data ISO.

    Retorna:
        O texto do CQL_FILTER.
    """
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


def _feicao_para_mapa(feature: dict) -> dict | None:
    """Reduz uma feição do WFS ao que o mapa usa: a geometria, com coordenadas arredondadas.

    Retorna:
        A feição GeoJSON sem propriedades; None se ela não tiver geometria.
    """
    geometria = feature.get('geometry')
    if not geometria or 'coordinates' not in geometria:
        return None
    return {'type': 'Feature', 'properties': {},
            'geometry': {'type': geometria['type'], 'coordinates': geo.arredondar(geometria['coordinates'], CASAS)}}


async def feicoes(cql: str) -> list[dict]:
    """Busca as geometrias da previsão escolhida, página a página (até MAX_PAGINAS × POR_PAGINA).

    Parâmetros:
        cql: filtro feito por `filtro_cql`.

    Retorna:
        Lista de feições GeoJSON prontas para o mapa. Levanta Indisponivel se o GeoServer falhar.
    """
    saida = []
    for pagina in range(MAX_PAGINAS):
        dados = await _wfs(propertyName='geometry,tipo', srsName='EPSG:4326', sortBy='validade D',
                           count=str(POR_PAGINA), startIndex=str(pagina * POR_PAGINA), CQL_FILTER=cql)
        lote = dados.get('features') or []
        for feature in lote:
            feicao = _feicao_para_mapa(feature)
            if feicao:
                saida.append(feicao)
        total = dados.get('numberMatched')
        ultima_pagina = len(lote) < POR_PAGINA
        ja_tem_todas = isinstance(total, int) and len(saida) >= total
        if ultima_pagina or ja_tem_todas:
            break
    return saida


def _pontos(coordenadas):
    """Percorre coordenadas GeoJSON aninhadas e gera cada ponto [lon, lat] (gerador)."""
    if coordenadas and isinstance(coordenadas[0], (int, float)):
        yield coordenadas
    else:
        for item in coordenadas or []:
            yield from _pontos(item)


def limites(lista: list[dict]) -> list[list[float]] | None:
    """Calcula o retângulo de cobertura de todas as feições.

    Parâmetros:
        lista: feições GeoJSON (como devolvidas por `feicoes`).

    Retorna:
        [[sul, oeste], [norte, leste]]; None se não houver pontos.
    """
    pontos_lat_lon = []
    for feicao in lista:
        for ponto in _pontos(feicao['geometry']['coordinates']):   # ponto = [lon, lat] ou [lon, lat, alt]
            pontos_lat_lon.append([ponto[1], ponto[0]])
    return geo.limites(pontos_lat_lon)


# ---------------------------------------------------------------------------
# Textos do painel
# ---------------------------------------------------------------------------

def rotulo(texto: str) -> str:
    """Formata uma data para o painel: '2026-11-13T06:00:00Z' -> '13/11 06:00Z'.

    Parâmetros:
        texto: data ISO.

    Retorna:
        O rótulo; se a data for ilegível, o texto como veio.
    """
    quando = datas.ler_iso(texto)
    if quando is None:
        return texto
    return f'{quando:%d/%m %H:%MZ}'


def situacao(validade: str, referencia: datetime | None = None) -> tuple[str, str]:
    """Classifica a validade como no original: expirada, próxima (< 6 h) ou válida.

    Parâmetros:
        validade: data ISO da validade.
        referencia: o "agora" a usar (testes); None = agora em UTC.

    Retorna:
        (texto, cor do Quasar: 'negative', 'warning' ou 'positive').
    """
    quando = datas.ler_iso(validade)
    if quando is None:
        return 'validade ilegível', 'warning'
    horas = (quando - (referencia or datetime.now(UTC))).total_seconds() / 3600
    if horas < 0:
        return 'expirada', 'negative'
    if horas < 6:
        return 'próxima (< 6 h)', 'warning'
    return 'válida', 'positive'
