"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Sonda: o que se sabe sobre um ponto do mapa.

Clique direito no mapa (ou clique com a sonda ligada) e o painel da direita mostra, para
aquele ponto: radar (dBZ agora e nas últimas horas), raios por perto, se está numa área
de tempo severo prevista, o perfil vertical do vento e o aeródromo mais próximo.

Tudo é lido de dados que o servidor já tem (API da REDEMET, arquivos de vento,
aeródromos). O radar é lido da cor do pixel das imagens — a mesma tabela de cores do
filtro de dBZ.

Quem usa: `tela/gaveta.py` (`sondar`, `Resultado`) e `impacto.py` (`raios_perto`, `RaiosPerto`).
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

import numpy as np

import aerodromos
import geo
import radar
import redemet
import vento

RAIOS_DISTANCIAS_KM = (10, 20, 50)
RAIO_CELULAS_NOVAS_KM = 20


# ---------------------------------------------------------------------------
# Contas (funções puras)
# ---------------------------------------------------------------------------

def cobre(imagem: redemet.Imagem, lat: float, lon: float) -> bool:
    """Diz se o ponto (lat, lon), em graus, está dentro da área da imagem."""
    return imagem.sul <= lat <= imagem.norte and imagem.oeste <= lon <= imagem.leste


def pixel(img: np.ndarray, imagem: redemet.Imagem, lat: float, lon: float) -> np.ndarray:
    """Cor RGBA no ponto (a imagem original está em grade lat/lon).

    Parâmetros:
        img: a imagem decodificada (altura, largura, 4).
        imagem: os limites da imagem (norte, sul, leste, oeste), graus.
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.

    Retorna:
        O pixel RGBA mais próximo do ponto (o da borda, se o ponto estiver fora).
    """
    altura, largura = img.shape[:2]
    linha = round((imagem.norte - lat) / (imagem.norte - imagem.sul) * (altura - 1))
    coluna = round((lon - imagem.oeste) / (imagem.leste - imagem.oeste) * (largura - 1))
    return img[min(max(linha, 0), altura - 1), min(max(coluna, 0), largura - 1)]


def dbz_da_cor(rgba: np.ndarray) -> float | None:
    """dBZ de um pixel do radar.

    Parâmetros:
        rgba: o pixel (R, G, B, A).

    Retorna:
        O dBZ arredondado, ou None se não há eco (transparente, fundo ou abaixo de 0 dBZ).
    """
    if rgba[3] == 0:
        return None
    dbz = float(radar.classificar_cores(np.array([rgba[:3]]))[0])
    if dbz == radar.FUNDO or dbz < 0:
        return None
    return round(dbz)


@dataclass(frozen=True)
class RaiosPerto:
    """Células com raios em volta de um ponto (resultado de `raios_perto`).

    Atributos:
        contagem: distância (km) -> quantas células até essa distância.
        novas: células novas (redemet.CELULA_NOVA_MIN) a até RAIO_CELULAS_NOVAS_KM.
        mais_perto: (distância em km, minutos ativa) da célula mais próxima; None = nenhuma
            até a maior distância de RAIOS_DISTANCIAS_KM.
    """
    contagem: dict[int, int]
    novas: int
    mais_perto: tuple[float, int] | None


def raios_perto(quadro: redemet.QuadroRaios, lat: float, lon: float) -> RaiosPerto:
    """Células com raios em volta do ponto: quantas em cada distância, a mais próxima, as novas.

    Parâmetros:
        quadro: o quadro de raios.
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.

    Retorna:
        A contagem (RaiosPerto).
    """
    contagem = dict.fromkeys(RAIOS_DISTANCIAS_KM, 0)
    novas = 0
    mais_perto = None
    limite_km = max(RAIOS_DISTANCIAS_KM)
    for lat_celula, lon_celula, minutos in quadro.celulas:
        # Corte grosseiro antes da conta exata: 1° de lat ≈ 111 km > 50 km; 1,5° de lon cobre
        # 50 km até ~60° de latitude. Evita o haversine nas milhares de células distantes.
        if abs(lat_celula - lat) > 1 or abs(lon_celula - lon) > 1.5:
            continue
        distancia = geo.distancia_km(lat, lon, lat_celula, lon_celula)
        if distancia > limite_km:
            continue
        for raio_km in RAIOS_DISTANCIAS_KM:
            if distancia <= raio_km:
                contagem[raio_km] += 1
        if distancia <= RAIO_CELULAS_NOVAS_KM and minutos < redemet.CELULA_NOVA_MIN:
            novas += 1
        if mais_perto is None or distancia < mais_perto[0]:
            mais_perto = (distancia, minutos)
    return RaiosPerto(contagem, novas, mais_perto)


