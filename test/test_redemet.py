import asyncio
from datetime import UTC, datetime

import httpx
import numpy as np

import config
import redemet

E = config.REDEMET_ESTATICO_URL


def _estacao(codigo, path, data):
    return {'localidade': codigo, 'lat_min': '-4.96', 'lat_max': '2.19', 'lon_min': '-52.06',
            'lon_max': '-44.90', 'path': path, 'data': data}


def test_converter_radar_pega_o_quadro_mais_novo():
    dados = {'radar': [
        [_estacao('be', f'{E}/radar/2026/11/13/be/maxcappi/maps/velho.png', '2026-11-13 15:58:07')],
        [_estacao('BE', f'{E}/radar/2026/11/13/be/maxcappi/maps/2026-11-13--16:08:07.png',
                  '2026-11-13 16:08:07'),
         _estacao('al', None, None),
         _estacao('xx', 'https://outro.site/radar/x.png', '2026-11-13 16:08:07'),
         {'localidade': 'zz'}],                               # sem coordenadas: ignorado
    ]}
    be, al, xx = redemet.converter_radar(dados)
    assert be.codigo == 'be' and be.caminho == 'radar/2026/11/13/be/maxcappi/maps/2026-11-13--16:08:07.png'
    assert be.quando == datetime(2026, 11, 13, 16, 8, 7, tzinfo=UTC)
    assert be.limites == [[-4.96, -52.06], [2.19, -44.90]]
    assert al.caminho == '' and al.quando is None
    assert xx.caminho == ''                                   # imagem de outro servidor: recusada
    assert redemet.converter_radar({}) == []


def test_converter_satelite():
    dados = {'lat_lon': {'lon_min': '-100', 'lon_max': '-25.24', 'lat_min': '-56', 'lat_max': '12.52'},
             'satelite': [{'data': '2026-11-13 15:50:00', 'path': f'{E}/satelite/a.png'},
                          {'data': '2026-11-13 16:00:00', 'path': f'{E}/satelite/2026/b.png'}]}
    img = redemet.converter_satelite(dados)
    assert img.caminho == 'satelite/2026/b.png' and img.quando.hour == 16
    assert (img.sul, img.norte, img.oeste, img.leste) == (-56, 12.52, -100, -25.24)
    assert redemet.converter_satelite({'satelite': []}) is None


def test_url_e_caminhos():
    img = redemet.Imagem('radar/2026/11/13/be/maxcappi/maps/2026-11-13--16:08:07.png', -5, 2, -52, -45, None)
    assert img.url() == ('/redemet/imagem?c=radar%2F2026%2F11%2F13%2Fbe%2Fmaxcappi%2Fmaps%2F'
                         '2026-11-13--16%3A08%3A07.png')                  # sem a área: o servidor sabe
    assert img.url(20).endswith('&dbz=20')
    assert redemet.caminho_valido(img.caminho)
    for ruim in ('radar/../etc/passwd.png', '/radar/a.png', 'radar/a.jpg', 'outra/a.png', 'radar/a b.png'):
        assert not redemet.caminho_valido(ruim), ruim


def test_reprojetar_para_mercator():
    # 121 linhas de 60°N a 60°S; cada linha guarda a própria latitude
    lats = np.linspace(60, -60, 121)
    img = np.repeat(lats[:, None, None], 4, axis=2).repeat(3, axis=1)
    saida = redemet.reprojetar(img, -60, 60)
    assert saida.shape == img.shape
    assert saida[0, 0, 0] == 60 and saida[-1, 0, 0] == -60 and saida[60, 0, 0] == 0
    # Em Mercator as altas latitudes se esticam: a linha a 1/4 da altura já é ~35°N (não 30°N)
    assert saida[30, 0, 0] == 35


def test_buscar_imagem_recusa_pedidos_estranhos(monkeypatch):
    monkeypatch.setattr(redemet, '_conhecidas', {})
    for args in [('../segredo.png',), ('radar/a.png', 99)]:
        status, _, _ = asyncio.run(redemet.buscar_imagem(*args))
        assert status == 400, args
    assert asyncio.run(redemet.buscar_imagem('radar/a.png'))[0] == 404     # a API não listou
    redemet._conhecer([redemet.Imagem('radar/b.png', 2, -5, 0, 1, None)])   # área impossível
    assert asyncio.run(redemet.buscar_imagem('radar/b.png'))[0] == 502


def test_buscar_imagem_usa_a_area_que_a_api_deu(monkeypatch):
    monkeypatch.setattr(redemet, '_conhecidas', {})
    monkeypatch.setattr(redemet, '_cache_img', {})
    pedidos = []

    async def png(caminho):
        return b'png'

    def preparar(png_, caminho, sul, norte, dbz):
        pedidos.append((caminho, sul, norte, dbz))
        return b'pronta', 'image/png'

    monkeypatch.setattr(redemet, 'png_original', png)
    monkeypatch.setattr(redemet, '_preparar', preparar)
    redemet._conhecer([redemet.Imagem('radar/a.png', -5, 2, -52, -45, None)])
    assert asyncio.run(redemet.buscar_imagem('radar/a.png', 20)) == (200, b'pronta', 'image/png')
    assert asyncio.run(redemet.buscar_imagem('radar/a.png', 20))[0] == 200   # do cache
    assert pedidos == [('radar/a.png', -5, 2, 20)]


def test_consulta_sem_produto_devolve_dict_vazio(monkeypatch):
    # De madrugada a API respondeu {"status": true, "data": []}: virava "'list' object has no attribute 'get'"
    transporte = httpx.MockTransport(lambda pedido: httpx.Response(200, json={'status': True, 'data': []}))
    monkeypatch.setattr(redemet, '_cliente', httpx.AsyncClient(transport=transporte))
    monkeypatch.setattr(redemet, '_cache_api', {})
    assert asyncio.run(redemet.satelite('realcada')) is None
