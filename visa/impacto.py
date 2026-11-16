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
    tipo: str                    # 'aerodromo' ou 'tma'
    ident: str                   # SBGR, SBSP...
    nome: str
    lat: float                   # onde centrar o mapa (aeródromo ou centro da TMA)
    lon: float
    nivel: str
    motivos: tuple[str, ...]
    pontos: float                # para ordenar dentro do nível


@dataclass
class Situacao:
    radar_quando: datetime | None = None
    raios_quando: datetime | None = None
    aerodromos: list[Impacto] = field(default_factory=list)
    tmas: list[Impacto] = field(default_factory=list)
    perigo: dict[geo.Chave, str] = field(default_factory=dict)   # célula 0,1° -> motivo (só fontes recentes)
    perigo_radar: dict[geo.Chave, str] = field(default_factory=dict)    # do último quadro, recente ou não
    perigo_raios: dict[geo.Chave, str] = field(default_factory=dict)
    fontes: frozenset = frozenset()          # previsao.RADAR e/ou previsao.RAIOS com quadro recente
    erro: str = ''
    versao: int = 0


situacao = Situacao()


# ---------------------------------------------------------------------------
# Regras (funções puras)
# ---------------------------------------------------------------------------

def classificar_aerodromo(dbz: int | None, raios: sonda.RaiosPerto, status: str) -> tuple[str | None, list[str], float]:
    """(nível ou None, motivos, pontos). `pontos` só ordena os itens de um mesmo nível."""
    c, novas, perto = raios.contagem, raios.novas, raios.mais_perto
    cor_metar, aviso = (status or ' ')[0], status.endswith('w')
    motivos = []
    if perto:
        motivos.append(f'raios a {perto[0]:.0f} km (célula ativa há {perto[1]} min)')
    if novas:
        motivos.append(f'{novas} célula(s) nova(s) até {sonda.RAIO_CELULAS_NOVAS_KM} km')
    if dbz is not None and dbz >= 20:
        motivos.append(f'{dbz} dBZ a até {RAIO_AERODROMO_KM} km')
    if cor_metar in STATUS_METAR:
        motivos.append(STATUS_METAR[cor_metar])
    if aviso:
        motivos.append('aviso de aeródromo em vigor')
    if c[10] or (dbz or 0) >= 45 or cor_metar == 'r':
        nivel = ALTO
    elif c[20] or novas or (dbz or 0) >= 35 or cor_metar == 'y' or aviso:
        nivel = MEDIO
    elif c[50] or (dbz or 0) >= 20:
        nivel = BAIXO
    else:
        return None, [], 0
    pontos = (dbz or 0) + 10 * c[10] + 3 * c[20] + c[50] + 5 * novas
    return nivel, motivos, pontos


def classificar_tma(dbz: int | None, celulas: int, novas: int) -> tuple[str | None, list[str], float]:
    motivos = []
    if celulas:
        motivos.append(f'{celulas} célula(s) com raios' + (f' ({novas} nova(s))' if novas else ''))
    if dbz is not None and dbz >= 20:
        motivos.append(f'máximo de {dbz} dBZ')
    if celulas >= 5 or (dbz or 0) >= 45:
        nivel = ALTO
    elif celulas or (dbz or 0) >= 35:
        nivel = MEDIO
    elif (dbz or 0) >= 20:
        nivel = BAIXO
    else:
        return None, [], 0
    return nivel, motivos, (dbz or 0) + 5 * celulas + 5 * novas


def ordenar(lista: list[Impacto]) -> list[Impacto]:
    return sorted(lista, key=lambda i: (ORDEM[i.nivel], -i.pontos, i.ident))


# ---------------------------------------------------------------------------
# Radar: dBZ de cada pixel das imagens
# ---------------------------------------------------------------------------

