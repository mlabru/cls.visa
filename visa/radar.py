"""
Radar meteorológico: proxy do WMS do GeoServer e filtro de refletividade (dBZ).

O navegador nunca fala direto com o GeoServer: ele pede os tiles a /radar/wms
(neste servidor), que repassa o pedido, e — se houver limiar de dBZ — apaga os
pixels abaixo do limiar antes de devolver a imagem.

Como o GeoServer devolve imagens já coloridas, o dBZ de cada pixel é deduzido
pela cor, comparando com a paleta da REDEMET (a mesma de colore_radar.py).
"""
import asyncio
import io
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
from PIL import Image

import config


@dataclass(frozen=True)
class Radar:
    codigo: str     # código usado no CQL_FILTER (radar_code)
    sigla: str
    nome: str
    uf: str
    lat: float
    lon: float


RADARES = [
    Radar('al', 'ALM', 'Almenara', 'MG', -16.1834, -40.6946),
    Radar('be', 'BEL', 'Belém', 'PA', -1.406667, -48.461389),
    Radar('bv', 'BVI', 'Boa Vista', 'RR', 2.844167, -60.700278),
    Radar('cn', 'CGU', 'Canguçu', 'RS', -31.404, -52.701644),
    Radar('cz', 'CZS', 'Cruzeiro do Sul', 'AC', -7.595833, -72.767778),
    Radar('ga', 'GAM', 'Gama', 'DF', -15.97643, -48.016142),
    Radar('jr', 'JRG', 'Jaraguari', 'MS', -20.27855, -54.47396),
    Radar('mo', 'MCO', 'Maceió', 'AL', -9.55129, -35.77068),
    Radar('mq', 'MCP', 'Macapá', 'AP', -0.047222, -51.097778),
    Radar('mi', 'MDI', 'Morro da Igreja', 'SC', -28.128373, -49.471816),
    Radar('mn', 'MNS', 'Manaus', 'AM', -3.149216, -59.991881),
    Radar('nt', 'NTL', 'Natal', 'RN', -5.90448, -35.25401),
    Radar('pc', 'PCO', 'Pico do Couto', 'RJ', -22.464278, -43.297476),
    Radar('pl', 'PTL', 'Petrolina', 'PE', -9.367, -40.573),
    Radar('pv', 'PVL', 'Porto Velho', 'RO', -8.715, -63.893889),
    Radar('rb', 'RBR', 'Rio Branco', 'AC', -9.86163, -67.8961),
    Radar('sf', 'SFR', 'São Francisco', 'MG', -16.00889, -44.69588),
    Radar('ua', 'SGC', 'São Gabriel da Cachoeira', 'AM', -0.143611, -67.056944),
    Radar('sl', 'SLU', 'São Luís', 'MA', -2.597222, -44.2375),
    Radar('sr', 'SRO', 'São Roque', 'SP', -23.601915, -47.094063),
    Radar('st', 'STA', 'Santa Teresa', 'ES', -19.98887, -40.5794),
    Radar('sg', 'STI', 'Santiago', 'RS', -29.225213, -54.930257),
    Radar('sn', 'STM', 'Santarém', 'PA', -2.429722, -54.798889),
    Radar('sv', 'SVD', 'Salvador', 'BA', -12.9025, -38.32666),
    Radar('tt', 'TBT', 'Tabatinga', 'AM', -4.248333, -69.935),
    Radar('tf', 'TEF', 'Tefé', 'AM', -3.377222, -64.720556),
    Radar('tm', 'TRM', 'Três Marias', 'MG', -18.207259, -45.460535),
    Radar('vh', 'VLH', 'Vilhena', 'RO', -12.741, -60.145),
]
POR_CODIGO = {r.codigo: r for r in RADARES}

PRODUTOS = {'maxcappi': 'MAXCAPPI', 'cappi': 'CAPPI'}
ALCANCES = {'400km': '400 km', '250km': '250 km'}
ALTURAS_CAPPI = {'3': '3 km', '5': '5 km', '7': '7 km', '10': '10 km'}


# ---------------------------------------------------------------------------
# Paleta e classificação de cores
# ---------------------------------------------------------------------------

# (dBZ, R, G, B). A primeira entrada é o cinza do CEMADEN (abaixo do limiar mínimo).
PALETA = np.array([
    (-5, 128, 128, 128),
    (0, 117, 255, 255), (5, 0, 238, 240), (10, 1, 161, 247), (15, 0, 0, 246),
    (20, 0, 255, 1), (25, 0, 203, 0), (30, 0, 145, 0), (35, 255, 255, 0),
    (40, 231, 194, 0), (45, 255, 144, 0), (50, 254, 0, 0), (55, 213, 1, 0),
    (60, 196, 0, 1), (65, 255, 0, 254), (70, 151, 88, 201), (75, 255, 255, 255),
], dtype=float)