def _feicao_atinge(geometria: dict, tipo: str, raio_km: float, lat: float, lon: float) -> bool:
    """Diz se uma feição de tempo severo (geometria GeoJSON) atinge o ponto.

    Pontos só contam quando a camada é do tipo 'ponto' (a menos de raio_km); polígonos
    contam se o ponto estiver dentro.
    """
    forma = geometria.get('type')
    if tipo == 'ponto' and forma == 'Point':
        lon_ponto, lat_ponto = geometria['coordinates'][:2]
        return geo.distancia_km(lat, lon, lat_ponto, lon_ponto) <= raio_km
    if forma == 'Polygon':
        return geo.dentro(lat, lon, geometria['coordinates'][0])
    if forma == 'MultiPolygon':
        return any(geo.dentro(lat, lon, poligono[0]) for poligono in geometria['coordinates'])
    return False


def em_area_severa(feicoes: list[dict], tipo: str, raio_km: float, lat: float, lon: float) -> bool:
    """O ponto está numa área (ou a menos de raio_km de um ponto) de tempo severo previsto?

    Parâmetros:
        feicoes: as features GeoJSON da camada de tempo severo.
        tipo: 'ponto' ou 'area' (como a camada é desenhada).
        raio_km: alcance de cada ponto, km (só para o tipo 'ponto').
        lat: latitude do ponto consultado, graus.
        lon: longitude do ponto consultado, graus.

    Retorna:
        True se alguma feição atinge o ponto.
    """
    for feicao in feicoes:
        geometria = feicao.get('geometry') or {}
        if _feicao_atinge(geometria, tipo, raio_km, lat, lon):
            return True
    return False


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

class RadarAgora(NamedTuple):
    """O dBZ no ponto no último quadro de radar.

    Atributos:
        dbz: o maior dBZ entre as estações (None = sem eco).
        estacao: sigla da estação de maior dBZ.
        quando: horário do quadro.
    """
    dbz: int | None
    estacao: str
    quando: datetime


class AerodromoPerto(NamedTuple):
    """O aeródromo mais próximo do ponto.

    Atributos:
        aerodromo: o aeródromo.
        km: distância até o ponto, km.
    """
    aerodromo: aerodromos.Aerodromo
    km: float


@dataclass
class Resultado:
    """Tudo o que a sonda achou sobre um ponto (o painel da direita mostra isto).

    Atributos:
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.
        radar_agora: o dBZ no último quadro (None = sem radar).
        radar_historico: (horário, dBZ ou None) de cada quadro das últimas horas.
        radar_erro: por que não há radar ('' = sem erro).
        raios: células com raios em volta (None = sem quadro).
        raios_quando: horário do quadro de raios.
        raios_erro: por que não há raios ('' = sem erro).
        severo: se está numa área de tempo severo (None = camada desligada ou sem dados).
        severo_validade: validade da previsão de tempo severo.
        vento: (FL, direção em graus, velocidade em kt) de cada nível, de baixo para cima.
        vento_referencia: horário dos arquivos de vento usados.
        aerodromo: o aeródromo mais próximo (None = lista de aeródromos vazia).
    """
    lat: float
    lon: float
    radar_agora: RadarAgora | None = None
    radar_historico: list[tuple[datetime, int | None]] = field(default_factory=list)
    radar_erro: str = ''
    raios: RaiosPerto | None = None
    raios_quando: datetime | None = None
    raios_erro: str = ''
    severo: bool | None = None
    severo_validade: str | None = None
    vento: list[tuple[int, int, int]] = field(default_factory=list)
    vento_referencia: datetime | None = None
    aerodromo: AerodromoPerto | None = None

    @property
    def titulo(self) -> str:
        """As coordenadas do ponto para o título do painel: '23,43°S 46,47°O'."""
        hemisferio_lat = 'S' if self.lat < 0 else 'N'
        hemisferio_lon = 'O' if self.lon < 0 else 'L'
        return f'{abs(self.lat):.2f}°{hemisferio_lat} {abs(self.lon):.2f}°{hemisferio_lon}'.replace('.', ',')


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def _sigla(codigo: str) -> str:
    """A sigla da estação de radar (o próprio código, se a estação for desconhecida)."""
    estacao = radar.POR_CODIGO.get(codigo)
    if estacao is None:
        return codigo
    return estacao.sigla


async def _dbz_no_quadro(imagens: tuple[redemet.Imagem, ...], lat: float, lon: float) -> tuple[int | None, str]:
    """O maior dBZ entre as estações que cobrem o ponto.

    Parâmetros:
        imagens: as imagens das estações de um quadro.
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.

    Retorna:
        (dBZ, sigla da estação). Sem eco: (None, a primeira estação que cobre o ponto, ou '').
    """
    maior = None
    sigla_maior = ''
    for imagem in imagens:
        if not imagem.caminho or not cobre(imagem, lat, lon):
            continue
        img = await redemet.imagem_bruta(imagem)
        if img is None:
            continue
        dbz = dbz_da_cor(pixel(img, imagem, lat, lon))
        sigla = _sigla(imagem.codigo)
        sigla_maior = sigla_maior or sigla             # sem eco: mostra a primeira estação que cobre
        if dbz is not None and (maior is None or dbz > maior):
            maior = dbz
            sigla_maior = sigla
    return maior, sigla_maior


