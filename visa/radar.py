"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Radar meteorológico: proxy do WMS do GeoServer e filtro de refletividade (dBZ).

O navegador nunca fala direto com o GeoServer: ele pede os tiles a /radar/wms
(neste servidor), que repassa o pedido, e — se houver limiar de dBZ — apaga os
pixels abaixo do limiar antes de devolver a imagem.

Como o GeoServer devolve imagens já coloridas, o dBZ de cada pixel é deduzido
pela cor, comparando com a paleta da REDEMET (a mesma de colore_radar.py).

Quem usa: as rotas /radar/wms e o status dos radares (main.py e camadas), o `redemet`
(filtro de dBZ nas imagens da API), o painel de impacto e a sonda (dBZ de um pixel).
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
import datas


@dataclass(frozen=True)
class Radar:
    """Uma estação de radar meteorológico da REDEMET.

    Atributos:
        codigo: código de duas letras usado no CQL_FILTER (campo `radar_code`).
        sigla: sigla de três letras mostrada na tela.
        nome: nome da localidade.
        uf: estado.
        lat: latitude da antena, em graus.
        lon: longitude da antena, em graus.
    """
    codigo: str
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
POR_CODIGO = {radar.codigo: radar for radar in RADARES}

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
    """Monta a tabela das cores intermediárias geradas por colore_radar.py.

    A REDEMET converte um tom de cinza (0–255) em dBZ e interpola a cor entre as duas
    entradas vizinhas da PALETA. Refazemos a mesma conta para reconhecer essas cores.
    Mesmo cálculo de buildDbzLookupTable() no visualizador.html (já corrigido).

    Retorna:
        Matriz T×4 com (dBZ, R, G, B) de cada cinza cujo dBZ fica em [0, 75).
    """
    escala = (95.5 - (-31.0)) / (254.0 - 2.0)
    linhas = []
    for cinza in range(256):
        dbz = escala * cinza - 32
        if not 0 <= dbz < 75:
            continue
        # PALETA[0] é o -5 dBZ extra; o intervalo [5k, 5k+5) usa os índices k+1 e k+2
        indice_inicio = max(1, min(len(PALETA) - 2, math.floor(dbz / 5) + 1))
        inicio = PALETA[indice_inicio]
        fim = PALETA[indice_inicio + 1]
        fracao = (dbz - inicio[0]) / (fim[0] - inicio[0])
        cor = np.round(inicio[1:] + fracao * (fim[1:] - inicio[1:]))
        linhas.append((dbz, *cor))
    return np.array(linhas)


TABELA = _tabela_interpolada()


def classificar_cores(cores: np.ndarray) -> np.ndarray:
    """Estima o dBZ de cada cor RGB (ou FUNDO, se a cor não for eco de radar).

    Fluxo:
        1. Se a cor bate (tolerância de ~4 níveis por canal) com uma cor interpolada da
           TABELA, usa aquele dBZ (`_procurar_na_tabela`).
        2. Senão, pega a cor mais próxima da PALETA e aplica as regras que separam
           fundo de mapa (tons de cinza) de eco de radar — igual ao visualizador.html
           (`_dbz_pela_paleta`).

    Parâmetros:
        cores: matriz N×3 com as cores (R, G, B), de 0 a 255.

    Retorna:
        Vetor de N valores (float): o dBZ de cada cor, ou FUNDO.
    """
    cores = cores.astype(int)
    resultado = np.empty(len(cores))
    achou_na_tabela, dbz_da_tabela = _procurar_na_tabela(cores)
    resultado[achou_na_tabela] = dbz_da_tabela
    sem_par = ~achou_na_tabela
    resultado[sem_par] = _dbz_pela_paleta(cores[sem_par])
    return resultado