# caminho -> (eco sim/não compactado com np.packbits, forma). 1 bit por pixel (20 KB por imagem)
# em vez de 1 byte (160 KB): guarda as ~250 imagens das últimas 3 h em ~5 MB.
_eco_cache: dict[str, tuple[np.ndarray, tuple]] = {}


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
    """
    com_eco = ~np.isnan(campo)
    if not com_eco.any():
        return np.zeros(campo.shape, bool)
    la, lo = np.meshgrid(lats, lons, indexing='ij')
    dy = (la - lat0) * geo.KM_POR_GRAU
    dx = (lo - lon0) * geo.KM_POR_GRAU * np.cos(np.radians(lat0))
    distancia = np.hypot(dx, dy)
    n_fatias = round(360 / INTERF_FATIA_GRAUS)
    fatia = (np.degrees(np.arctan2(dx, dy)) % 360 / INTERF_FATIA_GRAUS).astype(int) % n_fatias
    degrau = (distancia / INTERF_DEGRAU_KM).astype(int)
    n_degraus = int(degrau.max()) + 1
    inicio = int(INTERF_INICIO_KM / INTERF_DEGRAU_KM)

    ocupado = np.zeros((n_fatias, n_degraus), bool)
    ocupado[fatia[com_eco], degrau[com_eco]] = True
    ocupado[:, :inicio] = False
    # Uma falha de um degrau não interrompe o raio (as linhas reais são pontilhadas)
    cheio = ocupado.copy()
    cheio[:, 1:-1] |= ocupado[:, :-2] & ocupado[:, 2:]

    minimo = int(INTERF_COMPRIMENTO_KM / INTERF_DEGRAU_KM)
    vizinhas = [d for k in range(3, 9) for d in (k, -k)]
    apagar = np.zeros((n_fatias, n_degraus), bool)
    for f in range(n_fatias):
        linha = cheio[f]
        if linha.sum() < minimo:
            continue
        # trechos contínuos da fatia
        bordas = np.diff(np.concatenate(([0], linha.astype(np.int8), [0])))
        for comeco, fim in zip(np.nonzero(bordas == 1)[0], np.nonzero(bordas == -1)[0]):
            if fim - comeco < minimo:
                continue
            ao_lado = np.median([cheio[(f + d) % n_fatias, comeco:fim].mean() for d in vizinhas])
            if ao_lado >= INTERF_VIZINHAS_FRACAO:
                continue
            largura = sum(1 for d in range(-6, 7) if cheio[(f + d) % n_fatias, comeco:fim].mean() > 0.5)
            if largura <= INTERF_LARGURA_MAX_GRAUS or (fim - comeco) * INTERF_DEGRAU_KM >= INTERF_LONGO_KM:
                for d in (-1, 0, 1):
                    apagar[(f + d) % n_fatias, comeco:fim] = True
    return apagar[fatia, degrau] & com_eco


def apagar_interferencia(campo: np.ndarray, imagem: redemet.Imagem) -> np.ndarray:
    """Tira os raios de interferência (raios_de_interferencia) da imagem de uma estação."""
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
    """Tira os pixels a menos de SEM_ECO_PERTO_DO_RADAR_KM da antena (eco de solo)."""
    estacao = radar.POR_CODIGO.get(imagem.codigo)
    if estacao is None:
        return campo
    lats, lons = coordenadas(imagem, campo.shape)
    dlat, dlon = geo.graus_por_km(estacao.lat, SEM_ECO_PERTO_DO_RADAR_KM)
    la, lo = np.meshgrid(lats, lons, indexing='ij')
    perto = ((la - estacao.lat) / dlat) ** 2 + ((lo - estacao.lon) / dlon) ** 2 <= 1
    campo = campo.copy()
    campo[perto] = np.nan
    return campo


def eco_fixo(mascaras: list[np.ndarray]) -> np.ndarray | None:
    """Pixels com eco em pelo menos ECO_FIXO_FRACAO dos quadros (None = poucos quadros para saber)."""
    if len(mascaras) < ECO_FIXO_QUADROS_MIN:
        return None
    return np.mean(np.array(mascaras), axis=0) >= ECO_FIXO_FRACAO


def dbz_da_imagem(img: np.ndarray) -> np.ndarray:
    """Imagem RGBA -> dBZ por pixel, com NaN em tudo que não é eco de 0 dBZ ou mais.

    (radar.FUNDO vale -1 e o cinza do CEMADEN, -5: os dois caem em `< 0`.)
    """
    dbz = radar.dbz_por_pixel(img)
    dbz[dbz < 0] = np.nan
    return dbz


def coordenadas(imagem: redemet.Imagem, forma: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Latitude e longitude do centro de cada pixel (grade lat/lon)."""
    altura, largura = forma
    lats = imagem.norte - np.arange(altura) / (altura - 1) * (imagem.norte - imagem.sul)
    lons = imagem.oeste + np.arange(largura) / (largura - 1) * (imagem.leste - imagem.oeste)
    return lats, lons


