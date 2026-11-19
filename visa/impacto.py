"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Painel de impacto: onde o tempo está afetando (ou vai afetar) a operação agora.

O servidor calcula uma vez por minuto, para todos os usuários, a partir do último
quadro de radar (MAXCAPPI, API) e de raios (stsc, API):
  - aeródromos: dBZ máximo até 10 km, células com raios a 10/20/50 km, células novas,
    status do METAR;
  - TMAs: células com raios dentro da TMA e dBZ máximo dentro dela;
  - tráfego: as células de 0,1° "perigosas" (com raios por perto ou eco forte), para
    destacar as aeronaves que estão nelas;
  - FAB.ia, conflitos previstos (previsao.py): os núcleos de eco forte e de raios, e quais
    aeronaves em rota vão cruzá-los nos próximos 30 min.

Os níveis (alto, médio, baixo) e os motivos ficam em funções puras, fáceis de ajustar.
Quem usa: `main.py` (tarefa `vigiar`), a tela (`situacao`, `NIVEIS`, `alerta_da_aeronave`)
e `previsao.py` (núcleos e mapa de perigo).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

import numpy as np

import aerodromos
import aeronaves
import cache
import espaco
import geo
import previsao
import radar
import redemet
import sonda
import vento

log = logging.getLogger(__name__)

PRODUTO_RADAR = 'maxcappi'
RAIO_AERODROMO_KM = 10
ECO_FORTE_DBZ = 40           # aeronave em eco igual ou acima disto entra em alerta
SEM_ECO_PERTO_DO_RADAR_KM = 2   # a própria antena
# Eco de solo não se move: pixel com eco em quase todos os quadros das últimas ~3 h é descartado.
# (Ex.: perto de SBSN, a 1 km do radar de Santarém, um pixel de 66 dBZ estava em 100% dos quadros.)
# Não dá para usar uma máscara grande em volta das antenas: 17 aeroportos ficam a ~1 km de um radar.
ECO_FIXO_FRACAO = 0.9
ECO_FIXO_QUADROS_MIN = 6
PIXELS_MINIMOS = 3           # o dBZ de uma área é o 3º maior valor: um pixel isolado (ruído, eco de
                             # solo) não dispara impacto; um núcleo de convecção ocupa vários pixels
# Raios de interferência (raios_de_interferencia): linhas radiais estreitas e compridas
INTERF_FATIA_GRAUS = 1
INTERF_DEGRAU_KM = 4
INTERF_INICIO_KM = 30          # perto da antena, uma fatia de 1° tem menos de um pixel
INTERF_COMPRIMENTO_KM = 60
INTERF_VIZINHAS_FRACAO = 0.3
INTERF_LARGURA_MAX_GRAUS = 4   # raio: até 4 fatias cheias em ±6°; manchas de chuva alongadas: 7 ou mais
INTERF_LONGO_KM = 150          # mais longo que isso, mesmo largo, é feixe (faixas de Petrolina, 16/11)
ATUALIZA_S = 60
# Até que idade um quadro entra no mapa de risco e no FAB.ia. Mais velho que isso, a fonte fica
# de fora (sem nenhuma, o FAB.ia para): alerta sobre dado velho engana a tela e o registro.
# Radar da API: quadro a cada 20 min, que chega com 10–20 min. Raios: a cada 6–7 min.
RADAR_VALIDO_MIN = 45
RAIOS_VALIDO_MIN = 20

ALTO, MEDIO, BAIXO = 'alto', 'medio', 'baixo'
NIVEIS = {ALTO: ('#ef4444', 'Impacto alto'), MEDIO: ('#f59e0b', 'Impacto médio'), BAIXO: ('#3b82f6', 'Atenção')}
ORDEM = {ALTO: 0, MEDIO: 1, BAIXO: 2}
STATUS_METAR = {'r': 'METAR: restrição', 'y': 'METAR: atenção'}


@dataclass(frozen=True, slots=True)
class Impacto:
    """Um item do painel de impacto (aeródromo ou TMA afetado).

    Atributos:
        tipo: 'aerodromo' ou 'tma'.
        ident: indicativo (SBGR, SBSP...).
        nome: nome por extenso.
        lat: latitude onde centrar o mapa (o aeródromo ou o centro da TMA), graus.
        lon: longitude onde centrar o mapa, graus.
        nivel: ALTO, MEDIO ou BAIXO.
        motivos: textos que explicam o nível, na ordem em que aparecem na tela.
        pontos: só para ordenar os itens dentro de um mesmo nível (maior primeiro).
    """
    tipo: str
    ident: str
    nome: str
    lat: float
    lon: float
    nivel: str
    motivos: tuple[str, ...]
    pontos: float


@dataclass
class Situacao:
    """O resultado do último cálculo, compartilhado por todos os usuários.

    Atributos:
        radar_quando: horário do último quadro de radar usado (None = nenhum ainda).
        raios_quando: horário do último quadro de raios usado (None = nenhum ainda).
        aerodromos: aeródromos com impacto, já ordenados.
        tmas: TMAs com impacto, já ordenadas.
        perigo: célula 0,1° -> motivo, só com as fontes recentes (o que a tela e o FAB.ia usam).
        perigo_radar: células perigosas pelo último quadro de radar, recente ou não.
        perigo_raios: células perigosas pelo último quadro de raios, recente ou não.
        fontes: previsao.RADAR e/ou previsao.RAIOS, as que têm quadro recente.
        erro: falha da API no último ciclo ('' = nenhuma).
        versao: aumenta a cada cálculo novo (0 = ainda não calculou).
    """
    radar_quando: datetime | None = None
    raios_quando: datetime | None = None
    aerodromos: list[Impacto] = field(default_factory=list)
    tmas: list[Impacto] = field(default_factory=list)
    perigo: dict[geo.Chave, str] = field(default_factory=dict)
    perigo_radar: dict[geo.Chave, str] = field(default_factory=dict)
    perigo_raios: dict[geo.Chave, str] = field(default_factory=dict)
    fontes: frozenset = frozenset()
    erro: str = ''
    versao: int = 0


situacao = Situacao()


# ---------------------------------------------------------------------------
# Regras (funções puras)
# ---------------------------------------------------------------------------

