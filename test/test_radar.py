import asyncio
import io

import httpx
import numpy as np
from PIL import Image

import radar

PEDIDO = {'request': 'GetMap', 'layers': 'redemet:maxcappi_400km'}


def _png(pixels: list[list[tuple]]) -> bytes:
    img = Image.fromarray(np.array(pixels, dtype=np.uint8))
    saida = io.BytesIO()
    img.save(saida, 'PNG')
    return saida.getvalue()


def _alfa(png: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png)).convert('RGBA'))[..., 3]


def test_tabela_sem_cores_invalidas():
    # O bug antigo da tabela gerava componentes como rgb(0,317,-58)
    cores = radar.TABELA[:, 1:]
    assert cores.min() >= 0 and cores.max() <= 255


def test_tabela_bate_com_paleta():
    # 21 dBZ deve ser verde, 41 dBZ amarelo (conferido no visualizador.html corrigido)
    por_dbz = {round(l[0]): tuple(int(c) for c in l[1:]) for l in radar.TABELA}
    r, g, b = por_dbz[21]
    assert g > 200 and r < 50 and b < 50
    r, g, b = por_dbz[41]
    assert r > 200 and g > 150 and b < 50


def test_classifica_cores_da_paleta():
    verde_20, amarelo_35, vermelho_50 = (0, 255, 1), (255, 255, 0), (254, 0, 0)
    dbz = radar.classificar_cores(np.array([verde_20, amarelo_35, vermelho_50]))
    # o valor vem da tabela interpolada, por isso não é exato
    assert np.allclose(dbz, [20, 35, 50], atol=0.5)


def test_cinza_de_fundo_nao_e_eco():
    dbz = radar.classificar_cores(np.array([(30, 30, 30), (200, 200, 200)]))
    assert list(dbz) == [radar.FUNDO, radar.FUNDO]


def test_cinza_medio_e_tratado_como_cemaden():
    # Regra herdada do visualizador.html: cinzas próximos de (128,128,128) são o
    # "cinza CEMADEN" (-5 dBZ) e somem com qualquer limiar > 0. A CONFIRMAR.
    dbz = radar.classificar_cores(np.array([(128, 128, 128), (60, 60, 60)]))
    assert list(dbz) == [-5, -5]


def test_filtro_apaga_so_ecos_abaixo_do_limiar():
    azul_10 = (1, 161, 247, 255)
    verde_20 = (0, 255, 1, 255)
    vermelho_50 = (254, 0, 0, 255)
    fundo = (30, 30, 30, 255)
    transparente = (0, 0, 0, 0)
    png = _png([[azul_10, verde_20, vermelho_50, fundo, transparente]])
    alfa = _alfa(radar.filtrar_png(png, limiar_dbz=30))
    assert list(alfa[0]) == [0, 0, 255, 255, 0]


def test_cor_exata_do_limiar_continua_visivel():
    verde_escuro_30 = (0, 145, 0, 255)
    assert list(_alfa(radar.filtrar_png(_png([[verde_escuro_30]]), limiar_dbz=30))[0]) == [255]
    assert list(_alfa(radar.filtrar_png(_png([[verde_escuro_30]]), limiar_dbz=35))[0]) == [0]


def test_filtro_cql_aceita_so_codigos_conhecidos():
    assert radar.filtro_cql(['al', 'be']) == "radar_code IN ('al','be')"
    assert radar.filtro_cql(["al') OR 1=1 --"]) == 'INCLUDE'
    assert radar.filtro_cql([r.codigo for r in radar.RADARES]) == 'INCLUDE'


def test_proxy_aplica_limiar_e_esconde_parametro(monkeypatch):
    recebidos = {}
    vermelho, azul = (254, 0, 0, 255), (1, 161, 247, 255)

    def geoserver_falso(pedido: httpx.Request) -> httpx.Response:
        recebidos.update(pedido.url.params)
        return httpx.Response(200, content=_png([[vermelho, azul]]), headers={'content-type': 'image/png'})

    monkeypatch.setattr(radar, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(geoserver_falso)))
    status, conteudo, tipo = asyncio.run(radar.buscar_tile({'request': 'GetMap', 'layers': 'redemet:maxcappi_400km', 'dbz_min': '30'}))
    assert status == 200 and tipo == 'image/png'
    assert 'dbz_min' not in recebidos and recebidos['layers'] == 'redemet:maxcappi_400km'
    assert list(_alfa(conteudo)[0]) == [255, 0]
    assert radar.estado.ok is True


def test_proxy_erro_do_wms_vira_502(monkeypatch):
    def geoserver_falso(pedido):
        return httpx.Response(200, text='<ServiceExceptionReport/>', headers={'content-type': 'application/xml'})

    monkeypatch.setattr(radar, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(geoserver_falso)))
    status, _, _ = asyncio.run(radar.buscar_tile(PEDIDO))
    assert status == 502 and radar.estado.ok is False