def _algum_radar_cobre(quadros: list[redemet.QuadroRadar], lat: float, lon: float) -> bool:
    """Diz se alguma imagem de algum quadro cobre o ponto."""
    for quadro in quadros:
        for imagem in quadro.imagens:
            if cobre(imagem, lat, lon):
                return True
    return False


async def _radar(resultado: Resultado, produto: str) -> None:
    """Preenche o radar do resultado: o dBZ agora e o histórico (ou o erro).

    Parâmetros:
        resultado: onde guardar (radar_agora, radar_historico, radar_erro).
        produto: produto de radar da API (por exemplo 'maxcappi').
    """
    try:
        quadros = await redemet.radar_quadros(produto)
    except redemet.Indisponivel as erro:
        resultado.radar_erro = str(erro)
        return
    if not _algum_radar_cobre(quadros, resultado.lat, resultado.lon):
        resultado.radar_erro = 'fora do alcance dos radares'
        return
    valores = await asyncio.gather(*(_dbz_no_quadro(quadro.imagens, resultado.lat, resultado.lon)
                                     for quadro in quadros))
    for quadro, (dbz, _sigla_estacao) in zip(quadros, valores):
        resultado.radar_historico.append((quadro.quando, dbz))
    if quadros:
        dbz, sigla = valores[-1]
        resultado.radar_agora = RadarAgora(dbz, sigla, quadros[-1].quando)


async def _raios(resultado: Resultado) -> None:
    """Preenche os raios do resultado com o último quadro (ou o erro)."""
    try:
        quadros = await redemet.raios_quadros()
    except redemet.Indisponivel as erro:
        resultado.raios_erro = str(erro)
        return
    if not quadros:
        return
    ultimo = quadros[-1]
    resultado.raios = raios_perto(ultimo, resultado.lat, resultado.lon)
    resultado.raios_quando = ultimo.quando


def _vento(resultado: Resultado) -> None:
    """Perfil vertical: todos os níveis do horário mais recente que tem arquivos."""
    arquivos = vento.listar_arquivos()
    if not arquivos:
        return
    horario = arquivos[0].horario
    do_horario = [arquivo for arquivo in arquivos if arquivo.horario == horario]
    for arquivo in sorted(do_horario, key=lambda arquivo: -arquivo.pressao_pa):     # de baixo para cima
        valor = vento.valor_no_ponto(vento.carregar_recortado(arquivo.caminho), resultado.lat, resultado.lon)
        if valor:
            resultado.vento.append((vento.fl_da_pressao(arquivo.pressao_pa), *valor))
    resultado.vento_referencia = horario


def _distancia_ate(resultado: Resultado, aerodromo: aerodromos.Aerodromo) -> float:
    """Distância do ponto consultado até o aeródromo, km."""
    return geo.distancia_km(resultado.lat, resultado.lon, aerodromo.lat, aerodromo.lon)


def _aerodromo(resultado: Resultado) -> None:
    """Preenche o aeródromo mais próximo do ponto (nada, se a lista estiver vazia)."""
    lista = aerodromos.situacao.aerodromos
    if not lista:
        return
    mais_perto = min(lista, key=lambda aerodromo: _distancia_ate(resultado, aerodromo))
    resultado.aerodromo = AerodromoPerto(mais_perto, _distancia_ate(resultado, mais_perto))


async def sondar(lat: float, lon: float, produto_radar: str, severo_feicoes: list[dict] | None = None,
                 severo_tipo: str = 'area', severo_raio_km: float = 5, severo_validade: str | None = None
                 ) -> Resultado:
    """Reúne tudo o que se sabe sobre um ponto do mapa.

    Fluxo:
        1. radar e raios ao mesmo tempo (API da REDEMET);
        2. vento, lido dos arquivos numa thread;
        3. aeródromo mais próximo;
        4. tempo severo, se a camada estiver ligada.

    Parâmetros:
        lat: latitude do ponto, graus.
        lon: longitude do ponto, graus.
        produto_radar: produto de radar da API (por exemplo 'maxcappi').
        severo_feicoes: features da camada de tempo severo (None = camada desligada).
        severo_tipo: 'ponto' ou 'area' (em_area_severa).
        severo_raio_km: alcance de cada ponto de tempo severo, km.
        severo_validade: validade da previsão, só repassada ao resultado.

    Retorna:
        O Resultado; as partes que falharam ficam vazias, com o erro no campo `*_erro`.
    """
    resultado = Resultado(lat, lon)
    await asyncio.gather(_radar(resultado, produto_radar), _raios(resultado))
    await asyncio.to_thread(_vento, resultado)       # lê arquivos do disco: fora do laço principal
    _aerodromo(resultado)
    if severo_feicoes is not None:
        resultado.severo = em_area_severa(severo_feicoes, severo_tipo, severo_raio_km, lat, lon)
        resultado.severo_validade = severo_validade
    return resultado