def classificar_aerodromo(dbz: int | None, raios: sonda.RaiosPerto, status: str) -> tuple[str | None, list[str], float]:
    """Decide o nível de impacto de um aeródromo e os motivos.

    Parâmetros:
        dbz: dBZ robusto a até RAIO_AERODROMO_KM (None = sem eco).
        raios: células com raios em volta do aeródromo (sonda.raios_perto).
        status: código de cor do METAR da REDEMET ('g', 'y', 'r'...); um 'w' no fim indica
            aviso de aeródromo em vigor.

    Retorna:
        (nível ou None, motivos, pontos). None = sem impacto (e motivos vazios). `pontos` só
        ordena os itens de um mesmo nível.
    """
    contagem = raios.contagem
    novas = raios.novas
    mais_perto = raios.mais_perto
    cor_metar = (status or ' ')[0]
    aviso = status.endswith('w')
    dbz_ou_zero = dbz or 0

    motivos = []
    if mais_perto:
        distancia_km, minutos_ativa = mais_perto
        motivos.append(f'raios a {distancia_km:.0f} km (célula ativa há {minutos_ativa} min)')
    if novas:
        motivos.append(f'{novas} célula(s) nova(s) até {sonda.RAIO_CELULAS_NOVAS_KM} km')
    if dbz is not None and dbz >= 20:
        motivos.append(f'{dbz} dBZ a até {RAIO_AERODROMO_KM} km')
    if cor_metar in STATUS_METAR:
        motivos.append(STATUS_METAR[cor_metar])
    if aviso:
        motivos.append('aviso de aeródromo em vigor')

    if contagem[10] or dbz_ou_zero >= 45 or cor_metar == 'r':
        nivel = ALTO
    elif contagem[20] or novas or dbz_ou_zero >= 35 or cor_metar == 'y' or aviso:
        nivel = MEDIO
    elif contagem[50] or dbz_ou_zero >= 20:
        nivel = BAIXO
    else:
        return None, [], 0
    pontos = dbz_ou_zero + 10 * contagem[10] + 3 * contagem[20] + contagem[50] + 5 * novas
    return nivel, motivos, pontos


def classificar_tma(dbz: int | None, celulas: int, novas: int) -> tuple[str | None, list[str], float]:
    """Decide o nível de impacto de uma TMA e os motivos.

    Parâmetros:
        dbz: dBZ robusto dentro da TMA (None = sem eco).
        celulas: quantas células com raios têm o centro dentro da TMA.
        novas: quantas dessas células são novas (redemet.CELULA_NOVA_MIN).

    Retorna:
        (nível ou None, motivos, pontos). None = sem impacto (e motivos vazios).
    """
    dbz_ou_zero = dbz or 0
    motivos = []
    if celulas:
        texto = f'{celulas} célula(s) com raios'
        if novas:
            texto += f' ({novas} nova(s))'
        motivos.append(texto)
    if dbz is not None and dbz >= 20:
        motivos.append(f'máximo de {dbz} dBZ')

    if celulas >= 5 or dbz_ou_zero >= 45:
        nivel = ALTO
    elif celulas or dbz_ou_zero >= 35:
        nivel = MEDIO
    elif dbz_ou_zero >= 20:
        nivel = BAIXO
    else:
        return None, [], 0
    return nivel, motivos, dbz_ou_zero + 5 * celulas + 5 * novas


def ordenar(lista: list[Impacto]) -> list[Impacto]:
    """Ordena os itens: nível (alto primeiro), depois mais pontos, depois o indicativo.

    Parâmetros:
        lista: itens de impacto, em qualquer ordem.

    Retorna:
        Uma lista nova, ordenada.
    """
    return sorted(lista, key=lambda item: (ORDEM[item.nivel], -item.pontos, item.ident))


# ---------------------------------------------------------------------------
# Radar: dBZ de cada pixel das imagens
# ---------------------------------------------------------------------------

# caminho -> (eco sim/não compactado com np.packbits, forma). 1 bit por pixel (20 KB por imagem)
# em vez de 1 byte (160 KB): guarda as ~250 imagens das últimas 3 h em ~5 MB.
_eco_cache: dict[str, tuple[np.ndarray, tuple]] = {}

# Fatias vizinhas (em graus de distância) que devem estar quase vazias para a fatia ser um raio
_INTERF_VIZINHAS_DISTANTES = (3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8, -8)
# Até quantas fatias para cada lado medem a largura do trecho
_INTERF_LARGURA_ALCANCE = 6


