import asyncio
from datetime import UTC, date, datetime

import httpx
import pytest

import raios
import rotas
import severo

FIM = datetime(2026, 11, 13, 14, 30, tzinfo=UTC)


# --- Raios -------------------------------------------------------------------------

def test_parametros_dos_raios():
    p = raios.parametros(FIM, 'tempo', 10)
    assert p['TIME'] == '2026-11-13T13:30:00Z/2026-11-13T14:30:00Z'
    assert p['styles'] == 'redemet:lightning_time_classification'
    assert p['env'] == ('radius:10000;t0:20261113T143000Z;t15:20261113T141500Z;t30:20261113T140000Z;'
                        't45:20261113T134500Z;t60:20261113T133000Z')
    assert raios.parametros(FIM, 'intensidade', 3)['styles'] == 'redemet:lightning_by_intensity'
    assert raios.parametros(FIM, 'outra', 3)['styles'] == 'redemet:lightning_time_classification'


def test_janela_dos_raios_anda_e_volta_ao_vivo():
    antes = raios.deslocar(None, -15, referencia=FIM)
    assert antes == datetime(2026, 11, 13, 14, 15, tzinfo=UTC)
    assert raios.deslocar(antes, -15, referencia=FIM).minute == 0
    assert raios.deslocar(antes, 15, referencia=FIM) is None      # chegou em "agora": ao vivo
    assert raios.deslocar(None, 15, referencia=FIM) is None       # não vai para o futuro
    assert raios.rotulo_janela(FIM) == '13:30 – 14:30 UTC · 13/11'


# --- Tempo severo ----------------------------------------------------------------------

def _item(origem, res, tipo, rodada, validade):
    return {'origem': origem, 'resolucao_km': res, 'tipo': tipo, 'rodada': rodada, 'validade': validade}


