"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Contas de geografia usadas por vários módulos (funções puras, sem rede nem tela).

Por que um módulo só para isso: a mesma conta aparecia copiada em sonda.py, impacto.py,
desenho.py, severo.py e espaco.py, às vezes com pequenas diferenças. Uma correção feita
numa cópia não chegava às outras. Aqui cada conta existe uma vez e tem teste.

Usado por quase todos os módulos (impacto, previsao, sonda, desenho, severo, espaco...):
os nomes públicos daqui não devem mudar.

Convenções (as mesmas do resto do sistema):
  - pontos do Leaflet e das nossas listas: [lat, lon];
  - anéis e coordenadas GeoJSON: [lon, lat] (é a ordem do padrão GeoJSON);
  - distâncias em km; ângulos em graus.

Precisão: as contas tratam a Terra como esfera (haversine) e, para áreas pequenas
(até algumas dezenas de km), aproximam o círculo por uma elipse em graus. O erro é bem
menor que a resolução dos dados (pixel de radar ~1 km, célula de raios 0,1° ≈ 11 km).
"""
import math

import numpy as np

RAIO_TERRA_KM = 6371.0
KM_POR_GRAU = 111.32     # comprimento de 1° de latitude (e de longitude no equador)


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula a distância entre dois pontos sobre a esfera (fórmula de haversine).

    Parâmetros:
        lat1, lon1: primeiro ponto, em graus.
        lat2, lon2: segundo ponto, em graus.

    Retorna:
        A distância, em km.
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    seno_meia_dlat = math.sin((lat2_rad - lat1_rad) / 2)
    seno_meia_dlon = math.sin(math.radians(lon2 - lon1) / 2)
    haversine = seno_meia_dlat ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * seno_meia_dlon ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(haversine))


def graus_por_km(lat: float, km: float) -> tuple[float, float]:
    """Converte uma distância em km para graus de latitude e de longitude.

    A longitude "encolhe" com o cosseno da latitude. O mínimo de 0,01 no cosseno evita
    divisão por zero perto dos polos (que o sistema nunca usa, mas não deve quebrar).

    Parâmetros:
        lat: latitude onde a conta vale, em graus.
        km: distância, em km.

    Retorna:
        (Δlat, Δlon) em graus.
    """
    delta_lat = km / KM_POR_GRAU
    delta_lon = km / (KM_POR_GRAU * max(math.cos(math.radians(lat)), 0.01))
    return delta_lat, delta_lon


def dentro(lat: float, lon: float, anel: list) -> bool:
    """Diz se o ponto está dentro do polígono.

    Algoritmo do raio: conta quantas arestas uma semirreta saindo do ponto (para leste)
    cruza; ímpar = dentro. Não precisa de biblioteca (shapely etc.) e é rápido o bastante
    para as poucas centenas de polígonos de TMA e de tempo severo.

    Parâmetros:
        lat, lon: o ponto, em graus.
        anel: vértices do polígono em [lon, lat] (ordem GeoJSON).

    Retorna:
        True se o ponto está dentro.
    """
    dentro_do_anel = False
    anterior = len(anel) - 1            # cada aresta vai do vértice anterior ao atual
    for atual in range(len(anel)):
        lon_atual, lat_atual = anel[atual][0], anel[atual][1]
        lon_anterior, lat_anterior = anel[anterior][0], anel[anterior][1]
        aresta_passa_pela_lat = (lat_atual > lat) != (lat_anterior > lat)
        if aresta_passa_pela_lat:
            lon_do_cruzamento = (lon_anterior - lon_atual) * (lat - lat_atual) / (lat_anterior - lat_atual) + lon_atual
            if lon < lon_do_cruzamento:
                dentro_do_anel = not dentro_do_anel
        anterior = atual
    return dentro_do_anel


def dentro_vetor(lats: np.ndarray, lons: np.ndarray, anel: list) -> np.ndarray:
    """Faz o mesmo que `dentro`, para muitos pontos de uma vez (numpy).

    Usado no painel de impacto, que testa milhares de pixels de radar contra cada TMA:
    o laço passa pelas arestas (poucas) e não pelos pontos (muitos).

    Parâmetros:
        lats, lons: arrays numpy de mesmo formato com os pontos, em graus.
        anel: vértices do polígono em [lon, lat] (ordem GeoJSON).

    Retorna:
        Array de bool do formato de `lats`: True onde o ponto está dentro.
    """
    lons_vertices = np.array([vertice[0] for vertice in anel], float)
    lats_vertices = np.array([vertice[1] for vertice in anel], float)
    dentro_do_anel = np.zeros(lats.shape, bool)
    for atual in range(len(anel)):
        anterior = atual - 1            # em atual = 0, o índice -1 fecha o anel com o último vértice
        lon_atual = lons_vertices[atual]
        lat_atual = lats_vertices[atual]
        lon_anterior = lons_vertices[anterior]
        lat_anterior = lats_vertices[anterior]
        if lat_atual == lat_anterior:   # aresta horizontal: nunca é cruzada (e dividiria por zero)
            continue
        aresta_passa_pela_lat = (lat_atual > lats) != (lat_anterior > lats)
        lons_do_cruzamento = (lon_anterior - lon_atual) * (lats - lat_atual) / (lat_anterior - lat_atual) + lon_atual
        dentro_do_anel ^= aresta_passa_pela_lat & (lons < lons_do_cruzamento)
    return dentro_do_anel


def deslocar(lat: float, lon: float, rumo: float, km):
    """Calcula o ponto a `km` de distância no `rumo`, partindo de (lat, lon).

    Conta no plano local (como `graus_por_km`): erro desprezível até algumas centenas de km,
    que é o alcance da previsão de conflitos (30 min de voo).

    Parâmetros:
        lat, lon: ponto de partida, em graus.
        rumo: em graus, 0 = norte, sentido horário.
        km: distância, em km; pode ser um número ou um array numpy.

    Retorna:
        (lat, lon) do ponto de chegada, em graus; arrays numpy se `km` for um array.
    """
    rumo_rad = math.radians(rumo)
    delta_lat = np.multiply(km, math.cos(rumo_rad)) / KM_POR_GRAU
    delta_lon = np.multiply(km, math.sin(rumo_rad)) / (KM_POR_GRAU * max(math.cos(math.radians(lat)), 0.01))
    return lat + delta_lat, lon + delta_lon


# ---------------------------------------------------------------------------
# Grade de 0,1° (a das células de raios da REDEMET): impacto e previsão usam a mesma
# ---------------------------------------------------------------------------

GRADE = 0.1                     # graus
Chave = tuple[int, int]         # (floor(lat*10), floor(lon*10))


def chave_celula(lat: float, lon: float) -> Chave:
    """Devolve a célula de 0,1° que contém o ponto.

    Parâmetros:
        lat, lon: o ponto, em graus.

    Retorna:
        Chave (floor(lat*10), floor(lon*10)).
    """
    return math.floor(lat * 10), math.floor(lon * 10)


def centro(chave: Chave) -> tuple[float, float]:
    """Devolve o centro da célula.

    Parâmetros:
        chave: a célula, como devolvida por `chave_celula`.

    Retorna:
        (lat, lon) do centro, em graus.
    """
    return (chave[0] + 0.5) * GRADE, (chave[1] + 0.5) * GRADE


def vizinhas(chave: Chave) -> list[Chave]:
    """Lista a célula e as 8 em volta (inclusive na diagonal).

    Parâmetros:
        chave: a célula central.

    Retorna:
        9 chaves: de sul para norte e, em cada fileira, de oeste para leste.
    """
    indice_lat, indice_lon = chave
    celulas = []
    for passo_lat in (-1, 0, 1):
        for passo_lon in (-1, 0, 1):
            celulas.append((indice_lat + passo_lat, indice_lon + passo_lon))
    return celulas


def mercator_y(lat: float) -> float:
    """Calcula a coordenada vertical da projeção de Mercator (a do mapa).

    Parâmetros:
        lat: latitude, em graus.

    Retorna:
        y de Mercator, em radianos.
    """
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def lat_de_mercator(y: float) -> float:
    """Calcula a latitude de uma coordenada vertical de Mercator (o inverso de mercator_y).

    Parâmetros:
        y: y de Mercator, em radianos.

    Retorna:
        A latitude, em graus.
    """
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def limites(pontos: list[list[float]]) -> list[list[float]] | None:
    """Calcula o retângulo que envolve os pontos.

    Parâmetros:
        pontos: [[lat, lon], ...].

    Retorna:
        [[sul, oeste], [norte, leste]] (formato do Leaflet); None se a lista for vazia.
    """
    if not pontos:
        return None
    lats = [ponto[0] for ponto in pontos]
    lons = [ponto[1] for ponto in pontos]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def arredondar(coordenadas, casas: int):
    """Arredonda coordenadas GeoJSON aninhadas (ponto, linha, polígono, multipolígono...).

    Menos casas = arquivo menor para o navegador: 4 casas ≈ 10 m, 5 casas ≈ 1 m.

    Parâmetros:
        coordenadas: um número ou listas aninhadas de números.
        casas: casas decimais.

    Retorna:
        A mesma estrutura (em listas novas), com os números arredondados.
    """
    if isinstance(coordenadas, (int, float)):
        return round(coordenadas, casas)
    return [arredondar(item, casas) for item in coordenadas]
