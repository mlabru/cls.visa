"""
Sonda: o que se sabe sobre um ponto do mapa.

Clique direito no mapa (ou clique com a sonda ligada) e o painel da direita mostra, para
aquele ponto: radar (dBZ agora e nas últimas horas), raios por perto, se está numa área
de tempo severo prevista, o perfil vertical do vento e o aeródromo mais próximo.

Tudo é lido de dados que o servidor já tem (API da REDEMET, arquivos de vento,
aeródromos). O radar é lido da cor do pixel das imagens — a mesma tabela de cores do
filtro de dBZ.
"""
import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

import numpy as np

import aerodromos
import config
import geo
import radar
import redemet
import vento

RAIOS_DISTANCIAS_KM = (10, 20, 50)
RAIO_CELULAS_NOVAS_KM = 20
KT_POR_MS = 1.943844


# ---------------------------------------------------------------------------
# Contas (funções puras)
# ---------------------------------------------------------------------------

def cobre(imagem: redemet.Imagem, lat: float, lon: float) -> bool:
    return imagem.sul <= lat <= imagem.norte and imagem.oeste <= lon <= imagem.leste


def pixel(img: np.ndarray, imagem: redemet.Imagem, lat: float, lon: float) -> np.ndarray:
    """Cor RGBA no ponto (a imagem original está em grade lat/lon)."""
    altura, largura = img.shape[:2]
    lin = round((imagem.norte - lat) / (imagem.norte - imagem.sul) * (altura - 1))
    col = round((lon - imagem.oeste) / (imagem.leste - imagem.oeste) * (largura - 1))
    return img[min(max(lin, 0), altura - 1), min(max(col, 0), largura - 1)]


def dbz_da_cor(rgba: np.ndarray) -> float | None:
    """dBZ de um pixel do radar (None = sem eco: transparente, fundo ou abaixo de 0 dBZ)."""
    if rgba[3] == 0:
        return None
    dbz = float(radar.classificar_cores(np.array([rgba[:3]]))[0])
    return None if dbz == radar.FUNDO or dbz < 0 else round(dbz)


def vento_no_ponto(registros: list[dict], lat: float, lon: float) -> tuple[int, int] | None:
    """(direção de onde vem, em graus; velocidade em kt) no ponto de grade mais próximo."""
    try:
        u_reg, v_reg = registros[0], registros[1]
        h = u_reg['header']
        col = round(((lon - h['lo1']) % 360) / h['dx'])
        lin = round((h['la1'] - lat) / h['dy'])
        if not (0 <= col < h['nx'] and 0 <= lin < h['ny']):
            return None
        u, v = u_reg['data'][lin * h['nx'] + col], v_reg['data'][lin * h['nx'] + col]
    except (IndexError, KeyError, TypeError):
        return None
    if u is None or v is None:
        return None
    direcao = round((270 - math.degrees(math.atan2(v, u))) % 360)
    return (direcao if direcao else 360), round(math.hypot(u, v) * KT_POR_MS)


_FL_DA_PRESSAO = {pa: fl for fl, pa in config.VENTO_FL_PARA_PA.items()}


def fl_da_pressao(pa: int) -> int:
    """Nível de voo de um nível de pressão: o mesmo do painel de vento (500 hPa = FL180);
    para os outros níveis, a atmosfera padrão arredondada de 5 em 5."""
    if pa in _FL_DA_PRESSAO:
        return _FL_DA_PRESSAO[pa]
    pes = 145366.45 * (1 - (pa / 101325) ** 0.190284)
    return round(pes / 100 / 5) * 5


@dataclass(frozen=True)
class RaiosPerto:
    """Células com raios em volta de um ponto (resultado de `raios_perto`)."""
    contagem: dict[int, int]                    # distância (km) -> células até essa distância
    novas: int                                  # células novas a até RAIO_CELULAS_NOVAS_KM
    mais_perto: tuple[float, int] | None        # (distância em km, minutos ativa) da mais próxima


def raios_perto(quadro: redemet.QuadroRaios, lat: float, lon: float) -> RaiosPerto:
    """Células com raios em volta do ponto: quantas em cada distância, a mais próxima, as novas."""
    contagem = dict.fromkeys(RAIOS_DISTANCIAS_KM, 0)
    novas, mais_perto = 0, None
    limite = max(RAIOS_DISTANCIAS_KM)
    for la, lo, minutos in quadro.celulas:
        # Corte grosseiro antes da conta exata: 1° de lat ≈ 111 km > 50 km; 1,5° de lon cobre
        # 50 km até ~60° de latitude. Evita o haversine nas milhares de células distantes.
        if abs(la - lat) > 1 or abs(lo - lon) > 1.5:
            continue
        d = geo.distancia_km(lat, lon, la, lo)
        if d > limite:
            continue
        for r in RAIOS_DISTANCIAS_KM:
            if d <= r:
                contagem[r] += 1
        if d <= RAIO_CELULAS_NOVAS_KM and minutos < redemet.CELULA_NOVA_MIN:
            novas += 1
        if mais_perto is None or d < mais_perto[0]:
            mais_perto = (d, minutos)
    return RaiosPerto(contagem, novas, mais_perto)