def _fatia_e_degrau_dos_pixels(lats: np.ndarray, lons: np.ndarray,
                               lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """Coloca cada pixel numa grade polar centrada na antena.

    Parâmetros:
        lats: latitude de cada linha da imagem, graus.
        lons: longitude de cada coluna da imagem, graus.
        lat0: latitude da antena, graus.
        lon0: longitude da antena, graus.

    Retorna:
        (fatia, degrau), dois arrays com a forma da imagem: o índice da fatia de azimute
        (INTERF_FATIA_GRAUS, 0 = norte, sentido horário) e o índice do degrau de distância
        (INTERF_DEGRAU_KM) de cada pixel.
    """
    lat_pixel, lon_pixel = np.meshgrid(lats, lons, indexing='ij')
    norte_km = (lat_pixel - lat0) * geo.KM_POR_GRAU
    leste_km = (lon_pixel - lon0) * geo.KM_POR_GRAU * np.cos(np.radians(lat0))
    distancia_km = np.hypot(leste_km, norte_km)
    azimute_graus = np.degrees(np.arctan2(leste_km, norte_km)) % 360
    n_fatias = round(360 / INTERF_FATIA_GRAUS)
    fatia = (azimute_graus / INTERF_FATIA_GRAUS).astype(int) % n_fatias
    degrau = (distancia_km / INTERF_DEGRAU_KM).astype(int)
    return fatia, degrau


def _grade_com_eco(com_eco: np.ndarray, fatia: np.ndarray, degrau: np.ndarray) -> np.ndarray:
    """Marca, na grade fatia × degrau, onde há eco (a partir de INTERF_INICIO_KM).

    Uma falha de um degrau entre dois degraus com eco conta como eco: as linhas de
    interferência reais são pontilhadas.

    Parâmetros:
        com_eco: True nos pixels com eco (forma da imagem).
        fatia: índice da fatia de cada pixel (_fatia_e_degrau_dos_pixels).
        degrau: índice do degrau de distância de cada pixel.

    Retorna:
        Array booleano (n_fatias, n_degraus).
    """
    n_fatias = round(360 / INTERF_FATIA_GRAUS)
    n_degraus = int(degrau.max()) + 1
    primeiro_degrau = int(INTERF_INICIO_KM / INTERF_DEGRAU_KM)

    ocupado = np.zeros((n_fatias, n_degraus), bool)
    ocupado[fatia[com_eco], degrau[com_eco]] = True
    ocupado[:, :primeiro_degrau] = False
    cheio = ocupado.copy()
    cheio[:, 1:-1] |= ocupado[:, :-2] & ocupado[:, 2:]
    return cheio


def _trechos_continuos(linha: np.ndarray) -> list[tuple[int, int]]:
    """Os trechos seguidos de True numa linha da grade.

    Parâmetros:
        linha: array booleano (os degraus de uma fatia).

    Retorna:
        Lista de (começo, fim), com `fim` exclusivo, como numa fatia do Python.
    """
    bordas = np.diff(np.concatenate(([0], linha.astype(np.int8), [0])))
    comecos = np.nonzero(bordas == 1)[0]
    fins = np.nonzero(bordas == -1)[0]
    return list(zip(comecos, fins))


def _fracao_com_eco(cheio: np.ndarray, fatia: int, comeco: int, fim: int) -> float:
    """Fração dos degraus [comeco, fim) da fatia que têm eco (a fatia dá a volta em 360°)."""
    n_fatias = cheio.shape[0]
    return cheio[fatia % n_fatias, comeco:fim].mean()


def _trecho_e_interferencia(cheio: np.ndarray, fatia: int, comeco: int, fim: int) -> bool:
    """Diz se um trecho contínuo de uma fatia é um raio de interferência (DECISOES 3.3).

    Fluxo:
        1. comprido o bastante (INTERF_COMPRIMENTO_KM);
        2. as fatias de 3 a 8 graus para cada lado quase vazias (mediana < INTERF_VIZINHAS_FRACAO);
        3. estreito (até INTERF_LARGURA_MAX_GRAUS fatias com eco em mais da metade do trecho,
           em ±6°) ou muito longo (INTERF_LONGO_KM).

    Parâmetros:
        cheio: grade fatia × degrau com eco (_grade_com_eco).
        fatia: índice da fatia.
        comeco: primeiro degrau do trecho.
        fim: degrau seguinte ao último do trecho.

    Retorna:
        True se o trecho deve sair.
    """
    degraus = fim - comeco
    if degraus < int(INTERF_COMPRIMENTO_KM / INTERF_DEGRAU_KM):
        return False

    fracoes_ao_lado = []
    for desvio in _INTERF_VIZINHAS_DISTANTES:
        fracoes_ao_lado.append(_fracao_com_eco(cheio, fatia + desvio, comeco, fim))
    if np.median(fracoes_ao_lado) >= INTERF_VIZINHAS_FRACAO:
        return False

    largura = 0
    for desvio in range(-_INTERF_LARGURA_ALCANCE, _INTERF_LARGURA_ALCANCE + 1):
        if _fracao_com_eco(cheio, fatia + desvio, comeco, fim) > 0.5:
            largura += 1
    estreito = largura <= INTERF_LARGURA_MAX_GRAUS
    muito_longo = degraus * INTERF_DEGRAU_KM >= INTERF_LONGO_KM
    return estreito or muito_longo


def raios_de_interferencia(campo: np.ndarray, lats: np.ndarray, lons: np.ndarray,
                           lat0: float, lon0: float) -> np.ndarray:
    """Máscara dos pixels em "raios" de interferência: faixas radiais estreitas que saem da antena.

    Interferência (Wi-Fi, outros transmissores, o Sol) aparece como uma linha de eco ao longo de
    um azimute, às vezes forte (Rio Branco, 16/11: 51 dBZ). Ela muda um pouco de quadro para
    quadro, então o filtro de eco fixo não a pega. Chuva de verdade é larga em azimute.

    Regra (DECISOES 3.3), numa fatia de INTERF_FATIA_GRAUS a partir de INTERF_INICIO_KM:
    1. eco contínuo (falhas de até um degrau de distância) por pelo menos INTERF_COMPRIMENTO_KM;
    2. as fatias de 3 a 8 graus para cada lado têm eco em menos de INTERF_VIZINHAS_FRACAO desse
       trecho (mediana);
    3. estreito (até INTERF_LARGURA_MAX_GRAUS fatias com eco em mais da metade do trecho, em ±6°)
       ou muito longo (INTERF_LONGO_KM). Sem isto, uma célula de chuva alongada na direção da
       antena, longe dela, também sairia (conferido em Jaraguari e Santiago, 16/11).
    Os pixels da fatia (e das duas vizinhas imediatas) nesse trecho saem.

    Fluxo:
        1. cada pixel ganha uma fatia de azimute e um degrau de distância (grade polar);
        2. a grade fatia × degrau marca onde há eco;
        3. cada trecho contínuo de cada fatia é testado (_trecho_e_interferencia);
        4. os trechos reprovados voltam para os pixels da imagem.

    Parâmetros:
        campo: dBZ por pixel, NaN onde não há eco.
        lats: latitude de cada linha, graus.
        lons: longitude de cada coluna, graus.
        lat0: latitude da antena, graus.
        lon0: longitude da antena, graus.

    Retorna:
        Array booleano com a forma de `campo`: True nos pixels com eco que devem sair.
    """
    com_eco = ~np.isnan(campo)
    if not com_eco.any():
        return np.zeros(campo.shape, bool)
    fatia, degrau = _fatia_e_degrau_dos_pixels(lats, lons, lat0, lon0)
    cheio = _grade_com_eco(com_eco, fatia, degrau)
    n_fatias = cheio.shape[0]
    minimo = int(INTERF_COMPRIMENTO_KM / INTERF_DEGRAU_KM)

    apagar = np.zeros(cheio.shape, bool)
    for indice in range(n_fatias):
        linha = cheio[indice]
        if linha.sum() < minimo:
            continue
        for comeco, fim in _trechos_continuos(linha):
            if not _trecho_e_interferencia(cheio, indice, comeco, fim):
                continue
            for desvio in (-1, 0, 1):
                apagar[(indice + desvio) % n_fatias, comeco:fim] = True
    return apagar[fatia, degrau] & com_eco


def apagar_interferencia(campo: np.ndarray, imagem: redemet.Imagem) -> np.ndarray:
    """Tira os raios de interferência (raios_de_interferencia) da imagem de uma estação.

    Parâmetros:
        campo: dBZ por pixel, NaN onde não há eco.
        imagem: a imagem da estação (área e código).

    Retorna:
        O campo com NaN nos raios (uma cópia), ou o próprio campo se não houver o que tirar
        ou a estação for desconhecida.
    """
    estacao = radar.POR_CODIGO.get(imagem.codigo)
    if estacao is None:
        return campo
    lats, lons = coordenadas(imagem, campo.shape)
    mascara = raios_de_interferencia(campo, lats, lons, estacao.lat, estacao.lon)
    if not mascara.any():
        return campo
    campo = campo.copy()
    campo[mascara] = np.nan
    return campo


def apagar_centro(campo: np.ndarray, imagem: redemet.Imagem) -> np.ndarray:
    """Tira os pixels a menos de SEM_ECO_PERTO_DO_RADAR_KM da antena (eco de solo).

    Parâmetros:
        campo: dBZ por pixel, NaN onde não há eco.
        imagem: a imagem da estação (área e código).

    Retorna:
        Uma cópia com NaN em volta da antena, ou o próprio campo se a estação for desconhecida.
    """
    estacao = radar.POR_CODIGO.get(imagem.codigo)
    if estacao is None:
        return campo
    lats, lons = coordenadas(imagem, campo.shape)
    dlat, dlon = geo.graus_por_km(estacao.lat, SEM_ECO_PERTO_DO_RADAR_KM)
    lat_pixel, lon_pixel = np.meshgrid(lats, lons, indexing='ij')
    perto = ((lat_pixel - estacao.lat) / dlat) ** 2 + ((lon_pixel - estacao.lon) / dlon) ** 2 <= 1
    campo = campo.copy()
    campo[perto] = np.nan
    return campo


def eco_fixo(mascaras: list[np.ndarray]) -> np.ndarray | None:
    """Pixels com eco em pelo menos ECO_FIXO_FRACAO dos quadros.

    Parâmetros:
        mascaras: onde há eco em cada quadro de uma estação (arrays booleanos da mesma forma).

    Retorna:
        Array booleano (True = eco fixo), ou None se houver poucos quadros para saber.
    """
    if len(mascaras) < ECO_FIXO_QUADROS_MIN:
        return None
    return np.mean(np.array(mascaras), axis=0) >= ECO_FIXO_FRACAO


def dbz_da_imagem(img: np.ndarray) -> np.ndarray:
    """Imagem RGBA -> dBZ por pixel, com NaN em tudo que não é eco de 0 dBZ ou mais.

    (radar.FUNDO vale -1 e o cinza do CEMADEN, -5: os dois caem em `< 0`.)

    Parâmetros:
        img: imagem RGBA (altura, largura, 4).

    Retorna:
        Array de float (altura, largura).
    """
    dbz = radar.dbz_por_pixel(img)
    dbz[dbz < 0] = np.nan
    return dbz


def coordenadas(imagem: redemet.Imagem, forma: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Latitude e longitude do centro de cada pixel (grade lat/lon).

    Parâmetros:
        imagem: a imagem (limites norte, sul, leste, oeste em graus).
        forma: (altura, largura) da imagem, em pixels.

    Retorna:
        (lats, lons): latitude de cada linha (de norte para sul) e longitude de cada coluna.
    """
    altura, largura = forma
    lats = imagem.norte - np.arange(altura) / (altura - 1) * (imagem.norte - imagem.sul)
    lons = imagem.oeste + np.arange(largura) / (largura - 1) * (imagem.leste - imagem.oeste)
    return lats, lons


def dbz_robusto(valores: np.ndarray) -> int | None:
    """O PIXELS_MINIMOS-ésimo maior valor.

    Parâmetros:
        valores: dBZ dos pixels com eco de uma área.

    Retorna:
        O dBZ arredondado, ou None se houver menos pixels com eco que PIXELS_MINIMOS.
    """
    if valores.size < PIXELS_MINIMOS:
        return None
    return round(float(np.partition(valores, -PIXELS_MINIMOS)[-PIXELS_MINIMOS]))


def _maior_dbz(atual: int | None, novo: int | None) -> int | None:
    """O maior dos dois dBZ, ignorando None (None só se os dois forem None)."""
    if novo is None:
        return atual
    if atual is None:
        return novo
    return max(atual, novo)


class _Recorte(NamedTuple):
    """Um pedaço retangular do campo de uma imagem.

    Atributos:
        janela: dBZ dos pixels do pedaço.
        lats: latitude de cada linha do pedaço, graus.
        lons: longitude de cada coluna do pedaço, graus.
    """
    janela: np.ndarray
    lats: np.ndarray
    lons: np.ndarray


def _recortar(campo: np.ndarray, lats: np.ndarray, lons: np.ndarray,
              linhas_dentro: np.ndarray, colunas_dentro: np.ndarray) -> _Recorte | None:
    """Recorta o retângulo que vai da primeira à última linha (e coluna) marcada.

    Parâmetros:
        campo: dBZ por pixel da imagem inteira.
        lats: latitude de cada linha da imagem.
        lons: longitude de cada coluna da imagem.
        linhas_dentro: True nas linhas que interessam.
        colunas_dentro: True nas colunas que interessam.

    Retorna:
        O recorte, ou None se nenhuma linha ou nenhuma coluna interessar.
    """
    linhas = np.nonzero(linhas_dentro)[0]
    colunas = np.nonzero(colunas_dentro)[0]
    if not len(linhas) or not len(colunas):
        return None
    faixa_linhas = slice(linhas[0], linhas[-1] + 1)
    faixa_colunas = slice(colunas[0], colunas[-1] + 1)
    return _Recorte(campo[faixa_linhas, faixa_colunas], lats[faixa_linhas], lons[faixa_colunas])


def _dbz_no_circulo(imagem: redemet.Imagem, campo: np.ndarray, lat: float, lon: float,
                    dlat: float, dlon: float) -> int | None:
    """dBZ robusto de uma imagem dentro da elipse (lat ± dlat, lon ± dlon), ~ um círculo em km.

    Parâmetros:
        imagem: a imagem da estação.
        campo: dBZ por pixel da imagem.
        lat: latitude do centro, graus.
        lon: longitude do centro, graus.
        dlat: raio em latitude, graus.
        dlon: raio em longitude, graus.

    Retorna:
        O dBZ robusto, ou None se não houver pixels com eco suficientes.
    """
    lats, lons = coordenadas(imagem, campo.shape)
    recorte = _recortar(campo, lats, lons, np.abs(lats - lat) <= dlat, np.abs(lons - lon) <= dlon)
    if recorte is None:
        return None
    lat_pixel, lon_pixel = np.meshgrid(recorte.lats, recorte.lons, indexing='ij')
    perto = ((lat_pixel - lat) / dlat) ** 2 + ((lon_pixel - lon) / dlon) ** 2 <= 1
    return dbz_robusto(recorte.janela[perto & ~np.isnan(recorte.janela)])


def dbz_max_perto(campos: list[tuple[redemet.Imagem, np.ndarray]], lat: float, lon: float,
                  raio_km: float) -> int | None:
    """dBZ (robusto) a até `raio_km` do ponto, o maior entre as estações que cobrem a área.

    Parâmetros:
        campos: (imagem, dBZ por pixel) de cada estação.
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.
        raio_km: raio em volta do ponto, km.

    Retorna:
        O maior dBZ, ou None se nenhuma estação tiver eco suficiente na área.
    """
    maior = None
    dlat, dlon = geo.graus_por_km(lat, raio_km)
    for imagem, campo in campos:
        cobre_lat = imagem.sul - dlat <= lat <= imagem.norte + dlat
        cobre_lon = imagem.oeste - dlon <= lon <= imagem.leste + dlon
        if not (cobre_lat and cobre_lon):
            continue
        maior = _maior_dbz(maior, _dbz_no_circulo(imagem, campo, lat, lon, dlat, dlon))
    return maior


# ---------------------------------------------------------------------------
# TMAs (do arquivo local)
# ---------------------------------------------------------------------------

class TMA(NamedTuple):
    """Uma TMA, com todas as suas partes juntas.

    Atributos:
        sigla: indicativo (SBXP...).
        nome: nome por extenso.
        aneis: anéis externos dos polígonos, cada um [[lon, lat], ...].
    """
    sigla: str
    nome: str
    aneis: list


_tmas: tuple[float, list[TMA]] | None = None


def _aneis_externos(geometria: dict) -> list | None:
    """Os anéis externos de um Polygon ou MultiPolygon do GeoJSON (None = outro tipo)."""
    tipo = geometria.get('type')
    if tipo == 'Polygon':
        return [geometria['coordinates'][0]]
    if tipo == 'MultiPolygon':
        return [poligono[0] for poligono in geometria['coordinates']]
    return None


def agrupar_tmas(features: list[dict]) -> list[TMA]:
    """Uma TMA por sigla. As partes de uma TMA (SBXP, SBXP_01...) viram uma só.

    Parâmetros:
        features: as features do GeoJSON de TMAs.

    Retorna:
        As TMAs, na ordem em que cada sigla apareceu pela primeira vez.
    """
    grupos: dict[str, tuple[str, list]] = {}
    for feature in features:
        geometria = feature.get('geometry') or {}
        propriedades = feature.get('properties') or {}
        aneis = _aneis_externos(geometria)
        if aneis is None:
            continue
        sigla = str(propriedades.get('ident') or '?').split('_')[0]
        nome = str(propriedades.get('nam') or '')
        if sigla not in grupos:
            grupos[sigla] = (nome, list(aneis))
            continue
        nome_atual, aneis_do_grupo = grupos[sigla]
        aneis_do_grupo.extend(aneis)
        if len(nome) < len(nome_atual):         # "São Paulo" em vez de "São Paulo 1"
            grupos[sigla] = (nome, aneis_do_grupo)
    return [TMA(sigla, nome, aneis) for sigla, (nome, aneis) in grupos.items()]


def carregar_tmas() -> list[TMA]:
    """TMAs do arquivo local (relê se o arquivo mudar).

    Retorna:
        As TMAs, ou lista vazia se não houver arquivo de TMAs.
    """
    global _tmas
    caminho = espaco.arquivo_local('tma')
    if caminho is None:
        return []
    mtime = caminho.stat().st_mtime
    if _tmas is None or _tmas[0] != mtime:
        _tmas = (mtime, agrupar_tmas(json.loads(caminho.read_text()).get('features', [])))
    return _tmas[1]


class _Limites(NamedTuple):
    """O retângulo que envolve uma área, em graus."""
    oeste: float
    sul: float
    leste: float
    norte: float


def _limites_dos_aneis(aneis: list) -> _Limites:
    """O retângulo que envolve todos os anéis ([[lon, lat], ...])."""
    pontos = np.array([ponto for anel in aneis for ponto in anel], float)
    oeste, sul = pontos.min(axis=0)
    leste, norte = pontos.max(axis=0)
    return _Limites(oeste, sul, leste, norte)


def _celulas_de_raios_na_tma(aneis: list, limites: _Limites, celulas: tuple) -> list[tuple[float, float, int]]:
    """As células com raios cujo centro está dentro da TMA.

    Parâmetros:
        aneis: anéis externos da TMA.
        limites: retângulo que envolve a TMA (corte rápido antes do teste do polígono).
        celulas: (lat, lon, minutos ativa) de cada célula do quadro de raios.

    Retorna:
        As células dentro da TMA, no mesmo formato.
    """
    dentro = []
    for lat, lon, minutos in celulas:
        if not (limites.sul <= lat <= limites.norte and limites.oeste <= lon <= limites.leste):
            continue
        if any(geo.dentro(lat, lon, anel) for anel in aneis):
            dentro.append((lat, lon, minutos))
    return dentro


def _dbz_na_tma(aneis: list, limites: _Limites, campos: list) -> int | None:
    """dBZ robusto dos pixels com eco (≥ 20 dBZ) dentro da TMA, o maior entre as estações.

    Parâmetros:
        aneis: anéis externos da TMA.
        limites: retângulo que envolve a TMA.
        campos: (imagem, dBZ por pixel) de cada estação.

    Retorna:
        O maior dBZ, ou None se nenhuma estação tiver eco suficiente dentro da TMA.
    """
    maior = None
    for imagem, campo in campos:
        fora = (imagem.leste < limites.oeste or imagem.oeste > limites.leste
                or imagem.norte < limites.sul or imagem.sul > limites.norte)
        if fora:
            continue
        lats, lons = coordenadas(imagem, campo.shape)
        recorte = _recortar(campo, lats, lons,
                            (lats >= limites.sul) & (lats <= limites.norte),
                            (lons >= limites.oeste) & (lons <= limites.leste))
        if recorte is None:
            continue
        janela = recorte.janela
        com_eco = ~np.isnan(janela) & (np.nan_to_num(janela) >= 20)
        if not com_eco.any():
            continue
        linhas_eco, colunas_eco = np.nonzero(com_eco)
        lat_eco = recorte.lats[linhas_eco]
        lon_eco = recorte.lons[colunas_eco]
        dentro = np.zeros(lat_eco.shape, bool)
        for anel in aneis:
            dentro |= geo.dentro_vetor(lat_eco, lon_eco, anel)
        maior = _maior_dbz(maior, dbz_robusto(janela[linhas_eco[dentro], colunas_eco[dentro]]))
    return maior


def avaliar_tma(sigla: str, nome: str, aneis: list, campos: list, celulas: tuple) -> Impacto | None:
    """Calcula o impacto numa TMA: células com raios e dBZ dentro dela.

    Parâmetros:
        sigla: indicativo da TMA.
        nome: nome por extenso.
        aneis: anéis externos dos polígonos ([[lon, lat], ...]).
        campos: (imagem, dBZ por pixel) de cada estação.
        celulas: (lat, lon, minutos ativa) de cada célula do quadro de raios.

    Retorna:
        O item de impacto, ou None se a TMA não tem impacto.
    """
    limites = _limites_dos_aneis(aneis)
    com_raios = _celulas_de_raios_na_tma(aneis, limites, celulas)
    novas = sum(1 for _, _, minutos in com_raios if minutos < redemet.CELULA_NOVA_MIN)
    dbz = _dbz_na_tma(aneis, limites, campos)
    nivel, motivos, pontos = classificar_tma(dbz, len(com_raios), novas)
    if nivel is None:
        return None
    centro_lat = float((limites.sul + limites.norte) / 2)
    centro_lon = float((limites.oeste + limites.leste) / 2)
    return Impacto('tma', sigla, nome, centro_lat, centro_lon, nivel, tuple(motivos), pontos)


# ---------------------------------------------------------------------------
# Cálculo da situação (uma vez por minuto, para todos)
# ---------------------------------------------------------------------------

def _pixels_fortes(campos: list):
    """Percorre os pixels com eco ≥ ECO_FORTE_DBZ de todas as estações.

    Parâmetros:
        campos: (imagem, dBZ por pixel) de cada estação.

    Retorna:
        Um gerador de (lat, lon, dBZ) de cada pixel forte.
    """
    for imagem, campo in campos:
        linhas, colunas = np.nonzero(np.nan_to_num(campo) >= ECO_FORTE_DBZ)
        if not linhas.size:
            continue
        lats, lons = coordenadas(imagem, campo.shape)
        yield from zip(lats[linhas], lons[colunas], campo[linhas, colunas])


def mapa_de_perigo(campos: list, celulas: tuple) -> dict[geo.Chave, str]:
    """Células de 0,1° perigosas para o tráfego: com raios (e as vizinhas) ou eco forte.

    Raios valem mais que eco: uma célula com eco forte e raios fica com o motivo dos raios.

    Parâmetros:
        campos: (imagem, dBZ por pixel) de cada estação.
        celulas: (lat, lon, minutos ativa) de cada célula do quadro de raios.

    Retorna:
        Chave da célula -> motivo (texto mostrado na tela).
    """
    perigo: dict[geo.Chave, str] = {}
    for lat, lon, _ in _pixels_fortes(campos):
        perigo.setdefault(geo.chave_celula(lat, lon), f'eco ≥ {ECO_FORTE_DBZ} dBZ')
    for lat, lon, _ in celulas:
        base = geo.chave_celula(lat, lon)
        for chave in geo.vizinhas(base):
            if chave == base:
                perigo[chave] = 'raios na célula'
            elif chave not in perigo or 'raios' not in perigo[chave]:
                perigo[chave] = 'raios a menos de 10 km'
    return perigo


def celulas_fortes(campos: list) -> dict[geo.Chave, int]:
    """Células de 0,1° com eco forte: chave -> dBZ (robusto). Para os núcleos da previsão.

    Mais exigente que `mapa_de_perigo`: a célula precisa de PIXELS_MINIMOS pixels fortes, para
    um pixel isolado não virar um núcleo que "anda" e gera conflitos falsos.

    Parâmetros:
        campos: (imagem, dBZ por pixel) de cada estação.

    Retorna:
        Chave da célula -> dBZ robusto, só das células com pixels fortes suficientes.
    """
    pixels: dict[geo.Chave, list[float]] = {}
    for lat, lon, valor in _pixels_fortes(campos):
        pixels.setdefault(geo.chave_celula(lat, lon), []).append(float(valor))
    fortes = {}
    for chave, valores in pixels.items():
        dbz = dbz_robusto(np.array(valores))
        if dbz is not None:
            fortes[chave] = dbz
    return fortes


def celulas_de_raios(quadro: redemet.QuadroRaios) -> dict[geo.Chave, int]:
    """Células com raios do quadro: chave -> minutos ativa."""
    return {geo.chave_celula(lat, lon): minutos for lat, lon, minutos in quadro.celulas}


def _recente(quando: datetime | None, agora: datetime, limite_min: float) -> bool:
    """O quadro de `quando` tem no máximo `limite_min` minutos (None = não há quadro)."""
    return quando is not None and (agora - quando).total_seconds() / 60 <= limite_min


def fontes_validas(radar_quando: datetime | None, raios_quando: datetime | None, agora: datetime) -> frozenset:
    """As fontes cujo último quadro ainda é recente (RADAR_VALIDO_MIN, RAIOS_VALIDO_MIN).

    Parâmetros:
        radar_quando: horário do último quadro de radar (None = nenhum).
        raios_quando: horário do último quadro de raios (None = nenhum).
        agora: o momento da verificação (UTC).

    Retorna:
        previsao.RADAR e/ou previsao.RAIOS; vazio se nenhuma fonte for recente.
    """
    fontes = set()
    if _recente(radar_quando, agora, RADAR_VALIDO_MIN):
        fontes.add(previsao.RADAR)
    if _recente(raios_quando, agora, RAIOS_VALIDO_MIN):
        fontes.add(previsao.RAIOS)
    return frozenset(fontes)


def combinar_perigo(radar_: dict, raios: dict, fontes: frozenset) -> dict[geo.Chave, str]:
    """O mapa de risco só com as fontes recentes. Raios valem mais que eco (como em mapa_de_perigo).

    Parâmetros:
        radar_: mapa de perigo do radar (chave -> motivo).
        raios: mapa de perigo dos raios (chave -> motivo).
        fontes: as fontes recentes (fontes_validas).

    Retorna:
        Um dicionário novo; onde os dois mapas têm a mesma célula, fica o motivo dos raios.
    """
    perigo = {}
    if previsao.RADAR in fontes:
        perigo.update(radar_)
    if previsao.RAIOS in fontes:
        perigo.update(raios)
    return perigo


def _impactos_nos_aerodromos(campos: list, quadro_raios: redemet.QuadroRaios) -> list[Impacto]:
    """Avalia cada aeródromo da lista da REDEMET (só os com impacto entram)."""
    lista = []
    for aerodromo in aerodromos.situacao.aerodromos:
        dbz = dbz_max_perto(campos, aerodromo.lat, aerodromo.lon, RAIO_AERODROMO_KM)
        perto = sonda.raios_perto(quadro_raios, aerodromo.lat, aerodromo.lon)
        nivel, motivos, pontos = classificar_aerodromo(dbz, perto, aerodromo.status)
        if nivel:
            lista.append(Impacto('aerodromo', aerodromo.icao, aerodromo.nome, aerodromo.lat, aerodromo.lon,
                                 nivel, tuple(motivos), pontos))
    return lista


def _impactos_nas_tmas(campos: list, quadro_raios: redemet.QuadroRaios) -> list[Impacto]:
    """Avalia cada TMA do arquivo local (só as com impacto entram)."""
    lista = []
    for tma in carregar_tmas():
        item = avaliar_tma(tma.sigla, tma.nome, tma.aneis, campos, quadro_raios.celulas)
        if item:
            lista.append(item)
    return lista


def _calcular(campos: list, quadro_raios: redemet.QuadroRaios | None) -> tuple[list, list, dict, dict]:
    """Calcula aeródromos, TMAs e os mapas de perigo de um ciclo.

    Parâmetros:
        campos: (imagem, dBZ por pixel) de cada estação do último quadro de radar.
        quadro_raios: o último quadro de raios (None = nenhum).

    Retorna:
        (aeródromos, TMAs, perigo do radar, perigo dos raios). Um mapa por fonte, para tirar do
        cálculo a que ficar velha (combinar_perigo).
    """
    quadro_raios = quadro_raios or redemet.QuadroRaios(datetime.min, ())
    return (ordenar(_impactos_nos_aerodromos(campos, quadro_raios)),
            ordenar(_impactos_nas_tmas(campos, quadro_raios)),
            mapa_de_perigo(campos, ()),
            mapa_de_perigo([], quadro_raios.celulas))


# Os últimos quadros que cada fonte entregou. Se uma fonte falhar, a outra segue sendo calculada
# com os quadros dela, e a que falhou fica com os últimos que deu: eles envelhecem e saem do
# cálculo sozinhos (fontes_validas). Antes, os raios fora do ar paravam também o radar.
_ultimos_quadros: dict[str, list] = {previsao.RADAR: [], previsao.RAIOS: []}


async def _buscar_quadros(fonte: str, buscar, erros: list[str]) -> list:
    """Busca os quadros de uma fonte; se falhar, fica com os últimos que ela deu.

    Parâmetros:
        fonte: previsao.RADAR ou previsao.RAIOS.
        buscar: função assíncrona sem argumentos que devolve a lista de quadros.
        erros: lista onde o erro, se houver, é acrescentado ('fonte: erro').

    Retorna:
        Os quadros novos, ou os últimos guardados se a busca falhou.
    """
    try:
        _ultimos_quadros[fonte] = await buscar()
    except redemet.Indisponivel as erro:
        erros.append(f'{fonte}: {erro}')
    return _ultimos_quadros[fonte]


@dataclass
class _QuadrosDoCiclo:
    """Os quadros que um ciclo usa no cálculo.

    Atributos:
        radar: o último quadro de radar (None = nenhum).
        radar_antes: um quadro de radar de 10 a 40 min antes, só para o movimento dos núcleos.
        raios: o último quadro de raios (None = nenhum).
        raios_antes: um quadro de raios de 10 a 40 min antes.
    """
    radar: redemet.QuadroRadar | None
    radar_antes: redemet.QuadroRadar | None
    raios: redemet.QuadroRaios | None
    raios_antes: redemet.QuadroRaios | None


def _ultimo(quadros: list):
    """O último quadro da lista (None = lista vazia)."""
    if not quadros:
        return None
    return quadros[-1]


def _quando(quadro) -> datetime | None:
    """O horário do quadro (None = sem quadro)."""
    if quadro is None:
        return None
    return quadro.quando


def _anterior(quadros: list, ultimo):
    """O quadro de 10 a 40 min antes do último (previsao.anterior); None se não há último."""
    if ultimo is None:
        return None
    return previsao.anterior(quadros, ultimo)


def _escolher_quadros(quadros_radar: list, quadros_raios: list) -> _QuadrosDoCiclo:
    """Separa, de cada fonte, o último quadro e o anterior (para medir o movimento dos núcleos)."""
    ultimo_radar = _ultimo(quadros_radar)
    ultimo_raios = _ultimo(quadros_raios)
    return _QuadrosDoCiclo(radar=ultimo_radar,
                           radar_antes=_anterior(quadros_radar, ultimo_radar),
                           raios=ultimo_raios,
                           raios_antes=_anterior(quadros_raios, ultimo_raios))


def _imagens_a_baixar(quadros_radar: list, escolhidos: _QuadrosDoCiclo) -> list[redemet.Imagem]:
    """As imagens cujo PNG o ciclo precisa.

    São as imagens ainda sem máscara de eco (_eco_cache) e as dos dois quadros de radar usados
    no cálculo, nessa ordem (pode haver repetidas).
    """
    imagens = []
    for quadro in quadros_radar:
        for imagem in quadro.imagens:
            if imagem.caminho not in _eco_cache:
                imagens.append(imagem)
    for quadro in (escolhidos.radar, escolhidos.radar_antes):
        if quadro is not None:
            imagens.extend(quadro.imagens)
    return imagens


async def _baixar_pngs(imagens: list[redemet.Imagem]) -> dict[str, bytes]:
    """Baixa o PNG original de cada imagem (uma vez por caminho).

    Retorna:
        Caminho -> PNG; as imagens que não vieram ficam de fora.
    """
    pngs: dict[str, bytes] = {}
    for imagem in imagens:
        if imagem.caminho in pngs:
            continue
        png = await redemet.png_original(imagem.caminho)
        if png is not None:
            pngs[imagem.caminho] = png
    return pngs


async def atualizar() -> None:
    """Busca os quadros mais novos e recalcula (se algo mudou).

    Aqui, no laço do servidor, só a rede (a lista de quadros e os PNGs). Decodificar as
    imagens, ler o dBZ e aplicar os filtros usa CPU e vai inteiro para uma thread
    (`_processar`): no laço, isso congelaria a tela de todos os usuários.

    Fluxo:
        1. busca os quadros de radar e de raios (cada fonte separada);
        2. se nenhum quadro mudou, para por aqui;
        3. baixa os PNGs que faltam;
        4. calcula tudo numa thread (_processar) e publica em `situacao`.
    """
    erros: list[str] = []
    quadros_radar = await _buscar_quadros(previsao.RADAR, lambda: redemet.radar_quadros(PRODUTO_RADAR), erros)
    quadros_raios = await _buscar_quadros(previsao.RAIOS, redemet.raios_quadros, erros)
    situacao.erro = '; '.join(erros)    # a API respondeu: o aviso some já, mesmo sem quadro novo
    if len(erros) == 2:
        return
    if not aerodromos.situacao.aerodromos:
        await aerodromos.obter()

    escolhidos = _escolher_quadros(quadros_radar, quadros_raios)
    radar_quando = _quando(escolhidos.radar)
    raios_quando = _quando(escolhidos.raios)
    nada_mudou = (radar_quando, raios_quando) == (situacao.radar_quando, situacao.raios_quando)
    if nada_mudou and situacao.versao:
        return

    pngs = await _baixar_pngs(_imagens_a_baixar(quadros_radar, escolhidos))
    inicio = time.monotonic()
    lista_aerodromos, lista_tmas, perigo_radar, perigo_raios = await asyncio.to_thread(
        _processar, quadros_radar, pngs, escolhidos)
    log.info('impacto: %d aeródromos, %d TMAs, %d células de perigo (%.1f s)',
             len(lista_aerodromos), len(lista_tmas), len({**perigo_radar, **perigo_raios}),
             time.monotonic() - inicio)
    situacao.aerodromos = lista_aerodromos
    situacao.tmas = lista_tmas
    situacao.perigo_radar = perigo_radar
    situacao.perigo_raios = perigo_raios
    situacao.radar_quando = radar_quando
    situacao.raios_quando = raios_quando
    situacao.versao += 1


def _decodificar(png: bytes | None) -> np.ndarray | None:
    """PNG -> imagem RGBA (None se não há PNG ou ele está corrompido)."""
    if png is None:
        return None
    try:
        return redemet.decodificar(png)
    except OSError:                     # PNG corrompido
        return None


def _mascaras_de_eco(quadros_radar: list, pngs: dict[str, bytes]) -> dict[str, list[np.ndarray]]:
    """Onde há eco em cada quadro, por estação (para achar o eco fixo).

    Cada máscara fica guardada compacta em _eco_cache (só sim/não, ~20 KB por imagem): só as
    imagens novas são decodificadas.

    Parâmetros:
        quadros_radar: todos os quadros de radar das últimas ~3 h.
        pngs: caminho -> PNG original, já baixado no laço.

    Retorna:
        Código da estação -> máscaras (arrays booleanos), na ordem dos quadros. Imagens sem PNG
        e fora do cache ficam de fora.
    """
    mascaras: dict[str, list[np.ndarray]] = {}
    for quadro in quadros_radar:
        for imagem in quadro.imagens:
            if imagem.caminho not in _eco_cache:
                bruta = _decodificar(pngs.get(imagem.caminho))
                if bruta is None:
                    continue
                compactada = (np.packbits(bruta[..., 3] > 0), bruta.shape[:2])
                cache.guardar(_eco_cache, imagem.caminho, compactada, redemet.IMAGENS_EM_CACHE)
            empacotada, forma = _eco_cache[imagem.caminho]
            altura, largura = forma
            mascara = np.unpackbits(empacotada)[:altura * largura].reshape(forma).astype(bool)
            mascaras.setdefault(imagem.codigo, []).append(mascara)
    return mascaras


def _quadro_de_celulas_fortes(quadro: redemet.QuadroRadar | None, campos: list) -> previsao.QuadroCelulas | None:
    """As células de eco forte de um quadro de radar, para os núcleos (None = sem campos)."""
    if not campos:
        return None
    return previsao.QuadroCelulas(_quando(quadro), celulas_fortes(campos))


def _quadro_de_celulas_de_raios(quadro: redemet.QuadroRaios | None) -> previsao.QuadroCelulas | None:
    """As células com raios de um quadro, para os núcleos (None = sem quadro)."""
    if not quadro:
        return None
    return previsao.QuadroCelulas(quadro.quando, celulas_de_raios(quadro))


def _processar(quadros_radar: list, pngs: dict[str, bytes], escolhidos: _QuadrosDoCiclo) -> tuple[list, list, dict, dict]:
    """Todo o trabalho de CPU de um ciclo (roda numa thread).

    Fluxo:
        1. máscaras de eco de todas as imagens (para o eco fixo);
        2. campos de dBZ do último quadro de radar e do anterior, já filtrados;
        3. aeródromos, TMAs e mapas de perigo (_calcular);
        4. núcleos do FAB.ia (previsao.atualizar_nucleos).

    Parâmetros:
        quadros_radar: todos os quadros de radar das últimas ~3 h.
        pngs: caminho -> PNG original, já baixado no laço.
        escolhidos: os quadros usados no cálculo (_escolher_quadros).

    Retorna:
        O mesmo que _calcular: (aeródromos, TMAs, perigo do radar, perigo dos raios).
    """
    mascaras = _mascaras_de_eco(quadros_radar, pngs)
    campos = _campos(escolhidos.radar, pngs, mascaras) if escolhidos.radar else []
    campos_antes = _campos(escolhidos.radar_antes, pngs, mascaras) if escolhidos.radar_antes else []
    resultado = _calcular(campos, escolhidos.raios)
    previsao.atualizar_nucleos(
        _quadro_de_celulas_fortes(escolhidos.radar, campos),
        _quadro_de_celulas_fortes(escolhidos.radar_antes, campos_antes),
        _quadro_de_celulas_de_raios(escolhidos.raios),
        _quadro_de_celulas_de_raios(escolhidos.raios_antes),
        vento.deslocamento(datetime.now(UTC)))
    return resultado


def _campos(quadro: redemet.QuadroRadar, pngs: dict[str, bytes], mascaras: dict) -> list:
    """(imagem, dBZ por pixel) de cada estação do quadro, sem a antena e sem o eco de solo.

    Fluxo (em cada imagem):
        1. dBZ pela cor; tira o círculo em volta da antena (apagar_centro);
        2. tira o eco fixo (eco_fixo), se houver quadros suficientes;
        3. tira os raios de interferência (apagar_interferencia).

    Parâmetros:
        quadro: o quadro de radar.
        pngs: caminho -> PNG original.
        mascaras: código da estação -> máscaras de eco (_mascaras_de_eco).

    Retorna:
        Lista de (imagem, campo); as imagens sem PNG ficam de fora.
    """
    campos = []
    for imagem in quadro.imagens:
        bruta = _decodificar(pngs.get(imagem.caminho))
        if bruta is None:
            continue
        campo = apagar_centro(dbz_da_imagem(bruta), imagem)
        fixo = eco_fixo(mascaras.get(imagem.codigo, []))
        if fixo is not None and fixo.shape == campo.shape:
            campo[fixo] = np.nan
        campos.append((imagem, apagar_interferencia(campo, imagem)))
    return campos


async def prever() -> None:
    """FAB.ia: conflitos previstos, a cada minuto (as aeronaves andam mesmo sem quadro novo).

    Antes, confere a idade dos quadros: fonte velha sai do mapa de risco (âmbar na tela e
    verificação) e dos núcleos; sem nenhuma fonte recente, o FAB.ia para.
    """
    if not situacao.versao:
        return                    # sem mapa de perigo ainda: o verificador contaria entradas falsas
    agora = datetime.now(UTC)
    situacao.fontes = fontes_validas(situacao.radar_quando, situacao.raios_quando, agora)
    situacao.perigo = combinar_perigo(situacao.perigo_radar, situacao.perigo_raios, situacao.fontes)
    if not situacao.fontes:
        previsao.pausar(agora, f'sem quadro recente de radar (até {RADAR_VALIDO_MIN} min) '
                               f'nem de raios (até {RAIOS_VALIDO_MIN} min)')
        return
    # Cópia da lista aqui, no laço do servidor: o Pusher altera o dicionário enquanto a conta roda.
    # Só posições recentes: a aeronave fica no mapa até 5 min depois de sumir, mas projetá-la
    # dali como se fosse agora erra dezenas de km (e a contaria como "presente" na verificação).
    relogio = time.monotonic()
    elegiveis = [aeronave for aeronave in list(aeronaves.trafego.aeronaves.values())
                 if previsao.posicao_recente(aeronave, relogio) and previsao.em_rota(aeronave)]
    em_perigo = {aeronave.chave: (aeronave.lat, aeronave.lon) for aeronave in elegiveis
                 if alerta_da_aeronave(aeronave.lat, aeronave.lon)}
    await asyncio.to_thread(previsao.ciclo, agora, elegiveis, em_perigo, situacao.fontes)


async def vigiar() -> None:
    """Tarefa de fundo do servidor: recalcula a cada minuto."""
    while True:
        try:
            await atualizar()
            await prever()
        except Exception:                      # nunca derruba a tarefa
            log.exception('impacto: falha no cálculo')
        await asyncio.sleep(ATUALIZA_S)


def alerta_da_aeronave(lat: float, lon: float) -> str | None:
    """O motivo de perigo da célula onde a aeronave está.

    Parâmetros:
        lat: latitude da aeronave, graus.
        lon: longitude da aeronave, graus.

    Retorna:
        O motivo (texto), ou None se a célula não é perigosa.
    """
    return situacao.perigo.get(geo.chave_celula(lat, lon))
