"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Radar e satélite pela API de produtos da REDEMET (funciona sem VPN).

A API devolve, para cada produto, o endereço de uma imagem PNG e a área que ela cobre.
As imagens estão em grade de latitude/longitude; o mapa usa Mercator. Por isso elas
passam pelo nosso servidor (/redemet/imagem), que:
  1. só aceita imagens que a API já listou (`_conhecidas`): o endereço e a área vêm da API,
     nunca do navegador — limites inventados gerariam trabalho de CPU e cache à vontade;
  2. reprojeta as linhas para Mercator (sem isso o satélite fica deslocado no sul);
  3. no radar, aplica o mesmo filtro de dBZ do GeoServer (as cores são as mesmas).
A api_key fica só no servidor.

Quem usa: as camadas de radar, satélite e raios (pela API), a animação, a sonda e o painel
de impacto (que relê os PNGs originais de radar).

Caches (todos em memória, perdidos ao reiniciar — de propósito: os dados envelhecem em minutos):
  _cache_api   resposta JSON da API por (caminho, parâmetros), por RADAR_CACHE_S/SATELITE_CACHE_S.
               Vários navegadores abertos fazem uma consulta só.
  _pngs        PNG ORIGINAL de cada imagem de radar (~3 KB cada). A sonda e o impacto leem o
               dBZ dele. Guardamos o PNG, e não a matriz decodificada (640 KB cada): decodificar
               um PNG de 400×400 leva ~1 ms, e 400 matrizes ocupariam ~250 MB.
  _cache_img   imagem PRONTA para o navegador (reprojetada, filtrada) por (caminho, dBZ).
               Permite à animação preparar tudo antes de tocar.
  _conhecidas  cada Imagem que a API listou, por caminho: de onde a rota tira a área.