def em_area_severa(feicoes: list[dict], tipo: str, raio_km: float, lat: float, lon: float) -> bool:
    """O ponto está numa área (ou a menos de raio_km de um ponto) de tempo severo previsto?"""
    for f in feicoes:
        g = f.get('geometry') or {}
        forma = g.get('type')
        if tipo == 'ponto' and forma == 'Point':
            plon, plat = g['coordinates'][:2]
            atinge = geo.distancia_km(lat, lon, plat, plon) <= raio_km
        elif forma == 'Polygon':
            atinge = geo.dentro(lat, lon, g['coordinates'][0])
        elif forma == 'MultiPolygon':
            atinge = any(geo.dentro(lat, lon, poligono[0]) for poligono in g['coordinates'])
        else:
            atinge = False
        if atinge:
            return True
    return False


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

class RadarAgora(NamedTuple):
    dbz: int | None              # None = sem eco
    estacao: str                 # sigla da estação de maior dBZ
    quando: datetime             # horário do quadro


class AerodromoPerto(NamedTuple):
    aerodromo: aerodromos.Aerodromo
    km: float


@dataclass
class Resultado:
    lat: float
    lon: float
    radar_agora: RadarAgora | None = None
    radar_historico: list[tuple[datetime, int | None]] = field(default_factory=list)
    radar_erro: str = ''
    raios: RaiosPerto | None = None
    raios_quando: datetime | None = None
    raios_erro: str = ''
    severo: bool | None = None            # None = camada desligada ou sem dados
    severo_validade: str | None = None
    vento: list[tuple[int, int, int]] = field(default_factory=list)       # (FL, direção, kt)
    vento_referencia: datetime | None = None
    aerodromo: AerodromoPerto | None = None

    @property
    def titulo(self) -> str:
        ns = 'S' if self.lat < 0 else 'N'
        lo = 'O' if self.lon < 0 else 'L'
        return f'{abs(self.lat):.2f}°{ns} {abs(self.lon):.2f}°{lo}'.replace('.', ',')


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

async def _dbz_no_quadro(imagens: tuple[redemet.Imagem, ...], lat: float, lon: float):
    """O maior dBZ entre as estações que cobrem o ponto: (dBZ, sigla da estação)."""
    maior, sigla_maior = None, ''
    for imagem in imagens:
        if not imagem.caminho or not cobre(imagem, lat, lon):
            continue
        img = await redemet.imagem_bruta(imagem)
        if img is None:
            continue
        dbz = dbz_da_cor(pixel(img, imagem, lat, lon))
        sigla = radar.POR_CODIGO[imagem.codigo].sigla if imagem.codigo in radar.POR_CODIGO else imagem.codigo
        sigla_maior = sigla_maior or sigla             # sem eco: mostra a primeira estação que cobre
        if dbz is not None and (maior is None or dbz > maior):
            maior, sigla_maior = dbz, sigla
    return maior, sigla_maior


async def _radar(r: Resultado, produto: str) -> None:
    try:
        quadros = await redemet.radar_quadros(produto)
    except redemet.Indisponivel as erro:
        r.radar_erro = str(erro)
        return
    if not any(cobre(i, r.lat, r.lon) for q in quadros for i in q.imagens):
        r.radar_erro = 'fora do alcance dos radares'
        return
    valores = await asyncio.gather(*(_dbz_no_quadro(q.imagens, r.lat, r.lon) for q in quadros))
    r.radar_historico = [(q.quando, v[0]) for q, v in zip(quadros, valores)]
    if quadros:
        dbz, sigla = valores[-1]
        r.radar_agora = RadarAgora(dbz, sigla, quadros[-1].quando)


async def _raios(r: Resultado) -> None:
    try:
        quadros = await redemet.raios_quadros()
    except redemet.Indisponivel as erro:
        r.raios_erro = str(erro)
        return
    if quadros:
        r.raios, r.raios_quando = raios_perto(quadros[-1], r.lat, r.lon), quadros[-1].quando


def _vento(r: Resultado) -> None:
    """Perfil vertical: todos os níveis do horário mais recente que tem arquivos."""
    arquivos = vento.listar_arquivos()
    if not arquivos:
        return
    horario = arquivos[0].horario
    for arquivo in sorted((a for a in arquivos if a.horario == horario), key=lambda a: -a.pressao_pa):
        valor = vento_no_ponto(vento.carregar_recortado(arquivo.caminho), r.lat, r.lon)
        if valor:
            r.vento.append((fl_da_pressao(arquivo.pressao_pa), *valor))
    r.vento_referencia = horario


def _aerodromo(r: Resultado) -> None:
    lista = aerodromos.situacao.aerodromos
    if lista:
        a = min(lista, key=lambda a: geo.distancia_km(r.lat, r.lon, a.lat, a.lon))
        r.aerodromo = AerodromoPerto(a, geo.distancia_km(r.lat, r.lon, a.lat, a.lon))


async def sondar(lat: float, lon: float, produto_radar: str, severo_feicoes: list[dict] | None = None,
                 severo_tipo: str = 'area', severo_raio_km: float = 5, severo_validade: str | None = None
                 ) -> Resultado:
    r = Resultado(lat, lon)
    await asyncio.gather(_radar(r, produto_radar), _raios(r))
    await asyncio.to_thread(_vento, r)       # lê arquivos do disco: fora do laço principal
    _aerodromo(r)
    if severo_feicoes is not None:
        r.severo = em_area_severa(severo_feicoes, severo_tipo, severo_raio_km, lat, lon)
        r.severo_validade = severo_validade
    return r
