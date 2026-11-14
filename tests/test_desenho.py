import desenho


def test_balao_escapa_o_texto():
    html = desenho.balao('<img src=x>', [('A → B', None), ('', None), ('ok', '#059669')],
                         nota=('(desatualizado)', '#ef4444'))
    assert html == ('<b>&lt;img src=x&gt;</b><div>A → B</div><div style="color:#059669">ok</div>'
                    '<div style="font-size:10px;color:#ef4444">(desatualizado)</div>')


def test_circulo_fecha_e_tem_o_raio_certo():
    anel = desenho.circulo(0.0, -47.0, 111.32, lados=4)
    assert anel[0] == anel[-1] and len(anel) == 5
    assert anel[0] == [-46.0, 0.0] and anel[1] == [-47.0, 1.0]   # 111,32 km = 1 grau no equador


def test_severo_pontos_viram_um_multipoligono():
    pontos = [{'geometry': {'type': 'Point', 'coordinates': [-45.0, -23.0]}},
              {'geometry': {'type': 'Point', 'coordinates': [-46.0, -22.0]}},
              {'geometry': {'type': 'Polygon', 'coordinates': []}}]
    dados = desenho.severo_geojson(pontos, 'ponto', 5)
    (feicao,) = dados['features']
    assert feicao['geometry']['type'] == 'MultiPolygon' and len(feicao['geometry']['coordinates']) == 2
    assert desenho.severo_geojson([], 'ponto', 5)['features'] == []
    area = {'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
    assert desenho.severo_geojson([area], 'area', 5)['features'][0]['geometry'] == area['geometry']
    estilo = {'cor': '#123456', 'opacidade': 0.3, 'raio_km': 5}
    assert desenho.severo_estilo(estilo, 'ponto')['fillOpacity'] == 0.7
    assert desenho.severo_estilo(estilo, 'area')['fillOpacity'] == 0.3
