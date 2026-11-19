"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Desenho de formas simples no mapa, feito em Python (sem JavaScript próprio).

Cada forma vira uma camada do próprio Leaflet (L.circle, L.polyline, L.geoJSON...),
criada com `mapa.generic_layer(...)`. Aqui ficam só funções puras que montam os dados:
os balões (HTML com o texto escapado), os polígonos e os retângulos.
Usado pelas camadas (pacote camadas/).
"""
import html
import math

import geo


def balao(titulo: str, linhas: list[tuple[str, str | None]] = (), nota: tuple[str, str] | None = None) -> str:
    """Monta o HTML do balão (popup): título em negrito e linhas de texto, com cor opcional.

    Todo texto passa por html.escape: o Leaflet insere o balão como HTML.

    Parâmetros:
        titulo: texto em negrito no topo.
        linhas: (texto, cor CSS ou None); linhas com texto vazio são puladas.
        nota: (texto, cor) em letra pequena no fim, ou None.

    Retorna:
        O HTML do balão.
    """
    partes = [f'<b>{html.escape(titulo)}</b>']
    for texto, cor in linhas:
        if not texto:
            continue
        estilo = f' style="color:{cor}"' if cor else ''
        partes.append(f'<div{estilo}>{html.escape(texto)}</div>')
    if nota:
        texto, cor = nota
        partes.append(f'<div style="font-size:10px;color:{cor}">{html.escape(texto)}</div>')
    return ''.join(partes)


def circulo(lat: float, lon: float, raio_km: float, lados: int = 16) -> list[list[float]]:
    """Aproxima um círculo de `raio_km` em volta do ponto por um polígono.

    Parâmetros:
        lat, lon: centro, em graus.
        raio_km: raio, em km.
        lados: número de lados do polígono.

    Retorna:
        Anel fechado de [lon, lat] (ordem do GeoJSON), com 4 casas decimais.
    """
    graus_lat, graus_lon = geo.graus_por_km(lat, raio_km)
    anel = []
    for i in range(lados):
        angulo = 2 * math.pi * i / lados
        anel.append([round(lon + graus_lon * math.cos(angulo), 4), round(lat + graus_lat * math.sin(angulo), 4)])
    return anel + [anel[0]]


# ---------------------------------------------------------------------------
# Tempo severo
# ---------------------------------------------------------------------------

def severo_geojson(feicoes: list[dict], tipo: str, raio_km: float) -> dict:
    """Monta o GeoJSON do tempo severo. Áreas: as feições como vieram. Pontos: um MultiPolygon de círculos.

    Os pontos viram polígonos aqui porque o L.geoJSON só desenha pontos como círculo
    com uma função JavaScript (pointToLayer); polígonos aceitam um estilo fixo.

    Parâmetros:
        feicoes: feições do WFS (dicts GeoJSON).
        tipo: 'ponto' ou 'area'.
        raio_km: raio dos círculos dos pontos, em km.

    Retorna:
        FeatureCollection; sem features se não há nada a desenhar.
    """
    if tipo != 'ponto':
        features = []
        for feicao in feicoes:
            if feicao.get('geometry'):
                features.append({'type': 'Feature', 'properties': {}, 'geometry': feicao['geometry']})
        return {'type': 'FeatureCollection', 'features': features}
    circulos = []
    for feicao in feicoes:
        geometria = feicao.get('geometry') or {}
        if geometria.get('type') != 'Point':
            continue
        lon, lat = feicao['geometry']['coordinates'][:2]
        circulos.append([circulo(lat, lon, raio_km, lados=12)])
    features = []
    if circulos:
        features = [{'type': 'Feature', 'properties': {},
                     'geometry': {'type': 'MultiPolygon', 'coordinates': circulos}}]
    return {'type': 'FeatureCollection', 'features': features}


def severo_estilo(estilo: dict, tipo: str) -> dict:
    """Monta o estilo Leaflet: pontos sempre bem visíveis (0,7); áreas com a opacidade escolhida.

    Parâmetros:
        estilo: preferência `severo_estilo` ({cor, opacidade, raio_km}).
        tipo: 'ponto' ou 'area'.

    Retorna:
        Opções de estilo do L.geoJSON.
    """
    opacidade = 0.7 if tipo == 'ponto' else estilo['opacidade']
    return {'color': estilo['cor'], 'weight': 1, 'fillColor': estilo['cor'], 'fillOpacity': opacidade}


COBERTURA = {'color': '#e2e8f0', 'weight': 1, 'opacity': 0.9, 'dashArray': '6 6',
             'fill': False, 'interactive': False}