def test_falha_de_conexao_responde_rapido_depois(monkeypatch):
    tentativas = []

    def geoserver_fora(pedido):
        tentativas.append(1)
        raise httpx.ConnectTimeout('sem rota')

    monkeypatch.setattr(radar, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(geoserver_fora)))
    monkeypatch.setattr(radar.estado, 'indisponivel_ate', 0.0)
    assert asyncio.run(radar.buscar_tile(PEDIDO))[0] == 502
    assert asyncio.run(radar.buscar_tile(PEDIDO))[0] == 503   # não tentou de novo
    assert len(tentativas) == 1
    monkeypatch.setattr(radar.estado, 'indisponivel_ate', 0.0)               # passou o tempo de espera
    asyncio.run(radar.buscar_tile(PEDIDO))
    assert len(tentativas) == 2


def test_alcancavel():
    async def caso():
        servidor = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
        porta = servidor.sockets[0].getsockname()[1]
        async with servidor:
            assert await radar.alcancavel(f'http://127.0.0.1:{porta}/geoserver/wms')
        assert not await radar.alcancavel(f'http://127.0.0.1:{porta}/geoserver/wms', espera_s=1)
    asyncio.run(caso())


def test_proxy_so_repassa_camadas_do_mapa():
    ok = {'request': 'GetMap', 'layers': 'redemet:lightning_data'}
    assert radar.pedido_permitido(ok)
    assert radar.pedido_permitido({'REQUEST': 'GetMap', 'LAYERS': 'redemet:cappi_250km'})
    assert not radar.pedido_permitido({'request': 'GetMap', 'layers': 'redemet:usuarios'})
    assert not radar.pedido_permitido({'request': 'GetMap', 'layers': 'redemet:lightning_data,redemet:x'})
    assert not radar.pedido_permitido({'request': 'GetFeatureInfo', 'layers': 'redemet:lightning_data'})
    assert radar.pedido_permitido({'REQUEST': 'GetLegendGraphic', 'LAYER': 'redemet:lightning_data'})
    assert not radar.pedido_permitido({'REQUEST': 'GetLegendGraphic', 'LAYER': 'redemet:outra'})
    assert asyncio.run(radar.buscar_tile({'request': 'GetCapabilities'}))[0] == 400


def test_proxy_aceita_os_pedidos_que_o_mapa_faz():
    import raios
    import wifs
    tile = {'service': 'WMS', 'request': 'GetMap', 'version': '1.1.1', 'styles': '', 'format': 'image/png',
            'transparent': 'true', 'width': '256', 'height': '256', 'srs': 'EPSG:3857',
            'bbox': '-5009377.085697311,-2504688.542848654,-3757032.814272985,-1252344.271424327'}
    radar_ = {**tile, 'layers': 'redemet:cappi_250km', 'TIME': radar.janela_de_tempo(), 'ELEVATION': '3',
              'CQL_FILTER': radar.filtro_cql(['al', 'be']), 'dbz_min': '20'}
    assert radar.limpar_parametros(radar_) == radar_
    assert radar.limpar_parametros({**radar_, 'CQL_FILTER': 'INCLUDE'})
    raios_ = {**tile, 'layers': 'redemet:lightning_data',
              **raios.parametros(raios.agora(), 'tempo', 10)}
    assert radar.limpar_parametros(raios_) == raios_
    wifs_ = {**tile, **{k: str(v).lower() if isinstance(v, bool) else str(v)
                        for k, v in wifs.opcoes_wms('temperatura', 240).items()
                        if k not in ('opacity', 'zIndex', 'attribution')}}
    assert radar.limpar_parametros(wifs_) == wifs_
    legenda = dict(p.split('=', 1) for p in wifs.url_legenda('turbulencia').split('?', 1)[1].split('&'))
    assert radar.limpar_parametros(legenda) == legenda


def test_proxy_recusa_valores_fora_do_formato_e_descarta_o_resto():
    base = {'request': 'GetMap', 'layers': 'redemet:maxcappi_400km', 'width': '256', 'height': '256'}
    assert radar.limpar_parametros({**base, 'SLD_BODY': '<StyledLayerDescriptor/>'}) == base
    assert radar.limpar_parametros({**base, 'width': '8000'}) is None          # imagem enorme
    assert radar.limpar_parametros({**base, 'height': '0'}) is None
    assert radar.limpar_parametros({**base, 'dbz_min': 'abc'}) is None         # antes: erro 500
    assert radar.limpar_parametros({**base, 'dbz_min': '99'}) is None
    assert radar.limpar_parametros({**base, 'format': 'application/openlayers'}) is None
    assert radar.limpar_parametros({**base, 'CQL_FILTER': "radar_code IN ('al') OR 1=1"}) is None
    assert radar.limpar_parametros({**base, 'CQL_FILTER': "radar_code IN ('zz')"}) is None
    assert radar.limpar_parametros({**base, 'env': 'radius:1;x:${java}'}) is None
    assert asyncio.run(radar.buscar_tile({**base, 'width': '8000'}))[0] == 400


def test_legenda_do_radar():
    assert radar.LEGENDA[0] == (0, '#75ffff')
    assert radar.LEGENDA[-1] == (75, '#ffffff')
    assert [d for d, _ in radar.LEGENDA] == list(range(0, 80, 5))