FUNDO = -1  # "não é eco de radar" (borda, texto, mapa): nunca é apagado

# Legenda do painel: (dBZ, cor) de 0 a 75 — as mesmas cores que o GeoServer usa nas imagens
LEGENDA = [(int(dbz), f'#{int(r):02x}{int(g):02x}{int(b):02x}') for dbz, r, g, b in PALETA[1:]]


def _tabela_interpolada() -> np.ndarray:
    """Cores intermediárias geradas por colore_radar.py: cinza 0–255 -> (dBZ, R, G, B).

    Mesmo cálculo de buildDbzLookupTable() no visualizador.html (já corrigido).
    """
    escala = (95.5 - (-31.0)) / (254.0 - 2.0)
    linhas = []
    for cinza in range(256):
        dbz = escala * cinza - 32
        if not 0 <= dbz < 75:
            continue
        # PALETA[0] é o -5 dBZ extra; o intervalo [5k, 5k+5) usa os índices k+1 e k+2
        n0 = max(1, min(len(PALETA) - 2, math.floor(dbz / 5) + 1))
        p0, p1 = PALETA[n0], PALETA[n0 + 1]
        f = (dbz - p0[0]) / (p1[0] - p0[0])
        cor = np.round(p0[1:] + f * (p1[1:] - p0[1:]))
        linhas.append((dbz, *cor))
    return np.array(linhas)


TABELA = _tabela_interpolada()


def classificar_cores(cores: np.ndarray) -> np.ndarray:
    """Recebe N cores RGB (N×3) e devolve o dBZ estimado de cada uma (ou FUNDO).

    1. Se a cor bate (tolerância de ~4 níveis por canal) com uma cor interpolada da
       tabela, usa aquele dBZ.
    2. Senão, pega a cor mais próxima da paleta e aplica as regras que separam
       fundo de mapa (tons de cinza) de eco de radar — igual ao visualizador.html.
    """
    cores = cores.astype(int)
    resultado = np.empty(len(cores))

    # 1) Comparação quantizada (>>2) com a tabela, tolerância de ±1 degrau
    q_cor = cores[:, None, :] >> 2                       # N×1×3
    q_tab = TABELA[None, :, 1:].astype(int) >> 2         # 1×T×3
    bate = np.all(np.abs(q_cor - q_tab) <= 1, axis=2)    # N×T
    tem_par = bate.any(axis=1)
    primeiro = bate.argmax(axis=1)                       # 1ª entrada que bate
    resultado[tem_par] = TABELA[primeiro[tem_par], 0]

    # 2) Cor mais próxima da paleta + regras de "é fundo?"
    resto = ~tem_par
    c = cores[resto]
    dist = ((c[:, None, :] - PALETA[None, :, 1:]) ** 2).sum(axis=2)
    mais_perto = dist.argmin(axis=1)
    dist_min = dist.min(axis=1)
    dbz = PALETA[mais_perto, 0].copy()

    maior, menor = c.max(axis=1), c.min(axis=1)
    cinza = (maior - menor) < 20
    longe_demais = dist_min > 16000
    cinza_suspeito = cinza & np.isin(dbz, (0, 70, 75))   # bordas/texto parecidos com branco/roxo/ciano
    cinza_comum = cinza & (maior < 250) & (dbz != -5)    # outros cinzas (o -5 do CEMADEN fica)
    dbz[longe_demais | cinza_suspeito | cinza_comum] = FUNDO
    resultado[resto] = dbz
    return resultado


def dbz_por_pixel(img: np.ndarray) -> np.ndarray:
    """Imagem RGBA (altura × largura × 4) -> dBZ de cada pixel (float).

    NaN = pixel transparente; FUNDO = pixel visível que não é eco (borda, texto, mapa).
    Usado pelo filtro de limiar (abaixo) e pelo painel de impacto (impacto.py).

    Uma imagem tem poucas cores distintas (dezenas), mas muitos pixels (centenas de mil):
    `np.unique` classifica cada cor uma vez só e espalha o resultado pelos pixels.
    """
    altura, largura = img.shape[:2]
    rgb, alfa = img[..., :3].reshape(-1, 3), img[..., 3].reshape(-1)
    saida = np.full(altura * largura, np.nan)
    visiveis = alfa > 0
    if visiveis.any():
        cores, indice = np.unique(rgb[visiveis], axis=0, return_inverse=True)
        saida[visiveis] = classificar_cores(cores)[indice.ravel()]
    return saida.reshape(altura, largura)