def dbz_robusto(valores: np.ndarray) -> int | None:
    """O PIXELS_MINIMOS-ésimo maior valor (None se houver menos pixels com eco que isso)."""
    if valores.size < PIXELS_MINIMOS:
        return None
    return round(float(np.partition(valores, -PIXELS_MINIMOS)[-PIXELS_MINIMOS]))


def dbz_max_perto(campos: list[tuple[redemet.Imagem, np.ndarray]], lat: float, lon: float,
                  raio_km: float) -> int | None:
    """dBZ (robusto) a até `raio_km` do ponto, o maior entre as estações que cobrem a área."""
    maior = None
    dlat, dlon = geo.graus_por_km(lat, raio_km)
    for imagem, campo in campos:
        if not (imagem.sul - dlat <= lat <= imagem.norte + dlat and imagem.oeste - dlon <= lon <= imagem.leste + dlon):
            continue
        lats, lons = coordenadas(imagem, campo.shape)
        linhas = np.nonzero(np.abs(lats - lat) <= dlat)[0]
        colunas = np.nonzero(np.abs(lons - lon) <= dlon)[0]
        if not len(linhas) or not len(colunas):
            continue
        janela = campo[linhas[0]:linhas[-1] + 1, colunas[0]:colunas[-1] + 1]
        la, lo = np.meshgrid(lats[linhas[0]:linhas[-1] + 1], lons[colunas[0]:colunas[-1] + 1], indexing='ij')
        perto = ((la - lat) / dlat) ** 2 + ((lo - lon) / dlon) ** 2 <= 1      # elipse ~ círculo em km
        v = dbz_robusto(janela[perto & ~np.isnan(janela)])
        if v is not None:
            maior = v if maior is None else max(maior, v)
    return maior


# ---------------------------------------------------------------------------
# TMAs (do arquivo local)
# ---------------------------------------------------------------------------

class TMA(NamedTuple):
    sigla: str                   # SBXP...
    nome: str
    aneis: list                  # anéis externos dos polígonos: [[lon, lat], ...]


_tmas: tuple[float, list[TMA]] | None = None


def agrupar_tmas(features: list[dict]) -> list[TMA]:
    """Uma TMA por sigla. As partes de uma TMA (SBXP, SBXP_01...) viram uma só."""
    grupos: dict[str, tuple[str, list]] = {}
    for f in features:
        g, p = f.get('geometry') or {}, f.get('properties') or {}
        if g.get('type') == 'Polygon':
            aneis = [g['coordinates'][0]]
        elif g.get('type') == 'MultiPolygon':
            aneis = [poligono[0] for poligono in g['coordinates']]
        else:
            continue
        sigla = str(p.get('ident') or '?').split('_')[0]
        nome = str(p.get('nam') or '')
        if sigla in grupos:
            grupos[sigla][1].extend(aneis)
            if len(nome) < len(grupos[sigla][0]):         # "São Paulo" em vez de "São Paulo 1"
                grupos[sigla] = (nome, grupos[sigla][1])
        else:
            grupos[sigla] = (nome, list(aneis))
    return [TMA(sigla, nome, aneis) for sigla, (nome, aneis) in grupos.items()]


