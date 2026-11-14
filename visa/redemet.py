"""
Radar e satélite pela API de produtos da REDEMET (funciona sem VPN).

A API devolve, para cada produto, o endereço de uma imagem PNG e a área que ela cobre.
As imagens estão em grade de latitude/longitude; o mapa usa Mercator. Por isso elas
passam pelo nosso servidor (/redemet/imagem), que:
  1. só aceita imagens do servidor estático da REDEMET (nada de endereço arbitrário);
  2. reprojeta as linhas para Mercator (sem isso o satélite fica deslocado no sul);
  3. no radar, aplica o mesmo filtro de dBZ do GeoServer (as cores são as mesmas).
A api_key fica só no servidor.

Caches (todos em memória, perdidos ao reiniciar — de propósito: os dados envelhecem em minutos):
  _cache_api   resposta JSON da API por (caminho, parâmetros), por RADAR_CACHE_S/SATELITE_CACHE_S.
               Vários navegadores abertos fazem uma consulta só.
  _pngs        PNG ORIGINAL de cada imagem de radar (~3 KB cada). A sonda e o impacto leem o
               dBZ dele. Guardamos o PNG, e não a matriz decodificada (640 KB cada): decodificar
               um PNG de 400×400 leva ~1 ms, e 400 matrizes ocupariam ~250 MB.
  _cache_img   imagem PRONTA para o navegador (reprojetada, filtrada) por (caminho, área, dBZ).
               Permite à animação preparar tudo antes de tocar.
Todos têm tamanho máximo e descartam o item mais antigo (ver `guardar_no_cache`).
"""
import asyncio
import io
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import numpy as np
from PIL import Image

import config
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


@dataclass(frozen=True)
class Imagem:
    caminho: str                 # relativo a REDEMET_ESTATICO_URL
    sul: float
    norte: float
    oeste: float
    leste: float
    quando: datetime | None
    codigo: str = ''             # radar: código da estação ('al', 'be'...)

    @property
    def limites(self) -> list[list[float]]:
        return [[self.sul, self.oeste], [self.norte, self.leste]]

    def url(self, dbz_min: int = 0) -> str:
        """Endereço no nosso servidor (o navegador nunca fala direto com a REDEMET)."""
        parametros = {'c': self.caminho, 's': self.sul, 'n': self.norte}
        if dbz_min:
            parametros['dbz'] = dbz_min
        return '/redemet/imagem?' + urlencode(parametros)


_cliente = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
_cache_api: dict[tuple, tuple[float, object]] = {}
_cache_img: dict[tuple, tuple[bytes, str]] = {}
_pngs: dict[str, bytes] = {}


def guardar_no_cache(cache: dict, chave, valor, limite: int = IMAGENS_EM_CACHE) -> None:
    """Guarda no cache; se passou do limite, descarta o item mais antigo.

    Um dict do Python lembra a ordem de inserção, então `next(iter(cache))` é o mais antigo
    (FIFO). Não é LRU, mas para dados que envelhecem sozinhos dá no mesmo, sem dependência.
    """
    cache[chave] = valor
    while len(cache) > limite:
        cache.pop(next(iter(cache)))


# ---------------------------------------------------------------------------
# Conversões (funções puras)
# ---------------------------------------------------------------------------