def filtrar_rgba(img: np.ndarray, limiar_dbz: int) -> np.ndarray:
    """Cópia da imagem com os pixels de eco abaixo do limiar transparentes (o fundo fica)."""
    dbz = dbz_por_pixel(img)
    # Arredonda: a cor exata de 30 dBZ sai da tabela como 29,8 e não deve sumir com limiar 30.
    # (Comparações com NaN dão False: pixels já transparentes não entram.)
    apagar = (dbz != FUNDO) & (np.round(dbz) < limiar_dbz)
    saida = img.copy()
    saida[apagar, 3] = 0
    return saida


def filtrar_png(png: bytes, limiar_dbz: int) -> bytes:
    """O mesmo filtro, de PNG para PNG (tiles do GeoServer, que já chegam codificados)."""
    img = filtrar_rgba(np.array(Image.open(io.BytesIO(png)).convert('RGBA')), limiar_dbz)
    saida = io.BytesIO()
    Image.fromarray(img).save(saida, 'PNG', compress_level=1)
    return saida.getvalue()


def cor_dbz(dbz: float | None) -> str:
    """Cor da legenda para um valor de dBZ (a faixa de 5 em 5 que o contém); None = sem eco."""
    if dbz is None:
        return '#334155'
    cor = LEGENDA[0][1]
    for limite, c in LEGENDA:
        if dbz >= limite:
            cor = c
    return cor


# ---------------------------------------------------------------------------
# Proxy do WMS
# ---------------------------------------------------------------------------

# Se o GeoServer não responde, cada tile prenderia uma conexão do navegador por 5 s e
# travaria os outros pedidos (o navegador só abre 6 conexões por servidor). Depois de uma
# falha de conexão, respondemos "indisponível" na hora durante RADAR_ESPERA_FALHA_S.
RADAR_ESPERA_FALHA_S = 30


@dataclass
class EstadoGeoServer:
    ok: bool | None = None          # o último pedido deu certo? None = ainda não testado (barra de saúde)
    indisponivel_ate: float = 0.0   # disjuntor: até quando (time.monotonic) responder "fora" sem tentar


estado = EstadoGeoServer()
_cliente = httpx.AsyncClient(timeout=httpx.Timeout(config.RADAR_TIMEOUT_S, connect=5))


async def alcancavel(url: str = '', espera_s: float = 5) -> bool:
    """Testa só a conexão TCP com o GeoServer (sem pedir imagem nenhuma)."""
    endereco = httpx.URL(url or config.GEOSERVER_WMS)
    porta = endereco.port or (443 if endereco.scheme == 'https' else 80)
    try:
        _, escrita = await asyncio.wait_for(asyncio.open_connection(endereco.host, porta), espera_s)
    except (TimeoutError, OSError):
        return False
    escrita.close()
    return True


async def vigiar() -> None:
    """Tarefa de fundo: verifica o GeoServer a cada RADAR_ESPERA_FALHA_S.

    Assim o "indisponível" já está valendo quando a página abre, e nenhum tile fica
    5 s preso esperando a conexão (o que atrasava as camadas de espaço aéreo).
    """
    while True:
        if await alcancavel():
            estado.indisponivel_ate = 0.0
            if estado.ok is None:
                estado.ok = True
        else:
            estado.indisponivel_ate = math.inf
            estado.ok = False
        await asyncio.sleep(RADAR_ESPERA_FALHA_S)


# O proxy só repassa o que o mapa usa (o GeoServer é interno): imagens (GetMap) e
# legendas (GetLegendGraphic) das camadas abaixo
CAMADAS_PERMITIDAS = re.compile(
    r'redemet:((max)?cappi_\w+|lightning_data|tempo_severo|temperatura|turbulencia|umidade|gelo_severo|cb_extensao)')
PEDIDOS = {'getmap': 'layers', 'getlegendgraphic': 'layer'}   # pedido -> parâmetro com a camada


def pedido_permitido(parametros: dict[str, str]) -> bool:
    p = {k.lower(): v for k, v in parametros.items()}
    chave = PEDIDOS.get(p.get('request', '').lower())
    if chave is None:
        return False
    camadas = p.get(chave, '').split(',')
    return all(CAMADAS_PERMITIDAS.fullmatch(c) for c in camadas)


def geoserver_fora() -> bool:
    """True enquanto o GeoServer está marcado como inacessível (disjuntor ou verificação de fundo)."""
    return time.monotonic() < estado.indisponivel_ate