def carregar_tmas() -> list[TMA]:
    """TMAs do arquivo local (relê se o arquivo mudar)."""
    global _tmas
    caminho = espaco.arquivo_local('tma')
    if caminho is None:
        return []
    mtime = caminho.stat().st_mtime
    if _tmas is None or _tmas[0] != mtime:
        _tmas = (mtime, agrupar_tmas(json.loads(caminho.read_text()).get('features', [])))
    return _tmas[1]


def avaliar_tma(sigla: str, nome: str, aneis: list, campos: list, celulas: tuple) -> Impacto | None:
    todos = np.array([p for anel in aneis for p in anel], float)
    oeste, sul = todos.min(axis=0)
    leste, norte = todos.max(axis=0)
    # Raios: células cujo centro está dentro da TMA
    dentro_raios = [(la, lo, m) for la, lo, m in celulas
                    if sul <= la <= norte and oeste <= lo <= leste
                    and any(geo.dentro(la, lo, anel) for anel in aneis)]
    novas = sum(1 for _, _, m in dentro_raios if m < redemet.CELULA_NOVA_MIN)
    # Radar: pixels com eco dentro da TMA
    maior = None
    for imagem, campo in campos:
        if imagem.leste < oeste or imagem.oeste > leste or imagem.norte < sul or imagem.sul > norte:
            continue
        lats, lons = coordenadas(imagem, campo.shape)
        linhas = np.nonzero((lats >= sul) & (lats <= norte))[0]
        colunas = np.nonzero((lons >= oeste) & (lons <= leste))[0]
        if not len(linhas) or not len(colunas):
            continue
        janela = campo[linhas[0]:linhas[-1] + 1, colunas[0]:colunas[-1] + 1]
        com_eco = ~np.isnan(janela) & (np.nan_to_num(janela) >= 20)
        if not com_eco.any():
            continue
        li, co = np.nonzero(com_eco)
        la, lo = lats[linhas[0] + li], lons[colunas[0] + co]
        dentro = np.zeros(la.shape, bool)
        for anel in aneis:
            dentro |= geo.dentro_vetor(la, lo, anel)
        v = dbz_robusto(janela[li[dentro], co[dentro]])
        if v is not None:
            maior = v if maior is None else max(maior, v)
    nivel, motivos, pontos = classificar_tma(maior, len(dentro_raios), novas)
    if nivel is None:
        return None
    return Impacto('tma', sigla, nome, float((sul + norte) / 2), float((oeste + leste) / 2),
                   nivel, tuple(motivos), pontos)


# ---------------------------------------------------------------------------
# Cálculo da situação (uma vez por minuto, para todos)
# ---------------------------------------------------------------------------

def mapa_de_perigo(campos: list, celulas: tuple) -> dict[geo.Chave, str]:
    """Células de 0,1° perigosas para o tráfego: com raios (e as vizinhas) ou eco forte."""
    perigo: dict[geo.Chave, str] = {}
    for imagem, campo in campos:
        li, co = np.nonzero(np.nan_to_num(campo) >= ECO_FORTE_DBZ)
        if li.size:
            lats, lons = coordenadas(imagem, campo.shape)
            for la, lo in zip(lats[li], lons[co]):
                perigo.setdefault(geo.chave_celula(la, lo), f'eco ≥ {ECO_FORTE_DBZ} dBZ')
    for la, lo, _ in celulas:
        base = geo.chave_celula(la, lo)
        for chave in geo.vizinhas(base):
            if chave == base or chave not in perigo or 'raios' not in perigo[chave]:
                perigo[chave] = 'raios na célula' if chave == base else 'raios a menos de 10 km'
    return perigo