def _data(texto: str | None) -> datetime | None:
    """'2026-11-13 16:08:07' (UTC) -> datetime."""
    if not texto:
        return None
    try:
        return datetime.strptime(texto[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _relativo(url: str | None) -> str | None:
    if not url or not url.startswith(config.REDEMET_ESTATICO_URL + '/'):
        return None
    caminho = url.removeprefix(config.REDEMET_ESTATICO_URL + '/')
    return caminho if caminho_valido(caminho) else None


def caminho_valido(caminho: str) -> bool:
    return bool(CAMINHO.match(caminho)) and '..' not in caminho


def converter_radar(dados: dict) -> list[Imagem]:
    """Resposta de /produtos/radar/<tipo> -> uma Imagem por estação (a mais recente)."""
    quadros = dados.get('radar') or []
    if not quadros:
        return []
    lista = []
    for r in quadros[-1]:            # com anima=N vêm N quadros; o último é o mais novo
        caminho = _relativo(r.get('path'))
        try:
            lista.append(Imagem(caminho or '', float(r['lat_min']), float(r['lat_max']),
                                float(r['lon_min']), float(r['lon_max']),
                                _data(r.get('data')), str(r.get('localidade', '')).lower()))
        except (KeyError, TypeError, ValueError):
            continue
    return lista


@dataclass(frozen=True)
class QuadroRadar:
    """Um momento da animação do radar: as imagens das estações nesse horário."""
    quando: datetime
    imagens: tuple[Imagem, ...]


def converter_radar_quadros(dados: dict) -> list[QuadroRadar]:
    """Resposta com anima=N -> quadros em ordem de tempo, sem repetições.

    O horário do quadro é o da imagem mais nova (o rótulo "anima" da API não serve).
    Estações paradas (imagem bem mais velha que o quadro) ficam de fora do quadro.
    """
    quadros, vistos = [], set()
    for estacoes in dados.get('radar') or []:
        imagens = converter_radar({'radar': [estacoes]})
        horarios = [i.quando for i in imagens if i.caminho and i.quando]
        if not horarios:
            continue
        quando = max(horarios)
        validas = tuple(i for i in imagens if i.caminho and i.quando
                        and (quando - i.quando).total_seconds() <= RADAR_VELHA_S)
        assinatura = frozenset(i.caminho for i in validas)
        if assinatura in vistos:      # a API repete o último quadro
            continue
        vistos.add(assinatura)
        quadros.append(QuadroRadar(quando, validas))
    return sorted(quadros, key=lambda q: q.quando)


def converter_satelite_quadros(dados: dict) -> list[Imagem]:
    """Resposta com anima=N -> uma Imagem por horário, em ordem de tempo."""
    area = dados.get('lat_lon') or {}
    lista = []
    for item in dados.get('satelite') or []:
        imagem = converter_satelite({'lat_lon': area, 'satelite': [item]})
        if imagem and imagem.quando:
            lista.append(imagem)
    return sorted(lista, key=lambda i: i.quando)


def converter_satelite(dados: dict) -> Imagem | None:
    """Resposta de /produtos/satelite/<tipo> -> a imagem mais recente."""
    area, imagens = dados.get('lat_lon') or {}, dados.get('satelite') or []
    if not imagens:
        return None
    ultima = imagens[-1]
    caminho = _relativo(ultima.get('path'))
    if not caminho:
        return None
    try:
        return Imagem(caminho, float(area['lat_min']), float(area['lat_max']),
                      float(area['lon_min']), float(area['lon_max']), _data(ultima.get('data')))
    except (KeyError, TypeError, ValueError):
        return None


def _mercator_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def reprojetar(img: np.ndarray, sul: float, norte: float) -> np.ndarray:
    """Imagem em grade lat/lon (linhas igualmente espaçadas em latitude) -> Mercator.

    As colunas não mudam (longitude é linear nas duas). Cada linha da saída pega a
    linha de origem da latitude correspondente (vizinho mais próximo: não inventa cor,
    então o filtro de dBZ continua reconhecendo as cores).
    """
    altura = img.shape[0]
    y = np.linspace(_mercator_y(norte), _mercator_y(sul), altura)
    lat = np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)
    origem = np.clip(np.round((norte - lat) / (norte - sul) * (altura - 1)).astype(int), 0, altura - 1)
    return img[origem]


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------

async def _consultar(caminho: str, validade_s: float, **parametros) -> dict:
    chave = (caminho, tuple(sorted(parametros.items())))
    guardado = _cache_api.get(chave)
    if guardado and time.monotonic() - guardado[0] < validade_s:
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
    _cache_api[chave] = (time.monotonic(), corpo['data'])
    return corpo['data']


async def radar_api(produto: str) -> list[Imagem]:
    if produto not in PRODUTOS_RADAR:
        raise ValueError(produto)
    return converter_radar(await _consultar(f'radar/{produto}', RADAR_CACHE_S))


async def satelite(tipo: str) -> Imagem | None:
    if tipo not in SATELITES:
        raise ValueError(tipo)
    return converter_satelite(await _consultar(f'satelite/{tipo}', SATELITE_CACHE_S))


async def radar_quadros(produto: str) -> list[QuadroRadar]:
    if produto not in PRODUTOS_RADAR:
        raise ValueError(produto)
    dados = await _consultar(f'radar/{produto}', RADAR_CACHE_S, anima=RADAR_QUADROS_MAX)
    return converter_radar_quadros(dados)


async def satelite_quadros(tipo: str, quantos: int) -> list[Imagem]:
    if tipo not in SATELITES:
        raise ValueError(tipo)
    quantos = max(1, min(quantos, SATELITE_QUADROS_MAX))
    return converter_satelite_quadros(await _consultar(f'satelite/{tipo}', SATELITE_CACHE_S, anima=quantos))


# ---------------------------------------------------------------------------
# Raios (produto "stsc"): células de 0,1° com descargas numa janela móvel
# ---------------------------------------------------------------------------

CELULA_NOVA_MIN = 15             # célula de raios ativa há menos que isso = convecção nova
RAIOS_QUADROS_MAX = 27           # a API não devolve mais que isso (~3 h, um quadro a cada 6–7 min)
CELULA_GRAUS = 0.1
# Área desenhada (cobre o que a REDEMET devolve) e resolução: 2 pixels por célula na longitude
RAIOS_SUL, RAIOS_NORTE, RAIOS_OESTE, RAIOS_LESTE = -60.0, 15.0, -100.0, -25.0
RAIOS_PX_POR_CELULA = 2
# Cor pela persistência: há quanto tempo a célula aparece em todos os quadros seguidos
RAIOS_CLASSES = [(CELULA_NOVA_MIN, '#ef4444', f'ativa há menos de {CELULA_NOVA_MIN} min (convecção nova)'),
                 (30, '#facc15', 'ativa há 15 a 30 min'),
                 (60, '#22c55e', 'ativa há 30 a 60 min'),
                 (None, '#3b82f6', 'ativa há mais de 1 h')]


@dataclass(frozen=True)
class QuadroRaios:
    quando: datetime
    celulas: tuple[tuple[float, float, int], ...]    # (lat, lon, minutos ativa) — centro da célula

    @property
    def chave(self) -> str:
        return f'{self.quando:%Y%m%dT%H%M}'

    @property
    def limites(self) -> list[list[float]]:
        return [[RAIOS_SUL, RAIOS_OESTE], [RAIOS_NORTE, RAIOS_LESTE]]

    def url(self) -> str:
        return f'/redemet/raios?q={self.chave}'


_quadros_raios: dict[str, QuadroRaios] = {}      # chave -> quadro (para a rota desenhar a imagem)


def _horarios_raios(rotulos: list[str], ultima: datetime) -> list[datetime]:
    """'HH:MM' -> datetime. A data vem da última ocorrência; volta um dia se passar da meia-noite."""
    horarios = []
    for rotulo in rotulos:
        try:
            h, m = map(int, rotulo.split(':'))
        except ValueError:
            return []
        t = ultima.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > ultima + timedelta(minutes=1):
            t -= timedelta(days=1)
        horarios.append(t)
    return horarios


def converter_raios(dados: dict) -> list[QuadroRaios]:
    """Resposta do stsc (com anima=N) -> quadros em ordem, cada célula com há quanto tempo
    ela aparece sem interrupção (só dá para saber dentro dos quadros recebidos)."""
    info, rotulos, listas = dados.get('info') or {}, dados.get('anima') or [], dados.get('stsc') or []
    ultima = _data(info.get('ultima_ocorrencia'))
    if not ultima or len(rotulos) != len(listas):
        return []
    horarios = _horarios_raios(rotulos, ultima)
    if len(horarios) != len(listas):
        return []
    conjuntos = []
    for pontos in listas:
        celulas = set()
        for p in pontos:
            try:
                celulas.add((round(float(p['la']), 2), round(float(p['lo']), 2)))
            except (KeyError, TypeError, ValueError):
                continue
        conjuntos.append(celulas)
    quadros: list[QuadroRaios] = []
    desde: dict[tuple, datetime] = {}         # célula -> desde quando aparece sem parar
    for quando, celulas in zip(horarios, conjuntos):
        desde = {c: desde.get(c, quando) for c in celulas}     # some do quadro = recomeça a contar
        quadros.append(QuadroRaios(quando, tuple(
            (la, lo, round((quando - desde[(la, lo)]).total_seconds() / 60)) for la, lo in sorted(celulas))))
    return quadros


def classe_raio(minutos: int) -> int:
    for i, (limite, _, _) in enumerate(RAIOS_CLASSES):
        if limite is None or minutos < limite:
            return i
    return len(RAIOS_CLASSES) - 1


def desenhar_raios(quadro: QuadroRaios) -> bytes:
    """Imagem PNG (já em Mercator) com um quadrado colorido por célula; o resto transparente."""
    largura = round((RAIOS_LESTE - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
    px_por_unidade = largura / math.radians(RAIOS_LESTE - RAIOS_OESTE)
    y_norte = _mercator_y(RAIOS_NORTE)
    altura = round((y_norte - _mercator_y(RAIOS_SUL)) * px_por_unidade)
    img = np.zeros((altura, largura), np.uint8)              # 0 = transparente
    meia = CELULA_GRAUS / 2
    for la, lo, minutos in quadro.celulas:
        if not (RAIOS_SUL < la < RAIOS_NORTE and RAIOS_OESTE < lo < RAIOS_LESTE):
            continue
        x0 = round((lo - meia - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
        x1 = round((lo + meia - RAIOS_OESTE) / CELULA_GRAUS * RAIOS_PX_POR_CELULA)
        y0 = round((y_norte - _mercator_y(la + meia)) * px_por_unidade)
        y1 = round((y_norte - _mercator_y(la - meia)) * px_por_unidade)
        img[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)] = classe_raio(minutos) + 1
    paleta = [0, 0, 0] + [int(c[i:i + 2], 16) for _, c, _ in RAIOS_CLASSES for i in (1, 3, 5)]
    imagem = Image.fromarray(img, 'P')
    imagem.putpalette(paleta)
    saida = io.BytesIO()
    imagem.save(saida, 'PNG', transparency=0, optimize=True)
    return saida.getvalue()


async def raios_quadros() -> list[QuadroRaios]:
    quadros = converter_raios(await _consultar('stsc', RADAR_CACHE_S, anima=RAIOS_QUADROS_MAX))
    for quadro in quadros:
        _quadros_raios[quadro.chave] = quadro
    while len(_quadros_raios) > 200:                    # guarda as últimas ~10 h
        _quadros_raios.pop(min(_quadros_raios))         # chave AAAAMMDDTHHMM: a menor é a mais velha
    return quadros


def imagem_raios(chave: str) -> tuple[int, bytes, str]:
    """Para a rota /redemet/raios: a imagem de um quadro já consultado."""
    quadro = _quadros_raios.get(chave)
    if quadro is None:
        return 404, b'quadro desconhecido', 'text/plain'
    cache = ('raios', chave)
    if cache not in _cache_img:
        guardar_no_cache(_cache_img, cache, (desenhar_raios(quadro), 'image/png'))
    return 200, *_cache_img[cache]


async def buscar_imagem(caminho: str, sul: float, norte: float, dbz_min: int = 0) -> tuple[int, bytes, str]:
    """Imagem reprojetada (e filtrada, no radar) para o navegador. Devolve (status, corpo, tipo)."""
    if not caminho_valido(caminho) or not (-85 < sul < norte < 85) or not 0 <= dbz_min <= 75:
        return 400, b'pedido recusado', 'text/plain'
    chave = (caminho, sul, norte, dbz_min)
    if chave in _cache_img:
        return 200, *_cache_img[chave]
    png = await png_original(caminho)
    if png is None:
        return 502, b'REDEMET indisponivel', 'text/plain'
    try:
        # Decodificar, reprojetar e codificar usa CPU (~0,5 s numa imagem de satélite). Numa
        # thread, o laço do servidor continua atendendo os outros usuários enquanto isso.
        conteudo, tipo = await asyncio.to_thread(_preparar, png, caminho, sul, norte, dbz_min)
    except OSError as erro:              # PNG corrompido
        log.warning('imagem da REDEMET ilegível: %s (%s)', caminho, erro)
        return 502, b'imagem da REDEMET ilegivel', 'text/plain'
    guardar_no_cache(_cache_img, chave, (conteudo, tipo))
    return 200, conteudo, tipo


def _preparar(png: bytes, caminho: str, sul: float, norte: float, dbz_min: int) -> tuple[bytes, str]:
    """PNG original (grade lat/lon) -> imagem para o navegador, em Mercator. Roda numa thread."""
    img = reprojetar(decodificar(png), sul, norte)
    saida = io.BytesIO()
    if caminho.startswith('satelite/'):
        # Satélite é foto, sem transparência: WebP fica ~3x menor que PNG (450 KB x 1,25 MB)
        Image.fromarray(img, 'RGBA').convert('RGB').save(saida, 'WEBP', quality=85)
        return saida.getvalue(), 'image/webp'
    # Radar: PNG sem perdas, cores exatas (o filtro de dBZ e a sonda dependem delas).
    # O filtro age na matriz, antes de codificar: um PNG só, em vez de codificar, decodificar
    # e codificar de novo.
    if dbz_min:
        img = radar.filtrar_rgba(img, dbz_min)
    Image.fromarray(img, 'RGBA').save(saida, 'PNG', compress_level=1)
    return saida.getvalue(), 'image/png'


def decodificar(png: bytes) -> np.ndarray:
    """PNG -> matriz altura × largura × 4 (RGBA, uint8). OSError se o arquivo for inválido."""
    return np.array(Image.open(io.BytesIO(png)).convert('RGBA'))


async def png_original(caminho: str) -> bytes | None:
    """O PNG como está no servidor estático da REDEMET (None se não deu para baixar).

    Os de radar ficam em `_pngs` (pequenos, e a sonda e o impacto voltam a eles); os de
    satélite não (1,4 MB cada, e só servem para gerar a imagem pronta, que já fica em cache).
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
        guardar_no_cache(_pngs, caminho, resposta.content)
    return resposta.content


async def imagem_bruta(imagem: Imagem) -> np.ndarray | None:
    """Matriz RGBA da imagem ORIGINAL (grade lat/lon, sem reprojetar): para ler o dBZ de um ponto."""
    png = await png_original(imagem.caminho)
    if png is None:
        return None
    try:
        return decodificar(png)
    except OSError:
        return None


async def preparar(imagens: list[tuple[Imagem, int]], simultaneas: int = 8) -> None:
    """Baixa e prepara de antemão (cache) as imagens de uma animação: quando o navegador
    pedir, elas saem na hora. `imagens`: (Imagem, dbz_min)."""
    limite = asyncio.Semaphore(simultaneas)

    async def uma(imagem: Imagem, dbz: int) -> None:
        async with limite:
            await buscar_imagem(imagem.caminho, imagem.sul, imagem.norte, dbz)

    await asyncio.gather(*(uma(i, d) for i, d in imagens))