def _procurar_na_tabela(cores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Procura cada cor na TABELA interpolada (passo 1 de `classificar_cores`).

    A comparação é quantizada (>>2, ou seja, de 4 em 4 níveis) com tolerância de ±1 degrau.
    Se várias entradas batem, vale a primeira.

    Parâmetros:
        cores: matriz N×3 de inteiros (R, G, B).

    Retorna:
        (achou, dbz): `achou` é um vetor booleano de N posições; `dbz` traz o dBZ só das
        cores com `achou` verdadeiro, na mesma ordem.
    """
    cores_quantizadas = cores[:, None, :] >> 2                       # N×1×3
    tabela_quantizada = TABELA[None, :, 1:].astype(int) >> 2         # 1×T×3
    bate = np.all(np.abs(cores_quantizadas - tabela_quantizada) <= 1, axis=2)    # N×T
    achou = bate.any(axis=1)
    primeira_que_bate = bate.argmax(axis=1)
    return achou, TABELA[primeira_que_bate[achou], 0]


def _dbz_pela_paleta(cores: np.ndarray) -> np.ndarray:
    """Dá o dBZ da cor mais próxima da PALETA, ou FUNDO (passo 2 de `classificar_cores`).

    É FUNDO a cor longe demais de qualquer cor da paleta, ou um tom de cinza (canais quase
    iguais) que não seja o cinza -5 dBZ do CEMADEN.

    Parâmetros:
        cores: matriz N×3 de inteiros (R, G, B) que não estão na TABELA.

    Retorna:
        Vetor de N valores (float): dBZ da paleta ou FUNDO.
    """
    distancias = ((cores[:, None, :] - PALETA[None, :, 1:]) ** 2).sum(axis=2)    # N×16
    mais_perto = distancias.argmin(axis=1)
    distancia_minima = distancias.min(axis=1)
    dbz = PALETA[mais_perto, 0].copy()

    canal_maior = cores.max(axis=1)
    canal_menor = cores.min(axis=1)
    cinza = (canal_maior - canal_menor) < 20
    longe_demais = distancia_minima > 16000
    cinza_suspeito = cinza & np.isin(dbz, (0, 70, 75))           # bordas/texto parecidos com branco/roxo/ciano
    cinza_comum = cinza & (canal_maior < 250) & (dbz != -5)      # outros cinzas (o -5 do CEMADEN fica)
    dbz[longe_demais | cinza_suspeito | cinza_comum] = FUNDO
    return dbz


def dbz_por_pixel(img: np.ndarray) -> np.ndarray:
    """Calcula o dBZ de cada pixel de uma imagem de radar.

    Usado pelo filtro de limiar (abaixo) e pelo painel de impacto (impacto.py).
    Uma imagem tem poucas cores distintas (dezenas), mas muitos pixels (centenas de mil):
    `np.unique` classifica cada cor uma vez só e espalha o resultado pelos pixels.

    Parâmetros:
        img: matriz RGBA (altura × largura × 4), uint8.

    Retorna:
        Matriz altura × largura (float): o dBZ de cada pixel. NaN = pixel transparente;
        FUNDO = pixel visível que não é eco (borda, texto, mapa).
    """
    altura, largura = img.shape[:2]
    rgb = img[..., :3].reshape(-1, 3)
    alfa = img[..., 3].reshape(-1)
    saida = np.full(altura * largura, np.nan)
    visiveis = alfa > 0
    if visiveis.any():
        cores, indice_da_cor = np.unique(rgb[visiveis], axis=0, return_inverse=True)
        saida[visiveis] = classificar_cores(cores)[indice_da_cor.ravel()]
    return saida.reshape(altura, largura)


def filtrar_rgba(img: np.ndarray, limiar_dbz: int) -> np.ndarray:
    """Torna transparentes os pixels de eco abaixo do limiar (o fundo fica).

    Parâmetros:
        img: matriz RGBA (altura × largura × 4), uint8. Não é alterada.
        limiar_dbz: menor dBZ que continua visível.

    Retorna:
        Uma cópia da imagem, já filtrada.
    """
    dbz = dbz_por_pixel(img)
    # Arredonda: a cor exata de 30 dBZ sai da tabela como 29,8 e não deve sumir com limiar 30.
    # (Comparações com NaN dão False: pixels já transparentes não entram.)
    apagar = (dbz != FUNDO) & (np.round(dbz) < limiar_dbz)
    saida = img.copy()
    saida[apagar, 3] = 0
    return saida


def filtrar_png(png: bytes, limiar_dbz: int) -> bytes:
    """Aplica o mesmo filtro de `filtrar_rgba`, de PNG para PNG.

    Usado nos tiles do GeoServer, que já chegam codificados. O PNG de saída é sem perdas
    (as cores precisam chegar exatas); `compress_level=1` comprime várias vezes mais rápido.

    Parâmetros:
        png: conteúdo do arquivo PNG.
        limiar_dbz: menor dBZ que continua visível.

    Retorna:
        O PNG filtrado.
    """
    img = filtrar_rgba(np.array(Image.open(io.BytesIO(png)).convert('RGBA')), limiar_dbz)
    saida = io.BytesIO()
    Image.fromarray(img).save(saida, 'PNG', compress_level=1)
    return saida.getvalue()


def cor_dbz(dbz: float | None) -> str:
    """Dá a cor da legenda para um valor de dBZ (a faixa de 5 em 5 que o contém).

    Parâmetros:
        dbz: refletividade, em dBZ; None = sem eco.

    Retorna:
        Cor em '#rrggbb'. Abaixo de 0 dBZ, a cor da primeira faixa.
    """
    if dbz is None:
        return '#334155'
    cor = LEGENDA[0][1]
    for limite, cor_da_faixa in LEGENDA:
        if dbz >= limite:
            cor = cor_da_faixa
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
    """Saúde do GeoServer, compartilhada por todos os usuários (ver DECISOES 2.3).

    Atributos:
        ok: o último pedido deu certo? None = ainda não testado (barra de saúde).
        indisponivel_ate: disjuntor: até quando (`time.monotonic`) responder "fora" sem
            tentar. `math.inf` = até a verificação de fundo achar o servidor de novo.
    """
    ok: bool | None = None
    indisponivel_ate: float = 0.0


estado = EstadoGeoServer()
_cliente = httpx.AsyncClient(timeout=httpx.Timeout(config.RADAR_TIMEOUT_S, connect=5))


async def alcancavel(url: str = '', espera_s: float = 5) -> bool:
    """Testa só a conexão TCP com o GeoServer (sem pedir imagem nenhuma).

    Parâmetros:
        url: endereço a testar; vazio = `config.GEOSERVER_WMS`.
        espera_s: tempo máximo de espera pela conexão, em segundos.

    Retorna:
        True se a conexão abriu.
    """
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
    Fora do ar, o disjuntor fica ligado (`math.inf`) até a próxima verificação dar certo.
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
    """Diz se o pedido é um GetMap/GetLegendGraphic só de camadas permitidas.

    Parâmetros:
        parametros: parâmetros da URL, com os nomes em qualquer caixa.

    Retorna:
        True se o tipo de pedido é conhecido e todas as camadas estão em CAMADAS_PERMITIDAS.
    """
    em_minusculas = {nome.lower(): valor for nome, valor in parametros.items()}
    parametro_da_camada = PEDIDOS.get(em_minusculas.get('request', '').lower())
    if parametro_da_camada is None:
        return False
    camadas = em_minusculas.get(parametro_da_camada, '').split(',')
    return all(CAMADAS_PERMITIDAS.fullmatch(camada) for camada in camadas)


# Parâmetros que o mapa manda (Leaflet + as nossas opções em observacao.py, raios.py, wifs.py e
# nas legendas) e o formato aceito para cada um. Qualquer outro parâmetro é descartado (SLD_BODY,
# SLD, FORMAT_OPTIONS... nunca chegam ao GeoServer); um valor fora do formato recusa o pedido.
# O CQL_FILTER não vem daqui: é conferido à parte (`_cql_permitido`).
TAMANHO_MAX_PX = 1024           # tiles do Leaflet: 256 (512 em tela de alta densidade)
_NUMERO = r'-?\d+(\.\d+)?([eE][-+]?\d+)?'
_INSTANTE = r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z'
PARAMETROS_WMS = {
    'service': re.compile(r'WMS', re.IGNORECASE),
    'request': re.compile(r'GetMap|GetLegendGraphic', re.IGNORECASE),
    'version': re.compile(r'1\.1\.1|1\.3\.0'),
    'layers': re.compile(r'[\w:,]+'),             # o nome da camada é conferido em pedido_permitido
    'layer': re.compile(r'[\w:]+'),
    'styles': re.compile(r'((redemet:)?\w+)?'),
    'style': re.compile(r'((redemet:)?\w+)?'),
    'format': re.compile(r'image/png'),
    'transparent': re.compile(r'true|false', re.IGNORECASE),
    'width': re.compile(r'\d{1,4}'),
    'height': re.compile(r'\d{1,4}'),
    'srs': re.compile(r'EPSG:\d{4,6}'),
    'crs': re.compile(r'EPSG:\d{4,6}'),
    'bbox': re.compile(rf'{_NUMERO}(,{_NUMERO}){{3}}'),
    'time': re.compile(rf'current|{_INSTANTE}(/{_INSTANTE})?'),
    'elevation': re.compile(r'\d{1,5}'),
    'env': re.compile(r'radius:\d{1,7}(;t\d{1,2}:\d{8}T\d{6}Z){0,5}'),        # raios.parametros
    'legend_options': re.compile(r'[\w:;.]{1,200}'),                         # legendas (wifs, raios)
    'dbz_min': re.compile(r'\d{1,2}'),
}


def _cql_permitido(cql: str) -> bool:
    """Aceita só o que `filtro_cql` produziria: 'INCLUDE' ou a lista de radares conhecidos.

    Parâmetros:
        cql: valor do CQL_FILTER que veio na URL.

    Retorna:
        True se `filtro_cql` geraria exatamente esse texto.
    """
    if cql == 'INCLUDE':
        return True
    codigos = re.fullmatch(r"radar_code IN \('(\w+(?:','\w+)*)'\)", cql)
    return bool(codigos) and filtro_cql(codigos.group(1).split("','")) == cql


def _valor_aceito(chave: str, valor: str) -> bool:
    """Confere o valor de um parâmetro que o mapa usa (CQL_FILTER ou um de PARAMETROS_WMS).

    Parâmetros:
        chave: nome do parâmetro, em minúsculas.
        valor: valor que veio na URL.

    Retorna:
        True se o valor tem o formato esperado e, nos números, está dentro do limite.
    """
    if chave == 'cql_filter':
        return _cql_permitido(valor)
    if not PARAMETROS_WMS[chave].fullmatch(valor):
        return False
    if chave in ('width', 'height'):
        return 1 <= int(valor) <= TAMANHO_MAX_PX
    if chave == 'dbz_min':
        return int(valor) <= 75
    return True


def limpar_parametros(parametros: dict[str, str]) -> dict[str, str] | None:
    """Separa os parâmetros que podem seguir para o GeoServer (DECISOES 1.4).

    O navegador monta a URL do tile, então nada do que vem nela é confiável: cada valor tem
    de ter o formato que o nosso mapa usa, e o tamanho da imagem tem limite (sem ele, um
    WIDTH=8000&HEIGHT=8000 faria o GeoServer e o filtro de dBZ gerarem centenas de MB).

    Fluxo:
        1. Pedido ou camada fora da lista: recusa.
        2. Parâmetro que o mapa não usa: descartado em silêncio.
        3. Parâmetro conhecido com valor fora do formato: recusa o pedido inteiro.

    Parâmetros:
        parametros: parâmetros da URL, como vieram do navegador.

    Retorna:
        Os parâmetros aceitos (com os nomes como vieram), ou None se o pedido deve ser recusado.
    """
    if not pedido_permitido(parametros):
        return None
    limpos = {}
    for nome, valor in parametros.items():
        chave = nome.lower()
        if chave != 'cql_filter' and chave not in PARAMETROS_WMS:
            continue                    # parâmetro que o mapa não usa: descartado
        if not _valor_aceito(chave, valor):
            return None
        limpos[nome] = valor
    return limpos


def geoserver_fora() -> bool:
    """True enquanto o GeoServer está marcado como inacessível (disjuntor ou verificação de fundo)."""
    return time.monotonic() < estado.indisponivel_ate


async def buscar_tile(parametros: dict[str, str]) -> tuple[int, bytes, str]:
    """Repassa um GetMap/GetLegendGraphic ao GeoServer (rota /radar/wms).

    Fluxo:
        1. Só segue o que `limpar_parametros` aceita (senão, 400).
        2. Com o disjuntor ligado, responde 503 na hora, sem tentar.
        3. Retira `dbz_min` (parâmetro nosso, o GeoServer não o conhece) e pede a imagem.
        4. Com limiar, filtra a imagem numa thread.

    Parâmetros:
        parametros: parâmetros da URL, como vieram do navegador.

    Retorna:
        (status HTTP, conteúdo, tipo do conteúdo).
    """
    parametros = limpar_parametros(parametros)
    if parametros is None:
        return 400, b'camada ou pedido nao permitido', 'text/plain'
    if geoserver_fora():
        return 503, b'GeoServer inacessivel (aguardando nova tentativa)', 'text/plain'

    limiar = int(parametros.pop('dbz_min', 0) or 0)
    status, conteudo, tipo = await _pedir_ao_geoserver(parametros)
    if status != 200 or limiar <= 0:
        return status, conteudo, tipo
    # o filtro usa CPU: roda numa thread para não travar os outros usuários
    return 200, await asyncio.to_thread(filtrar_png, conteudo, limiar), 'image/png'


async def _pedir_ao_geoserver(parametros: dict[str, str]) -> tuple[int, bytes, str]:
    """Faz o pedido WMS e anota em `estado` se o GeoServer respondeu.

    Uma falha de conexão liga o disjuntor por RADAR_ESPERA_FALHA_S.

    Parâmetros:
        parametros: parâmetros já limpos, sem `dbz_min`.

    Retorna:
        (200, imagem, tipo) se deu certo; (502, mensagem, 'text/plain') se não.
    """
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
    return 200, resposta.content, tipo


# ---------------------------------------------------------------------------
# Parâmetros do pedido WMS
# ---------------------------------------------------------------------------

def janela_de_tempo(minutos: int = config.RADAR_JANELA_MIN) -> str:
    """Monta o intervalo TIME do WMS: 'início/agora' em UTC, sem milissegundos.

    Parâmetros:
        minutos: tamanho da janela, em minutos, terminando agora.

    Retorna:
        Texto como '2026-11-19T12:00:00Z/2026-11-19T12:30:00Z'.
    """
    agora = datetime.now(UTC).replace(microsecond=0)
    inicio = agora - timedelta(minutes=minutos)
    formato = '%Y-%m-%dT%H:%M:%SZ'
    return f'{inicio.strftime(formato)}/{agora.strftime(formato)}'


def filtro_cql(codigos: list[str]) -> str:
    """Monta o CQL_FILTER para os radares escolhidos.

    Só entram códigos conhecidos (evita injeção de CQL).

    Parâmetros:
        codigos: códigos dos radares ('al', 'be'...).

    Retorna:
        "radar_code IN ('al','be')", ou 'INCLUDE' (todos) se a lista ficar vazia ou completa.
    """
    validos = {radar.codigo for radar in RADARES}
    escolhidos = [codigo for codigo in codigos if codigo in validos]
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
    """Classifica um radar pela idade da sua imagem mais recente.

    Parâmetros:
        ultima: horário (UTC, com fuso) da última imagem; None = sem dados.
        agora: horário de referência; None = o relógio atual (os testes passam um fixo).

    Retorna:
        Uma chave de STATUS: 'green', 'yellow', 'red' ou 'gray'.
    """
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
    """Monta o CQL do WFS de status. Só aceita valores conhecidos (vão para o GeoServer).

    Parâmetros:
        produto: chave de PRODUTOS ('maxcappi' ou 'cappi').
        alcance: chave de ALCANCES ('400km' ou '250km').
        altura: chave de ALTURAS_CAPPI, em km; só entra no filtro do CAPPI.

    Retorna:
        O texto do filtro. Levanta ValueError se algum valor for desconhecido.
    """
    if produto not in PRODUTOS or alcance not in ALCANCES or altura not in ALTURAS_CAPPI:
        raise ValueError('produto, alcance ou altura desconhecido')
    cql = f"produto='{produto}_{alcance}'"
    if produto != 'cappi':
        return cql
    return cql + f' AND elevacao={altura}'


async def buscar_status(produto: str, alcance: str, altura: str) -> dict[str, datetime] | None:
    """Consulta no GeoServer o horário da última imagem de cada radar.

    Parâmetros:
        produto, alcance, altura: como em `filtro_status`.

    Retorna:
        {código do radar: horário da última imagem}. None se o GeoServer não respondeu
        (diferente de {} = respondeu, mas nenhum radar tem dados).
    """
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
    return _ultimas_por_radar(feicoes)


def _ultimas_por_radar(feicoes: list[dict]) -> dict[str, datetime]:
    """Lê as feições do WFS de status: código do radar -> horário da última imagem.

    O GeoServer manda o código em maiúsculas; feições de radar desconhecido ou sem data
    legível ficam de fora.

    Parâmetros:
        feicoes: lista `features` do GeoJSON devolvido pelo WFS.

    Retorna:
        {código do radar em minúsculas: horário}.
    """
    codigo_minusculo = {radar.codigo.upper(): radar.codigo for radar in RADARES}
    ultimas = {}
    for feicao in feicoes:
        propriedades = feicao.get('properties') or {}
        codigo = codigo_minusculo.get(str(propriedades.get('radar_code', '')).upper())
        quando = datas.ler_iso(propriedades.get('last_update'))
        if codigo and quando:
            ultimas[codigo] = quando
    return ultimas