def celulas_fortes(campos: list) -> dict[geo.Chave, int]:
    """Células de 0,1° com eco forte: chave -> dBZ (robusto). Para os núcleos da previsão.

    Mais exigente que `mapa_de_perigo`: a célula precisa de PIXELS_MINIMOS pixels fortes, para
    um pixel isolado não virar um núcleo que "anda" e gera conflitos falsos.
    """
    pixels: dict[geo.Chave, list[float]] = {}
    for imagem, campo in campos:
        li, co = np.nonzero(np.nan_to_num(campo) >= ECO_FORTE_DBZ)
        if li.size:
            lats, lons = coordenadas(imagem, campo.shape)
            for la, lo, v in zip(lats[li], lons[co], campo[li, co]):
                pixels.setdefault(geo.chave_celula(la, lo), []).append(float(v))
    return {k: dbz for k, valores in pixels.items() if (dbz := dbz_robusto(np.array(valores))) is not None}


def celulas_de_raios(quadro: redemet.QuadroRaios) -> dict[geo.Chave, int]:
    """Células com raios do quadro: chave -> minutos ativa."""
    return {geo.chave_celula(la, lo): m for la, lo, m in quadro.celulas}


def _nucleos(campos: list, campos_antes: list, radar_quando, radar_antes_quando,
             raios: redemet.QuadroRaios | None, raios_antes: redemet.QuadroRaios | None) -> None:
    quadro = previsao.QuadroCelulas
    previsao.atualizar_nucleos(
        quadro(radar_quando, celulas_fortes(campos)) if campos else None,
        quadro(radar_antes_quando, celulas_fortes(campos_antes)) if campos_antes else None,
        quadro(raios.quando, celulas_de_raios(raios)) if raios else None,
        quadro(raios_antes.quando, celulas_de_raios(raios_antes)) if raios_antes else None,
        vento.deslocamento(datetime.now(UTC)))


def fontes_validas(radar_quando: datetime | None, raios_quando: datetime | None, agora: datetime) -> frozenset:
    """As fontes cujo último quadro ainda é recente (RADAR_VALIDO_MIN, RAIOS_VALIDO_MIN)."""
    def recente(quando: datetime | None, limite_min: float) -> bool:
        return quando is not None and (agora - quando).total_seconds() / 60 <= limite_min
    return frozenset(f for f, ok in ((previsao.RADAR, recente(radar_quando, RADAR_VALIDO_MIN)),
                                     (previsao.RAIOS, recente(raios_quando, RAIOS_VALIDO_MIN))) if ok)


def combinar_perigo(radar_: dict, raios: dict, fontes: frozenset) -> dict[geo.Chave, str]:
    """O mapa de risco só com as fontes recentes. Raios valem mais que eco (como em mapa_de_perigo)."""
    return {**(radar_ if previsao.RADAR in fontes else {}), **(raios if previsao.RAIOS in fontes else {})}


def _calcular(campos: list, quadro_raios: redemet.QuadroRaios | None) -> tuple[list, list, dict, dict]:
    vazio = redemet.QuadroRaios(datetime.min, ())
    quadro_raios = quadro_raios or vazio
    lista_a = []
    for a in aerodromos.situacao.aerodromos:
        dbz = dbz_max_perto(campos, a.lat, a.lon, RAIO_AERODROMO_KM)
        perto = sonda.raios_perto(quadro_raios, a.lat, a.lon)
        nivel, motivos, pontos = classificar_aerodromo(dbz, perto, a.status)
        if nivel:
            lista_a.append(Impacto('aerodromo', a.icao, a.nome, a.lat, a.lon, nivel, tuple(motivos), pontos))
    lista_t = [i for t in carregar_tmas()
               if (i := avaliar_tma(t.sigla, t.nome, t.aneis, campos, quadro_raios.celulas))]
    # Um mapa por fonte, para tirar do cálculo a que ficar velha (combinar_perigo)
    return ordenar(lista_a), ordenar(lista_t), mapa_de_perigo(campos, ()), mapa_de_perigo([], quadro_raios.celulas)