Todos têm tamanho máximo e descartam o item mais antigo (ver `cache.guardar`).
"""
import asyncio
import io
import logging
import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
import numpy as np
from PIL import Image

import cache
import config
import datas
import geo
import radar

log = logging.getLogger(__name__)

# Produto -> (rótulo, raio de alcance em km)
PRODUTOS_RADAR = {
    'maxcappi': ('MAXCAPPI', 400),
    '03km': ('CAPPI 3 km', 250),
    '05km': ('CAPPI 5 km', 250),
    '07km': ('CAPPI 7 km', 250),
    '10km': ('CAPPI 10 km', 250),
}
SATELITES = {'realcada': 'Infravermelho realçado', 'ir': 'Infravermelho', 'vis': 'Visível'}

RADAR_CACHE_S = 60
SATELITE_CACHE_S = 120
IMAGENS_EM_CACHE = 400           # uma animação de 3 h usa ~250 imagens de radar + ~20 de satélite
RADAR_QUADROS_MAX = 12           # a API não devolve mais que isso (um quadro a cada 20 min)
SATELITE_QUADROS_MAX = 20
RADAR_VELHA_S = 40 * 60          # numa animação, imagem de estação mais velha que isso some
CAMINHO = re.compile(r'^(radar|satelite)/[\w\-/:.]+\.png$')   # caminho relativo no servidor estático


class Indisponivel(Exception):
    """A API da REDEMET não respondeu ou recusou o pedido.

    A mensagem já vem pronta para a tela ('API da REDEMET indisponível (ConnectError)'):
    quem captura só faz `str(erro)`, sem remontar o texto em cada lugar.
    """


@dataclass(frozen=True, slots=True)
class Imagem:
    """Uma imagem listada pela API (de uma estação de radar ou do satélite).

    Atributos:
        caminho: caminho relativo a REDEMET_ESTATICO_URL ('' se a API mandou um inválido).
        sul, norte: latitudes dos limites da imagem, em graus.
        oeste, leste: longitudes dos limites da imagem, em graus.
        quando: horário da imagem (UTC); None se ilegível.
        codigo: no radar, o código da estação ('al', 'be'...); no satélite, ''.
    """
    caminho: str
    sul: float
    norte: float
    oeste: float
    leste: float
    quando: datetime | None
    codigo: str = ''

    @property
    def limites(self) -> list[list[float]]:
        """Limites no formato do Leaflet: [[sul, oeste], [norte, leste]]."""
        return [[self.sul, self.oeste], [self.norte, self.leste]]

    def url(self, dbz_min: int = 0) -> str:
        """Endereço no nosso servidor (o navegador nunca fala direto com a REDEMET).

        Só o caminho: a área quem sabe é o servidor (`_conhecidas`), não o navegador.

        Parâmetros:
            dbz_min: limiar do filtro de dBZ (0 = sem filtro).

        Retorna:
            '/redemet/imagem?c=<caminho>' (com '&dbz=<limiar>' se houver filtro).
        """
        parametros = {'c': self.caminho}
        if dbz_min:
            parametros['dbz'] = dbz_min
        return '/redemet/imagem?' + urlencode(parametros)


_cliente = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
_cache_api: dict[tuple, tuple[float, object]] = {}
_cache_img: dict[tuple, tuple[bytes, str]] = {}
_pngs: dict[str, bytes] = {}
_conhecidas: dict[str, Imagem] = {}
IMAGENS_CONHECIDAS = 2000        # ~28 estações × 12 quadros × alguns produtos, mais o satélite


def _conhecer(imagens: Iterable[Imagem]) -> None:
    """Anota as imagens que a API listou: só elas podem ser pedidas em /redemet/imagem.

    Parâmetros:
        imagens: imagens recém-listadas; as sem caminho são ignoradas.
    """
    for imagem in imagens:
        if imagem.caminho:
            cache.guardar(_conhecidas, imagem.caminho, imagem, IMAGENS_CONHECIDAS)


# ---------------------------------------------------------------------------
# Conversões (funções puras)
# ---------------------------------------------------------------------------

def _data(texto: str | None) -> datetime | None:
    """Lê uma data da API: '2026-11-13 16:08:07' (UTC). Só os 19 primeiros caracteres valem.

    Retorna:
        O datetime em UTC, ou None se o texto faltar ou for ilegível.
    """
    return datas.ler_iso((texto or '')[:19])


def _relativo(url: str | None) -> str | None:
    """Tira o endereço do servidor estático da REDEMET de uma URL que a API devolveu.

    Parâmetros:
        url: URL completa da imagem.

    Retorna:
        O caminho relativo, ou None se a URL for de outro servidor ou o caminho for inválido.
    """
    prefixo = config.REDEMET_ESTATICO_URL + '/'
    if not url or not url.startswith(prefixo):
        return None
    caminho = url.removeprefix(prefixo)
    if not caminho_valido(caminho):
        return None
    return caminho


def caminho_valido(caminho: str) -> bool:
    """Confere um caminho de imagem (segurança do proxy, DECISOES 1.4).

    Parâmetros:
        caminho: caminho relativo, como 'radar/maxcappi/....png'.

    Retorna:
        True se segue o formato CAMINHO (radar ou satélite, terminando em .png) e não tem '..'.
    """
    return bool(CAMINHO.match(caminho)) and '..' not in caminho


def _imagens_estacoes(estacoes: list[dict]) -> list[Imagem]:
    """Converte um quadro da resposta do radar em uma Imagem por estação.

    Parâmetros:
        estacoes: itens da API, um por estação ('path', 'lat_min', 'localidade'...).

    Retorna:
        As imagens legíveis; uma estação com área ilegível fica de fora. Caminho inválido
        vira '' (a imagem entra na lista, mas não pode ser pedida).
    """
    imagens = []
    for estacao in estacoes:
        caminho = _relativo(estacao.get('path'))
        try:
            imagens.append(Imagem(
                caminho or '',
                float(estacao['lat_min']), float(estacao['lat_max']),
                float(estacao['lon_min']), float(estacao['lon_max']),
                _data(estacao.get('data')),
                str(estacao.get('localidade', '')).lower(),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return imagens


def converter_radar(dados: dict) -> list[Imagem]:
    """Converte a resposta de /produtos/radar/<tipo> nas imagens mais recentes.

    Parâmetros:
        dados: o campo 'data' da resposta da API.

    Retorna:
        Uma Imagem por estação, do último quadro (com anima=N, o último é o mais novo);
        lista vazia se não houver quadros.
    """
    quadros = dados.get('radar') or []
    if not quadros:
        return []
    return _imagens_estacoes(quadros[-1])


@dataclass(frozen=True, slots=True)
class QuadroRadar:
    """Um momento da animação do radar: as imagens das estações nesse horário.

    Atributos:
        quando: horário do quadro (o da imagem mais nova dele), UTC.
        imagens: imagens das estações que estavam em dia nesse horário.
    """
    quando: datetime
    imagens: tuple[Imagem, ...]


def converter_radar_quadros(dados: dict) -> list[QuadroRadar]:
    """Converte a resposta com anima=N em quadros em ordem de tempo, sem repetições.

    O horário do quadro é o da imagem mais nova (o rótulo "anima" da API não serve).
    Estações paradas (imagem bem mais velha que o quadro) ficam de fora do quadro.

    Parâmetros:
        dados: o campo 'data' da resposta da API.

    Retorna:
        Os quadros, do mais antigo ao mais novo; quadro sem nenhuma imagem datada fica de fora.
    """
    quadros = []
    vistos = set()
    for estacoes in dados.get('radar') or []:
        imagens = _imagens_estacoes(estacoes)
        horarios = [imagem.quando for imagem in imagens if imagem.caminho and imagem.quando]
        if not horarios:
            continue
        quando = max(horarios)
        recentes = _imagens_recentes(imagens, quando)
        assinatura = frozenset(imagem.caminho for imagem in recentes)
        if assinatura in vistos:      # a API repete o último quadro
            continue
        vistos.add(assinatura)
        quadros.append(QuadroRadar(quando, recentes))
    return sorted(quadros, key=lambda quadro: quadro.quando)


def _imagens_recentes(imagens: list[Imagem], quando: datetime) -> tuple[Imagem, ...]:
    """Fica com as imagens pedíveis e no máximo RADAR_VELHA_S mais velhas que o quadro.

    Parâmetros:
        imagens: imagens das estações num quadro.
        quando: horário do quadro (o da imagem mais nova).

    Retorna:
        As imagens com caminho e horário que não estão paradas, na ordem recebida.
    """
    recentes = []
    for imagem in imagens:
        if not imagem.caminho or not imagem.quando:
            continue
        if (quando - imagem.quando).total_seconds() <= RADAR_VELHA_S:
            recentes.append(imagem)
    return tuple(recentes)


def converter_satelite_quadros(dados: dict) -> list[Imagem]:
    """Converte a resposta do satélite com anima=N em uma Imagem por horário.

    Parâmetros:
        dados: o campo 'data' da resposta da API.

    Retorna:
        As imagens legíveis e datadas, da mais antiga à mais nova.
    """
    area = dados.get('lat_lon') or {}
    imagens = []
    for item in dados.get('satelite') or []:
        imagem = _imagem_satelite(area, item)
        if imagem and imagem.quando:
            imagens.append(imagem)
    return sorted(imagens, key=lambda imagem: imagem.quando)


def converter_satelite(dados: dict) -> Imagem | None:
    """Converte a resposta de /produtos/satelite/<tipo> na imagem mais recente.

    Parâmetros:
        dados: o campo 'data' da resposta da API.

    Retorna:
        A última imagem da lista, ou None se não houver imagens ou ela for ilegível.
    """
    imagens = dados.get('satelite') or []
    if not imagens:
        return None
    return _imagem_satelite(dados.get('lat_lon') or {}, imagens[-1])


def _imagem_satelite(area: dict, item: dict) -> Imagem | None:
    """Converte um horário da resposta do satélite em Imagem. A área é a mesma em todos.

    Parâmetros:
        area: o campo 'lat_lon' da resposta (lat_min, lat_max, lon_min, lon_max).
        item: um horário da lista 'satelite' ('path', 'data').

    Retorna:
        A Imagem, ou None se o caminho ou a área forem ilegíveis.
    """
    caminho = _relativo(item.get('path'))
    if not caminho:
        return None
    try:
        return Imagem(caminho, float(area['lat_min']), float(area['lat_max']),
                      float(area['lon_min']), float(area['lon_max']), _data(item.get('data')))
    except (KeyError, TypeError, ValueError):
        return None


def reprojetar(img: np.ndarray, sul: float, norte: float) -> np.ndarray:
    """Reprojeta uma imagem em grade lat/lon (linhas igualmente espaçadas em latitude) para Mercator.

    As colunas não mudam (longitude é linear nas duas). Cada linha da saída pega a
    linha de origem da latitude correspondente (vizinho mais próximo: não inventa cor,
    então o filtro de dBZ continua reconhecendo as cores). Ver DECISOES 3.2.

    Parâmetros:
        img: matriz da imagem (altura × largura × canais), linha 0 no norte.
        sul, norte: latitudes da última e da primeira linha, em graus.

    Retorna:
        Matriz do mesmo tamanho, com as linhas em Mercator.
    """
    altura = img.shape[0]
    # y de Mercator de cada linha de saída (igualmente espaçados) e a latitude correspondente
    y = np.linspace(geo.mercator_y(norte), geo.mercator_y(sul), altura)
    lat = np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)
    # linha de origem (igualmente espaçadas em latitude) mais próxima dessa latitude
    linha_aproximada = np.round((norte - lat) / (norte - sul) * (altura - 1)).astype(int)
    linha_de_origem = np.clip(linha_aproximada, 0, altura - 1)
    return img[linha_de_origem]


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------

async def _consultar(caminho: str, validade_s: float, **parametros) -> dict:
    """Consulta a API de produtos da REDEMET, com cache (`_cache_api`).

    Fluxo:
        1. Resposta guardada há menos de `validade_s`: devolve a guardada.
        2. Senão, consulta a API; falha de rede, JSON ilegível ou "status" falso: Indisponivel.
        3. Guarda e devolve o campo 'data'.

    Parâmetros:
        caminho: produto relativo a REDEMET_PRODUTOS_URL ('radar/maxcappi', 'stsc'...).
        validade_s: por quanto tempo a resposta guardada vale, em segundos.
        parametros: parâmetros extras da consulta (ex.: anima=12).

    Retorna:
        O campo 'data' da resposta; {} se a API não tiver produto no momento.
    """
    chave = (caminho, tuple(sorted(parametros.items())))
    guardado = _cache_api.get(chave)
    if guardado and cache.ainda_vale(guardado[0], validade_s):
        return guardado[1]
    try:
        resposta = await _cliente.get(f'{config.REDEMET_PRODUTOS_URL}/{caminho}',
                                      params={'api_key': config.REDEMET_API_KEY, **parametros})
        resposta.raise_for_status()
        corpo = resposta.json()
    except (httpx.HTTPError, ValueError) as erro:
        raise Indisponivel(f'API da REDEMET indisponível ({type(erro).__name__})') from erro
    if not corpo.get('status'):
        raise Indisponivel(f"API da REDEMET indisponível ({str(corpo.get('message', 'pedido recusado'))[:80]})")
    # Sem produto no momento (de madrugada, por exemplo), a API devolve "data": [] em vez de
    # um objeto. Todos os conversores esperam um dict: lista vazia vira "nenhuma imagem".
    dados = corpo.get('data')
    if not isinstance(dados, dict):
        dados = {}
    _cache_api[chave] = (time.monotonic(), dados)
    return dados


async def radar_api(produto: str) -> list[Imagem]:
    """Busca a imagem mais recente de cada estação de radar.

    Parâmetros:
        produto: chave de PRODUTOS_RADAR ('maxcappi', '03km'...); outra levanta ValueError.

    Retorna:
        Uma Imagem por estação (já anotadas como pedíveis). Levanta Indisponivel se a API falhar.
    """
    if produto not in PRODUTOS_RADAR:
        raise ValueError(produto)
    imagens = converter_radar(await _consultar(f'radar/{produto}', RADAR_CACHE_S))
    _conhecer(imagens)
    return imagens


async def satelite(tipo: str) -> Imagem | None:
    """Busca a imagem de satélite mais recente.

    Parâmetros:
        tipo: chave de SATELITES ('realcada', 'ir', 'vis'); outra levanta ValueError.

    Retorna:
        A Imagem, ou None se a API não tiver imagem. Levanta Indisponivel se a API falhar.
    """
    if tipo not in SATELITES:
        raise ValueError(tipo)
    imagem = converter_satelite(await _consultar(f'satelite/{tipo}', SATELITE_CACHE_S))
    if imagem:
        _conhecer([imagem])
    return imagem


async def radar_quadros(produto: str) -> list[QuadroRadar]:
    """Busca os quadros recentes do radar (animação e painel de impacto).

    Parâmetros:
        produto: chave de PRODUTOS_RADAR; outra levanta ValueError.

    Retorna:
        Até RADAR_QUADROS_MAX quadros, do mais antigo ao mais novo. Levanta Indisponivel se a
        API falhar.
    """
    if produto not in PRODUTOS_RADAR:
        raise ValueError(produto)
    dados = await _consultar(f'radar/{produto}', RADAR_CACHE_S, anima=RADAR_QUADROS_MAX)
    quadros = converter_radar_quadros(dados)
    for quadro in quadros:
        _conhecer(quadro.imagens)
    return quadros


async def satelite_quadros(tipo: str, quantos: int) -> list[Imagem]:
    """Busca as imagens recentes de satélite (animação).

    Parâmetros:
        tipo: chave de SATELITES; outra levanta ValueError.
        quantos: quantas imagens pedir (limitado a 1..SATELITE_QUADROS_MAX).

    Retorna:
        As imagens, da mais antiga à mais nova. Levanta Indisponivel se a API falhar.
    """
    if tipo not in SATELITES:
        raise ValueError(tipo)
    quantos = max(1, min(quantos, SATELITE_QUADROS_MAX))
    imagens = converter_satelite_quadros(await _consultar(f'satelite/{tipo}', SATELITE_CACHE_S, anima=quantos))
    _conhecer(imagens)
    return imagens


# ---------------------------------------------------------------------------
# Raios (produto "stsc"): células de 0,1° com descargas numa janela móvel
# ---------------------------------------------------------------------------

CELULA_NOVA_MIN = 15             # célula de raios ativa há menos que isso = convecção nova
RAIOS_QUADROS_MAX = 27           # a API não devolve mais que isso (~3 h, um quadro a cada 6–7 min)
CELULA_GRAUS = 0.1
# Área desenhada (cobre o que a REDEMET devolve) e resolução: 2 pixels por célula na longitude
RAIOS_SUL = -60.0
RAIOS_NORTE = 15.0
RAIOS_OESTE = -100.0
RAIOS_LESTE = -25.0
RAIOS_PX_POR_CELULA = 2
# Cor pela persistência: há quanto tempo a célula aparece em todos os quadros seguidos
RAIOS_CLASSES = [(CELULA_NOVA_MIN, '#ef4444', f'ativa há menos de {CELULA_NOVA_MIN} min (convecção nova)'),
                 (30, '#facc15', 'ativa há 15 a 30 min'),
                 (60, '#22c55e', 'ativa há 30 a 60 min'),
                 (None, '#3b82f6', 'ativa há mais de 1 h')]


@dataclass(frozen=True)
class QuadroRaios:
    """Um quadro de raios: as células com descargas num horário.

    Atributos:
        quando: horário do quadro (UTC).
        celulas: (lat, lon, minutos ativa) de cada célula — lat/lon do centro, em graus;
            minutos ativa = há quanto tempo a célula aparece sem interrupção.
    """
    quando: datetime
    celulas: tuple[tuple[float, float, int], ...]

    @property
    def chave(self) -> str:
        """Identificador do quadro na rota /redemet/raios: 'AAAAMMDDTHHMM'."""
        return f'{self.quando:%Y%m%dT%H%M}'

    @property
    def limites(self) -> list[list[float]]:
        """Área desenhada, no formato do Leaflet: [[sul, oeste], [norte, leste]]."""
        return [[RAIOS_SUL, RAIOS_OESTE], [RAIOS_NORTE, RAIOS_LESTE]]

    def url(self) -> str:
        """Endereço da imagem deste quadro no nosso servidor."""
        return f'/redemet/raios?q={self.chave}'


QUADROS_RAIOS_GUARDADOS = 200    # ~20 h de quadros (um a cada 6–7 min): sobra para a animação de 3 h
_quadros_raios: dict[str, QuadroRaios] = {}      # chave -> quadro (para a rota desenhar a imagem)


def _horarios_raios(rotulos: list[str], ultima: datetime) -> list[datetime]:
    """Converte os rótulos 'HH:MM' dos quadros em datetime.

    A data vem da última ocorrência; volta um dia se o horário passar dela (meia-noite).

    Parâmetros:
        rotulos: horários dos quadros, como 'HH:MM'.
        ultima: horário da última descarga (UTC).

    Retorna:
        Um datetime por rótulo; lista vazia se algum rótulo for ilegível.
    """
    horarios = []
    for rotulo in rotulos:
        try:
            hora, minuto = map(int, rotulo.split(':'))
        except ValueError:
            return []
        horario = ultima.replace(hour=hora, minute=minuto, second=0, microsecond=0)
        if horario > ultima + timedelta(minutes=1):
            horario -= timedelta(days=1)
        horarios.append(horario)
    return horarios


def converter_raios(dados: dict) -> list[QuadroRaios]:
    """Converte a resposta do stsc (com anima=N) em quadros de raios.

    Para cada célula calcula há quanto tempo ela aparece sem interrupção (só dá para saber
    dentro dos quadros recebidos).

    Fluxo:
        1. Data de cada quadro, a partir dos rótulos 'HH:MM' e da última ocorrência.
        2. Células de cada quadro (`_celulas_do_quadro`).
        3. Persistência de cada célula ao longo dos quadros (`_quadros_com_persistencia`).

    Parâmetros:
        dados: o campo 'data' da resposta da API ('info', 'anima', 'stsc').

    Retorna:
        Os quadros, na ordem da API (do mais antigo ao mais novo); lista vazia se a resposta
        for incoerente (sem última ocorrência ou com rótulos e quadros em número diferente).
    """
    info = dados.get('info') or {}
    rotulos = dados.get('anima') or []
    listas = dados.get('stsc') or []
    ultima = _data(info.get('ultima_ocorrencia'))
    if not ultima or len(rotulos) != len(listas):
        return []
    horarios = _horarios_raios(rotulos, ultima)
    if len(horarios) != len(listas):
        return []
    conjuntos = [_celulas_do_quadro(pontos) for pontos in listas]
    return _quadros_com_persistencia(horarios, conjuntos)


def _celulas_do_quadro(pontos: list[dict]) -> set[tuple[float, float]]:
    """Lê as células de um quadro do stsc (pontos ilegíveis ficam de fora).

    Parâmetros:
        pontos: itens da API, cada um com 'la' e 'lo' (centro da célula, em graus).

    Retorna:
        Conjunto de (lat, lon) arredondados a 2 casas.
    """
    celulas = set()
    for ponto in pontos:
        try:
            celulas.add((round(float(ponto['la']), 2), round(float(ponto['lo']), 2)))
        except (KeyError, TypeError, ValueError):
            continue
    return celulas


def _quadros_com_persistencia(horarios: list[datetime],
                              conjuntos: list[set[tuple[float, float]]]) -> list[QuadroRaios]:
    """Monta os quadros, com os minutos que cada célula está ativa sem interrupção.

    Uma célula que some de um quadro recomeça a contar quando volta.

    Parâmetros:
        horarios: horário de cada quadro, em ordem de tempo.
        conjuntos: células (lat, lon) de cada quadro, na mesma ordem.

    Retorna:
        Um QuadroRaios por horário, com as células em ordem (lat, lon).
    """
    quadros: list[QuadroRaios] = []
    desde: dict[tuple, datetime] = {}         # célula -> desde quando aparece sem parar
    for quando, celulas in zip(horarios, conjuntos):
        desde = {celula: desde.get(celula, quando) for celula in celulas}     # some do quadro = recomeça a contar
        celulas_do_quadro = []
        for lat, lon in sorted(celulas):
            minutos_ativa = round((quando - desde[(lat, lon)]).total_seconds() / 60)
            celulas_do_quadro.append((lat, lon, minutos_ativa))
        quadros.append(QuadroRaios(quando, tuple(celulas_do_quadro)))
    return quadros


def classe_raio(minutos: int) -> int:
    """Dá a classe de cor (índice em RAIOS_CLASSES) de uma célula pela persistência.

    Parâmetros:
        minutos: há quanto tempo a célula está ativa, em minutos.

    Retorna:
        O índice da primeira classe cujo limite é maior que `minutos` (a última não tem limite).
    """
    for indice, (limite, _, _) in enumerate(RAIOS_CLASSES):
        if limite is None or minutos < limite:
            return indice
    return len(RAIOS_CLASSES) - 1


def desenhar_raios(quadro: QuadroRaios) -> bytes:
    """Desenha um quadro de raios: um quadrado colorido por célula, o resto transparente.

    A imagem já sai em Mercator e cobre RAIOS_SUL..RAIOS_NORTE × RAIOS_OESTE..RAIOS_LESTE.
    PNG com paleta: um byte por pixel e 5 cores (índice 0 = transparente).

    Parâmetros:
        quadro: o quadro a desenhar.

    Retorna:
        O conteúdo do PNG.
    """
    largura = round((RAIOS_LESTE - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
    px_por_unidade = largura / math.radians(RAIOS_LESTE - RAIOS_OESTE)
    y_norte = geo.mercator_y(RAIOS_NORTE)
    altura = round((y_norte - geo.mercator_y(RAIOS_SUL)) * px_por_unidade)
    img = np.zeros((altura, largura), np.uint8)              # 0 = transparente
    meia = CELULA_GRAUS / 2
    for lat, lon, minutos in quadro.celulas:
        if not (RAIOS_SUL < lat < RAIOS_NORTE and RAIOS_OESTE < lon < RAIOS_LESTE):
            continue
        x0 = round((lon - meia - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
        x1 = round((lon + meia - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
        y0 = round((y_norte - geo.mercator_y(lat + meia)) * px_por_unidade)
        y1 = round((y_norte - geo.mercator_y(lat - meia)) * px_por_unidade)
        # a célula ocupa pelo menos 1 pixel em cada direção
        img[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)] = classe_raio(minutos) + 1
    imagem = Image.fromarray(img)                 # array 2D uint8: modo L; putpalette o torna P
    imagem.putpalette(_paleta_raios())
    saida = io.BytesIO()
    imagem.save(saida, 'PNG', transparency=0, optimize=True)
    return saida.getvalue()


def _paleta_raios() -> list[int]:
    """Paleta do PNG de raios: índice 0 (preto, transparente) e depois a cor de cada classe.

    Retorna:
        Lista plana [R, G, B, R, G, B, ...] para `Image.putpalette`.
    """
    paleta = [0, 0, 0]
    for _, cor, _ in RAIOS_CLASSES:              # cor em '#rrggbb'
        paleta += [int(cor[1:3], 16), int(cor[3:5], 16), int(cor[5:7], 16)]
    return paleta


async def raios_quadros() -> list[QuadroRaios]:
    """Busca os quadros recentes de raios e guarda-os para a rota /redemet/raios.

    Retorna:
        Até RAIOS_QUADROS_MAX quadros, do mais antigo ao mais novo. Levanta Indisponivel se a
        API falhar.
    """
    quadros = converter_raios(await _consultar('stsc', RADAR_CACHE_S, anima=RAIOS_QUADROS_MAX))
    for quadro in quadros:                              # chegam em ordem de tempo: o FIFO descarta o mais velho
        cache.guardar(_quadros_raios, quadro.chave, quadro, QUADROS_RAIOS_GUARDADOS)
    return quadros


def imagem_raios(chave: str) -> tuple[int, bytes, str]:
    """Para a rota /redemet/raios: a imagem de um quadro já consultado.

    Parâmetros:
        chave: `QuadroRaios.chave` ('AAAAMMDDTHHMM').

    Retorna:
        (status HTTP, conteúdo, tipo); 404 se o quadro não foi consultado (ou já saiu do cache).
    """
    quadro = _quadros_raios.get(chave)
    if quadro is None:
        return 404, b'quadro desconhecido', 'text/plain'
    chave_img = ('raios', chave)
    if chave_img not in _cache_img:
        cache.guardar(_cache_img, chave_img, (desenhar_raios(quadro), 'image/png'), IMAGENS_EM_CACHE)
    return 200, *_cache_img[chave_img]


async def buscar_imagem(caminho: str, dbz_min: int = 0) -> tuple[int, bytes, str]:
    """Entrega a imagem reprojetada (e filtrada, no radar) para o navegador.

    Só imagens que a API listou (`_conhecidas`); a área vem de lá. Depois de reiniciar o
    servidor, as camadas consultam a API de novo antes de pedir imagens.

    Fluxo:
        1. Confere o pedido (`_recusar_pedido`): 400, 404 ou 502.
        2. Imagem já pronta em `_cache_img`: devolve.
        3. Senão, baixa, prepara e guarda (`_baixar_e_preparar`).

    Parâmetros:
        caminho: caminho relativo da imagem ('radar/...png' ou 'satelite/...png').
        dbz_min: limiar do filtro de dBZ (0 = sem filtro), de 0 a 75.

    Retorna:
        (status HTTP, corpo, tipo do conteúdo).
    """
    recusa = _recusar_pedido(caminho, dbz_min)
    if recusa is not None:
        return recusa
    chave = (caminho, dbz_min)
    if chave in _cache_img:
        return 200, *_cache_img[chave]
    return await _baixar_e_preparar(_conhecidas[caminho], dbz_min)


def _recusar_pedido(caminho: str, dbz_min: int) -> tuple[int, bytes, str] | None:
    """Confere um pedido de /redemet/imagem (segurança do proxy, DECISOES 1.4).

    Parâmetros:
        caminho: caminho relativo pedido pelo navegador.
        dbz_min: limiar de dBZ pedido.

    Retorna:
        A resposta de erro (400 pedido inválido, 404 imagem que a API não listou, 502 área
        impossível vinda da API), ou None se o pedido pode seguir.
    """
    if not caminho_valido(caminho) or not 0 <= dbz_min <= 75:
        return 400, b'pedido recusado', 'text/plain'
    imagem = _conhecidas.get(caminho)
    if imagem is None:
        return 404, b'imagem desconhecida', 'text/plain'
    if not -85 < imagem.sul < imagem.norte < 85:
        return 502, b'area invalida na resposta da REDEMET', 'text/plain'
    return None


async def _baixar_e_preparar(imagem: Imagem, dbz_min: int) -> tuple[int, bytes, str]:
    """Baixa o PNG original, prepara a imagem numa thread e guarda em `_cache_img`.

    Parâmetros:
        imagem: a imagem conhecida (dá o caminho e a área).
        dbz_min: limiar do filtro de dBZ (0 = sem filtro).

    Retorna:
        (200, conteúdo, tipo), ou 502 se a REDEMET falhou ou o PNG está corrompido.
    """
    png = await png_original(imagem.caminho)
    if png is None:
        return 502, b'REDEMET indisponivel', 'text/plain'
    try:
        # Decodificar, reprojetar e codificar usa CPU (~0,5 s numa imagem de satélite). Numa
        # thread, o laço do servidor continua atendendo os outros usuários enquanto isso.
        conteudo, tipo = await asyncio.to_thread(_preparar, png, imagem.caminho, imagem.sul, imagem.norte, dbz_min)
    except OSError as erro:              # PNG corrompido
        log.warning('imagem da REDEMET ilegível: %s (%s)', imagem.caminho, erro)
        return 502, b'imagem da REDEMET ilegivel', 'text/plain'
    cache.guardar(_cache_img, (imagem.caminho, dbz_min), (conteudo, tipo), IMAGENS_EM_CACHE)
    return 200, conteudo, tipo


def _preparar(png: bytes, caminho: str, sul: float, norte: float, dbz_min: int) -> tuple[bytes, str]:
    """Converte o PNG original (grade lat/lon) na imagem para o navegador, em Mercator.

    Roda numa thread (DECISOES 4.2).

    Parâmetros:
        png: conteúdo do PNG original.
        caminho: caminho relativo; diz se é satélite ou radar.
        sul, norte: latitudes dos limites da imagem, em graus.
        dbz_min: limiar do filtro de dBZ (0 = sem filtro; só no radar).

    Retorna:
        (conteúdo, tipo): WebP no satélite, PNG no radar. OSError se o PNG for inválido.
    """
    img = reprojetar(decodificar(png), sul, norte)
    saida = io.BytesIO()
    if caminho.startswith('satelite/'):
        # Satélite é foto, sem transparência: WebP fica ~3x menor que PNG (450 KB x 1,25 MB)
        Image.fromarray(img).convert('RGB').save(saida, 'WEBP', quality=85)
        return saida.getvalue(), 'image/webp'
    # Radar: PNG sem perdas, cores exatas (o filtro de dBZ e a sonda dependem delas).
    # O filtro age na matriz, antes de codificar: um PNG só, em vez de codificar, decodificar
    # e codificar de novo.
    if dbz_min:
        img = radar.filtrar_rgba(img, dbz_min)
    Image.fromarray(img).save(saida, 'PNG', compress_level=1)
    return saida.getvalue(), 'image/png'


def decodificar(png: bytes) -> np.ndarray:
    """Decodifica um PNG numa matriz altura × largura × 4 (RGBA, uint8).

    Parâmetros:
        png: conteúdo do arquivo.

    Retorna:
        A matriz. Levanta OSError se o arquivo for inválido.
    """
    return np.array(Image.open(io.BytesIO(png)).convert('RGBA'))


async def png_original(caminho: str) -> bytes | None:
    """Baixa o PNG como está no servidor estático da REDEMET.

    Os de radar ficam em `_pngs` (pequenos, e a sonda e o impacto voltam a eles); os de
    satélite não (1,4 MB cada, e só servem para gerar a imagem pronta, que já fica em cache).

    Parâmetros:
        caminho: caminho relativo da imagem.

    Retorna:
        O conteúdo do PNG, ou None se não deu para baixar.
    """
    if caminho in _pngs:
        return _pngs[caminho]
    try:
        resposta = await _cliente.get(f'{config.REDEMET_ESTATICO_URL}/{caminho}')
        resposta.raise_for_status()
    except httpx.HTTPError as erro:
        log.warning('imagem da REDEMET falhou: %s (%s)', caminho, type(erro).__name__)
        return None
    if caminho.startswith('radar/'):
        cache.guardar(_pngs, caminho, resposta.content, IMAGENS_EM_CACHE)
    return resposta.content


async def imagem_bruta(imagem: Imagem) -> np.ndarray | None:
    """Lê a matriz RGBA da imagem ORIGINAL (grade lat/lon, sem reprojetar), para o dBZ de um ponto.

    Parâmetros:
        imagem: a imagem de radar.

    Retorna:
        A matriz, ou None se não deu para baixar ou o PNG for inválido.
    """
    png = await png_original(imagem.caminho)
    if png is None:
        return None
    try:
        return decodificar(png)
    except OSError:
        return None


async def preparar(imagens: list[tuple[Imagem, int]], simultaneas: int = 8) -> None:
    """Baixa e prepara de antemão (cache) as imagens de uma animação.

    Quando o navegador pedir, elas saem na hora.

    Parâmetros:
        imagens: pares (Imagem, dbz_min) a preparar.
        simultaneas: quantas imagens preparar ao mesmo tempo, no máximo.
    """
    limite = asyncio.Semaphore(simultaneas)

    async def preparar_uma(imagem: Imagem, dbz_min: int) -> None:
        async with limite:
            await buscar_imagem(imagem.caminho, dbz_min)

    await asyncio.gather(*(preparar_uma(imagem, dbz_min) for imagem, dbz_min in imagens))