CATALOGO = [
    _item('icea', 6, 'area', '2026-11-13T00:00:00Z', '2026-11-13T06:00:00Z'),
    _item('icea', 6, 'area', '2026-11-13T00:00:00Z', '2026-11-13T03:00:00Z'),
    _item('icea', 6, 'area', '2026-11-12T12:00:00Z', '2026-11-13T00:00:00Z'),
    _item('wrf_3', 3.0, 'ponto', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z'),
    _item('wrf_3', 3.0, 'area', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z'),
]


def test_opcoes_em_cascata():
    assert severo.modelos(CATALOGO) == ['icea', 'wrf_3']
    assert severo.resolucoes(CATALOGO, 'wrf_3') == ['3']          # 3.0 vira '3'
    assert severo.tipos(CATALOGO, 'wrf_3', '3') == ['area', 'ponto']
    assert severo.rodadas(CATALOGO, 'icea', '6', 'area') == ['2026-11-13T00:00:00Z', '2026-11-12T12:00:00Z']
    assert severo.validades(CATALOGO, 'icea', '6', 'area', '2026-11-13T00:00:00Z') == \
        ['2026-11-13T03:00:00Z', '2026-11-13T06:00:00Z']
    assert severo.mais_recente(CATALOGO)['origem'] == 'wrf_3'
    assert severo.mais_recente([]) is None


def test_filtro_cql_recusa_valores_estranhos():
    cql = severo.filtro_cql('WRF_3', '3', 'ponto', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z')
    assert cql == ("strToLowerCase(origem)='wrf_3' AND resolucao_km=3 AND strToLowerCase(tipo)='ponto' "
                   "AND rodada='2026-11-13T00:00:00Z' AND validade='2026-11-13T09:00:00Z'")
    with pytest.raises(ValueError):
        severo.filtro_cql("x' OR '1'='1", '3', 'area', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z')
    with pytest.raises(ValueError):
        severo.filtro_cql('icea', '3', 'area', "2026' OR 1=1", '2026-11-13T09:00:00Z')
    with pytest.raises(ValueError):
        severo.filtro_cql('icea', '3', 'linha', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z')
    with pytest.raises(ValueError):
        severo.filtro_cql('icea', '3 OR 1=1', 'area', '2026-11-13T00:00:00Z', '2026-11-13T09:00:00Z')


def test_situacao_e_rotulo():
    agora = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)
    assert severo.situacao('2026-11-13T03:00:00Z', agora)[0] == 'expirada'
    assert severo.situacao('2026-11-13T06:00:00Z', agora)[0] == 'próxima (< 6 h)'
    assert severo.situacao('2026-11-13T12:00:00Z', agora)[0] == 'válida'
    assert severo.rotulo('2026-11-13T06:00:00Z') == '13/11 06:00Z'


def test_feicoes_paginadas_e_limites(monkeypatch):
    pedidos = []

    def wfs(pedido):
        pedidos.append(dict(pedido.url.params))
        inicio = int(pedido.url.params['startIndex'])
        lote = [] if inicio else [
            {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-45.123456, -23.5]}},
            {'type': 'Feature', 'geometry': {'type': 'Polygon',
                                             'coordinates': [[[-50, -20], [-49, -20], [-49, -19], [-50, -20]]]}},
            {'type': 'Feature', 'geometry': None},
        ]
        return httpx.Response(200, json={'features': lote, 'numberMatched': 3})

    monkeypatch.setattr(severo, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(wfs)))
    monkeypatch.setattr(severo.radar, '_indisponivel_ate', 0.0)
    lista = asyncio.run(severo.feicoes("tipo='area'"))
    assert len(lista) == 2 and len(pedidos) == 1
    assert pedidos[0]['CQL_FILTER'] == "tipo='area'" and pedidos[0]['typeName'] == 'redemet:tempo_severo'
    assert lista[0]['geometry']['coordinates'] == [-45.1235, -23.5]
    assert severo.limites(lista) == [[-23.5, -50], [-19, -45.1235]]
    assert severo.limites([]) is None


def test_geoserver_fora_nao_consulta(monkeypatch):
    monkeypatch.setattr(severo.radar, '_indisponivel_ate', float('inf'))
    with pytest.raises(severo.Indisponivel):
        asyncio.run(severo.catalogo(forcar=True))


# --- Rotas ---------------------------------------------------------------------------

def _pos(lat, lon, quando):
    return {'latitude': lat, 'longitude': lon, 'received_at': quando, 'flight_level': 350}


TRACKER = {'callsign': 'GLO1748', 'segments': [
    {'adep': 'SBFL', 'ades': 'SBBR', 'positions': [_pos(-15.0, -47.0, '2026-11-13T12:00:00Z'),
                                                   _pos(-15.5, -47.5, '2026-11-13T11:00:00Z')]},
    {'adep': 'SBBR', 'ades': 'SBFL', 'positions': [
        _pos(-16.0, -48.0, '2026-11-13T09:00:00Z'), _pos(-16.001, -48.001, '2026-11-13T09:01:00Z'),
        _pos(-17.0, -49.0, '2026-11-13T09:30:00Z'), _pos(-18.0, -50.0, '2026-11-13T10:00:00Z')]},
    {'adep': 'SBXX', 'ades': 'SBYY', 'positions': []},
]}


def test_rota_um_trecho_por_voo_em_ordem():
    rota = rotas.converter(TRACKER, 'GLO1748')
    assert [(t.origem, t.destino) for t in rota.trechos] == [('SBBR', 'SBFL'), ('SBFL', 'SBBR')]
    assert rota.trechos[0].pontos == [[-16.0, -48.0], [-17.0, -49.0], [-18.0, -50.0]]   # ponto colado some
    assert rota.trechos[1].pontos == [[-15.5, -47.5], [-15.0, -47.0]]       # ordenado pelo horário
    assert rotas.converter({'segments': []}, 'X1') is None


def test_rota_indicativo_invalido_nao_consulta(monkeypatch):
    monkeypatch.setattr(rotas, '_cliente', None)   # qualquer consulta quebraria
    for ruim in ('UNK_386', '../admin', 'A', ''):
        with pytest.raises(ValueError):
            asyncio.run(rotas.buscar(ruim))


def test_rota_tenta_ontem_quando_hoje_nao_tem(monkeypatch):
    urls = []

    def tracker(pedido):
        urls.append(pedido.url.path)
        if pedido.url.path.endswith('2026-11-13'):
            return httpx.Response(200, json={'segments': []})
        return httpx.Response(200, json=TRACKER)

    monkeypatch.setattr(rotas, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(tracker)))
    monkeypatch.setattr(rotas, '_cache', {})
    rota = asyncio.run(rotas.buscar('glo1748', hoje=date(2026, 11, 13)))
    assert rota.indicativo == 'GLO1748'
    assert [u.rsplit('/', 2)[-2:] for u in urls] == [['GLO1748', '2026-11-13'], ['GLO1748', '2026-11-12']]
    asyncio.run(rotas.buscar('GLO1748', hoje=date(2026, 11, 13)))
    assert len(urls) == 2                                   # segunda vez veio do cache