async def buscar_tile(parametros: dict[str, str]) -> tuple[int, bytes, str]:
    """Repassa um GetMap ao GeoServer. `dbz_min` (nosso) é retirado e aplicado aqui.

    Devolve (status HTTP, conteúdo, tipo).
    """
    if not pedido_permitido(parametros):
        return 400, b'camada ou pedido nao permitido', 'text/plain'
    if geoserver_fora():
        return 503, b'GeoServer inacessivel (aguardando nova tentativa)', 'text/plain'

    parametros = dict(parametros)
    limiar = int(float(parametros.pop('dbz_min', 0) or 0))
    try:
        resposta = await _cliente.get(config.GEOSERVER_WMS, params=parametros)
    except httpx.HTTPError as erro:
        if isinstance(erro, (httpx.ConnectError, httpx.ConnectTimeout)):
            estado.indisponivel_ate = time.monotonic() + RADAR_ESPERA_FALHA_S
        estado.ok = False
        return 502, b'GeoServer inacessivel', 'text/plain'

    tipo = resposta.headers.get('content-type', '')
    if resposta.status_code != 200 or not tipo.startswith('image/'):
        # O GeoServer responde erros de WMS como XML com status 200
        estado.ok = False
        return 502, resposta.content[:2000], 'text/plain'

    estado.ok = True
    if limiar <= 0:
        return 200, resposta.content, tipo
    # o filtro usa CPU: roda numa thread para não travar os outros usuários
    return 200, await asyncio.to_thread(filtrar_png, resposta.content, limiar), 'image/png'


# ---------------------------------------------------------------------------
# Parâmetros do pedido WMS
# ---------------------------------------------------------------------------

def janela_de_tempo(minutos: int = config.RADAR_JANELA_MIN) -> str:
    """Intervalo TIME do WMS: 'início/agora' em UTC, sem milissegundos."""
    agora = datetime.now(UTC).replace(microsecond=0)
    inicio = agora - timedelta(minutes=minutos)
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    return f'{inicio.strftime(fmt)}/{agora.strftime(fmt)}'


def filtro_cql(codigos: list[str]) -> str:
    """CQL_FILTER para os radares escolhidos. 'INCLUDE' = todos."""
    validos = {r.codigo for r in RADARES}
    escolhidos = [c for c in codigos if c in validos]  # só códigos conhecidos (evita injeção)
    if not escolhidos or len(escolhidos) == len(RADARES):
        return 'INCLUDE'
    return "radar_code IN ('" + "','".join(escolhidos) + "')"


# ---------------------------------------------------------------------------
# Status dos radares: horário da imagem mais recente de cada um (WFS radar_status_latest)
# ---------------------------------------------------------------------------

# status -> (cor, rótulo), com os limites do original: 30 min, 1 h e 3 h
STATUS = {
    'green': ('#10b981', 'Recente (< 30 min)'),
    'yellow': ('#fbbf24', 'Atrasado (< 1 h)'),
    'red': ('#ef4444', 'Crítico (< 3 h)'),
    'gray': ('#64748b', 'Sem dados'),
}
STATUS_FILTRO_PADRAO = {'green': True, 'yellow': True, 'red': True, 'gray': False}


def classificar_status(ultima: datetime | None, agora: datetime | None = None) -> str:
    if ultima is None:
        return 'gray'
    minutos = ((agora or datetime.now(UTC)) - ultima).total_seconds() / 60
    if minutos <= config.RADAR_VERDE_ATE_MIN:
        return 'green'
    if minutos <= config.RADAR_AMARELO_ATE_MIN:
        return 'yellow'
    if minutos <= config.RADAR_VERMELHO_ATE_MIN:
        return 'red'
    return 'gray'


def filtro_status(produto: str, alcance: str, altura: str) -> str:
    """CQL do WFS de status. Só valores conhecidos (vão para o GeoServer)."""
    if produto not in PRODUTOS or alcance not in ALCANCES or altura not in ALTURAS_CAPPI:
        raise ValueError('produto, alcance ou altura desconhecido')
    cql = f"produto='{produto}_{alcance}'"
    return cql + f' AND elevacao={altura}' if produto == 'cappi' else cql


def _data_utc(texto: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(texto))     # desde o Python 3.11 aceita o sufixo 'Z'
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


async def buscar_status(produto: str, alcance: str, altura: str) -> dict[str, datetime] | None:
    """{código do radar: horário da última imagem}. None se o GeoServer não respondeu
    (diferente de {} = respondeu, mas nenhum radar tem dados)."""
    if geoserver_fora():
        return None
    parametros = {'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
                  'typeName': 'redemet:radar_status_latest', 'outputFormat': 'application/json',
                  'cql_filter': filtro_status(produto, alcance, altura)}
    try:
        resposta = await _cliente.get(config.GEOSERVER_WFS, params=parametros)
        resposta.raise_for_status()
        feicoes = resposta.json().get('features') or []
    except (httpx.HTTPError, ValueError):
        return None
    codigos = {r.codigo.upper(): r.codigo for r in RADARES}
    saida = {}
    for f in feicoes:
        p = f.get('properties') or {}
        codigo = codigos.get(str(p.get('radar_code', '')).upper())
        quando = _data_utc(p.get('last_update', ''))
        if codigo and quando:
            saida[codigo] = quando
    return saida