async def atualizar() -> None:
    """Busca os quadros mais novos e recalcula (se algo mudou)."""
    try:
        quadros = await redemet.radar_quadros(PRODUTO_RADAR)
        raios = await redemet.raios_quadros()
    except redemet.Indisponivel as erro:
        situacao.erro = str(erro)
        return
    situacao.erro = ''          # a API respondeu: o aviso some já, mesmo sem quadro novo
    if not aerodromos.situacao.aerodromos:
        await aerodromos.obter()
    ultimo_radar = quadros[-1] if quadros else None
    ultimo_raios = raios[-1] if raios else None
    radar_quando = ultimo_radar.quando if ultimo_radar else None
    raios_quando = ultimo_raios.quando if ultimo_raios else None
    if (radar_quando, raios_quando) == (situacao.radar_quando, situacao.raios_quando) and situacao.versao:
        return
    # Onde há eco em cada quadro (guardado compacto: só sim/não, ~20 KB por imagem)
    mascaras: dict[str, list[np.ndarray]] = {}
    for quadro in quadros:
        for imagem in quadro.imagens:
            if imagem.caminho not in _eco_cache:
                bruta = await redemet.imagem_bruta(imagem)
                if bruta is None:
                    continue
                cache.guardar(_eco_cache, imagem.caminho, (np.packbits(bruta[..., 3] > 0), bruta.shape[:2]),
                              redemet.IMAGENS_EM_CACHE)
            empacotada, forma = _eco_cache[imagem.caminho]
            mascaras.setdefault(imagem.codigo, []).append(
                np.unpackbits(empacotada)[:forma[0] * forma[1]].reshape(forma).astype(bool))
    campos = await _campos(ultimo_radar, mascaras) if ultimo_radar else []
    # Quadros anteriores (10 a 40 min antes): só para medir o movimento dos núcleos
    radar_antes = previsao.anterior(quadros, ultimo_radar) if ultimo_radar else None
    campos_antes = await _campos(radar_antes, mascaras) if radar_antes else []
    raios_antes = previsao.anterior(raios, ultimo_raios) if ultimo_raios else None
    inicio = time.monotonic()
    lista_a, lista_t, perigo_radar, perigo_raios = await asyncio.to_thread(_calcular, campos, ultimo_raios)
    await asyncio.to_thread(_nucleos, campos, campos_antes, radar_quando,
                            radar_antes.quando if radar_antes else None, ultimo_raios, raios_antes)
    log.info('impacto: %d aeródromos, %d TMAs, %d células de perigo (%.1f s)',
             len(lista_a), len(lista_t), len({**perigo_radar, **perigo_raios}), time.monotonic() - inicio)
    situacao.aerodromos, situacao.tmas = lista_a, lista_t
    situacao.perigo_radar, situacao.perigo_raios = perigo_radar, perigo_raios
    situacao.radar_quando, situacao.raios_quando = radar_quando, raios_quando
    situacao.versao += 1


async def _campos(quadro: redemet.QuadroRadar, mascaras: dict) -> list:
    """(imagem, dBZ por pixel) de cada estação do quadro, sem a antena e sem o eco de solo."""
    campos = []
    for imagem in quadro.imagens:
        bruta = await redemet.imagem_bruta(imagem)
        if bruta is None:
            continue
        campo = apagar_centro(dbz_da_imagem(bruta), imagem)
        fixo = eco_fixo(mascaras.get(imagem.codigo, []))
        if fixo is not None and fixo.shape == campo.shape:
            campo[fixo] = np.nan
        campo = await asyncio.to_thread(apagar_interferencia, campo, imagem)
        campos.append((imagem, campo))
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
    # Cópia da lista aqui, no laço do servidor: o Pusher altera o dicionário enquanto a conta roda
    elegiveis = [a for a in list(aeronaves.trafego.aeronaves.values()) if previsao.em_rota(a)]
    em_perigo = {a.chave: (a.lat, a.lon) for a in elegiveis if alerta_da_aeronave(a.lat, a.lon)}
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
    return situacao.perigo.get(geo.chave_celula(lat, lon))
