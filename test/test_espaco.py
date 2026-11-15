import json

import espaco


def test_camadas_de_espaco():
    assert espaco.POR_CHAVE['cartas_altas'].camadas_wms.count('ICA:ENRC_H') == 9
    assert [c.chave for c in espaco.CAMADAS if c.estilizavel] == ['aerovias_altas', 'aerovias_baixas']


def _geojson(tmp_path, nome='aerovias_altas'):
    dados = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': 'a.1', 'properties': {'ident': 'UZ6', 'nivel_inf': 245, 'obs': None, 'bbox': [1]},
         'geometry': {'type': 'LineString', 'coordinates': [[-47.123456789, -15.987654321], [-43.2, -22.9]]}},
        {'type': 'Feature', 'properties': {'ident': 'sem geometria'}, 'geometry': None},
    ]}
    caminho = tmp_path / f'{nome}.geojson'
    caminho.write_text(json.dumps(dados))
    return caminho


def test_arquivo_local_e_preparo(tmp_path):
    assert espaco.arquivo_local('aerovias_altas', tmp_path) is None
    caminho = _geojson(tmp_path)
    assert espaco.arquivo_local('aerovias_altas', tmp_path) == caminho

    saida = json.loads(espaco.geojson_preparado(caminho))
    assert len(saida['features']) == 1                                   # feição sem geometria sai
    f = saida['features'][0]
    assert f['geometry']['coordinates'][0] == [-47.12346, -15.98765]     # 5 casas
    assert f['properties'] == {'ident': 'UZ6', 'nivel_inf': 245}         # nulos e bbox saem


def test_preparo_usa_cache_ate_o_arquivo_mudar(tmp_path):
    import os
    caminho = _geojson(tmp_path)
    primeiro = espaco.geojson_preparado(caminho)
    assert espaco.geojson_preparado(caminho) is primeiro
    caminho.write_text('{"type":"FeatureCollection","features":[]}')
    os.utime(caminho, (1, 1))                                            # muda o mtime
    assert json.loads(espaco.geojson_preparado(caminho))['features'] == []


# Segmento real do WFS do DECEA (vw_aerovia_alta_v2, AIRAC 2026-10-29)
UM402 = {"gid": 1840, "effectived": "2025-12-25Z", "code_type": "RNAV", "level": "UPPER",
         "upper_limit": 999.0, "uom_upper_limit": "FL", "lower_limit": 245.0, "uom_lower_limit": "FL",
         "initial_magnetic_track": 6.0, "reverse_magnetic_track": 186.0, "text_remark": None,
         "minimumn_level": None, "text_designator": "UM402", "direction": "BOTH",
         "segment_length": None, "segment_length_uom": "258.900289",
         "from_fix_type": "WAYPOINT", "from_fix_ident": "KIMIK", "to_fix_type": "VOR", "to_fix_ident": "VAS",
         "emenda": "2026-10-29Z", "emenda_futura": "2026-11-26Z"}


def test_ficha_da_aerovia():
    f = espaco.ficha_aerovia(UM402)
    assert f['_titulo'] == 'UM402  KIMIK → VAS'
    assert f['Limites'] == 'FL245 a ilimitado'
    assert f['Distância'] == '258.9 NM'          # valor lido do campo trocado na base do DECEA
    assert f['Rumo mag.'] == '006° / volta 186°'
    assert 'Observação' not in f and 'Nível mínimo' not in f


def test_validade_airac(tmp_path):
    caminho = tmp_path / 'aerovias_altas.geojson'
    caminho.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': UM402,
         'geometry': {'type': 'LineString', 'coordinates': [[-56.9, -29.5], [-57.0, -28.7]]}}]}))
    assert espaco.validade(caminho) == ('2026-10-29', '2026-11-26')
    props = json.loads(espaco.geojson_preparado(caminho))['features'][0]['properties']
    assert props['_titulo'].startswith('UM402')
    assert espaco.texto_fonte(caminho, '2026-11-20') == \
        ('fonte: arquivo local, AIRAC de 29/10, válido até 26/11', False)
    assert espaco.texto_fonte(caminho, '2026-11-26') == \
        ('fonte: arquivo local, AIRAC de 29/10 · VENCIDO em 26/11, baixe de novo', True)
    assert espaco.texto_fonte(None, '2026-11-20') == ('fonte: imagens do DECEA', False)


# Itens reais do WFS do DECEA (AIRAC 2026-10-29)
TMA_SBWL = {"typ": "TMA", "ident": "SBWL", "nam": "Ilhéus", "codedistve": "STD", "uplimituni": "FL",
            "codedistv1": "MSL", "lowerlimit": "FT", "upperlimit": 195.0, "lowerlimi1": 3500.0,
            "relatedfir": "SBRE", "airspace_p": 1039.0, "effectived": "2023-05-18Z"}
FIR_SBRE = {"typ": "FIR", "ident": "SBRE", "nam": "Recife", "codedistve": "STD", "uplimituni": "FL",
            "codedistv1": "SFC", "lowerlimit": "FT", "upperlimit": 999.0, "lowerlimi1": 0.0, "airspace_p": 1604.0}


def test_ficha_de_espaco_aereo():
    tma = espaco.ficha_espaco(TMA_SBWL)
    assert tma['_titulo'] == 'TMA SBWL – Ilhéus'
    assert tma['Limites'] == '3500 ft MSL a FL195'
    assert tma['FIR'] == 'SBRE'
    assert espaco.ficha_espaco(FIR_SBRE)['Limites'] == 'SFC a ilimitado'
    assert espaco._propriedades('tma', TMA_SBWL)['_titulo'].startswith('TMA SBWL')


def test_areas_proibidas_restritas_perigosas():
    assert espaco.POR_CHAVE['areas_restritas'].camadas_wms == 'ICA:eac_r'
    assert espaco.POR_CHAVE['areas_proibidas'].preenchimento > espaco.POR_CHAVE['tma'].preenchimento
    area = {'airspace_p': 1, 'typ': 'R', 'ident': 'SBR123', 'nam': 'TESTE',
            'lowerlimi1': 0, 'lowerlimit': 'FT', 'codedistv1': 'SFC',
            'upperlimit': 5000, 'uplimituni': 'FT', 'codedistve': 'MSL', 'codewrkhr': 'H24'}
    ficha = espaco._propriedades('areas_restritas', area)
    assert ficha['_titulo'] == 'Área restrita SBR123 – TESTE'
    assert ficha['Limites'] == 'SFC a 5000 ft MSL'
    assert ficha['Horário'] == 'H24'


SBP433 = {'gid': 36, 'pk': 37.0, 'efetivacao': '2025-10-30Z', 'tipo': 'P', 'id': 'SBP433', 'nome': 'BARUERI',
          'uom_ulimit': 'FT', 'uom_llimit': 'FT', 'upperlimit': 3400.0, 'lowerlimit': 0.0, 'perigo': None,
          'observacao': None, 'designador': None, 'fir': 'SBCW'}


def test_ficha_de_area_real():
    ficha = espaco._propriedades('areas_proibidas', SBP433)
    assert ficha == {'_titulo': 'Área proibida SBP433 – BARUERI', 'Limites': 'GND a 3400 ft',
                     'FIR': 'SBCW', 'Vigente desde': '2025-10-30'}
    restrita = dict(SBP433, tipo='R', uom_llimit='FL', lowerlimit=50, uom_ulimit='FL', upperlimit=999)
    assert espaco.ficha_area(restrita)['Limites'] == 'FL050 a ilimitado'
