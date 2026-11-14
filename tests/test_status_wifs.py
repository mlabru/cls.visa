import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import radar
import wifs

AGORA = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_classificacao_do_status_como_no_original():
    m = lambda minutos: AGORA - timedelta(minutes=minutos)
    assert radar.classificar_status(m(30), AGORA) == 'green'
    assert radar.classificar_status(m(31), AGORA) == 'yellow'
    assert radar.classificar_status(m(60), AGORA) == 'yellow'
    assert radar.classificar_status(m(180), AGORA) == 'red'
    assert radar.classificar_status(m(181), AGORA) == 'gray'
    assert radar.classificar_status(None, AGORA) == 'gray'


def test_filtro_do_status():
    assert radar.filtro_status('maxcappi', '400km', '3') == "produto='maxcappi_400km'"
    assert radar.filtro_status('cappi', '250km', '5') == "produto='cappi_250km' AND elevacao=5"
    with pytest.raises(ValueError):
        radar.filtro_status("maxcappi' OR '1'='1", '400km', '3')


def test_busca_do_status(monkeypatch):
    pedidos = []

    def wfs_falso(pedido):
        pedidos.append(dict(pedido.url.params))
        return httpx.Response(200, json={'features': [
            {'properties': {'radar_code': 'GA', 'last_update': '2026-11-13T11:50:00Z'}},
            {'properties': {'radar_code': 'be', 'last_update': '2026-11-13T09:00:00'}},     # sem fuso: UTC
            {'properties': {'radar_code': 'XX', 'last_update': '2026-11-13T11:00:00Z'}},    # desconhecido
            {'properties': {'radar_code': 'al', 'last_update': 'ontem'}},                   # data inválida
        ]})

    monkeypatch.setattr(radar, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(wfs_falso)))
    monkeypatch.setattr(radar, '_indisponivel_ate', 0.0)
    status = asyncio.run(radar.buscar_status('maxcappi', '400km', '3'))
    assert status == {'ga': datetime(2026, 11, 13, 11, 50, tzinfo=UTC),
                      'be': datetime(2026, 11, 13, 9, 0, tzinfo=UTC)}
    assert pedidos[0]['typeName'] == 'redemet:radar_status_latest'
    assert pedidos[0]['cql_filter'] == "produto='maxcappi_400km'"


def test_status_sem_geoserver_e_none(monkeypatch):
    monkeypatch.setattr(radar, '_indisponivel_ate', float('inf'))
    assert asyncio.run(radar.buscar_status('maxcappi', '400km', '3')) is None

    def fora(pedido):
        raise httpx.ConnectError('sem rota')
    monkeypatch.setattr(radar, '_indisponivel_ate', 0.0)
    monkeypatch.setattr(radar, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(fora)))
    assert asyncio.run(radar.buscar_status('maxcappi', '400km', '3')) is None


def test_camadas_wifs():
    opcoes = wifs.opcoes_wms('gelo_severo', 340)
    assert opcoes['layers'] == 'redemet:gelo_severo' and opcoes['styles'] == 'gelo_wifs'
    assert opcoes['ELEVATION'] == '340' and opcoes['TIME'] == 'current'
    # o proxy aceita todas as camadas WIFS
    for c in wifs.CAMADAS:
        assert radar.pedido_permitido({'request': 'GetMap', 'layers': f'redemet:{c.chave}'})
    assert 'STYLE=redemet:temperatura_novo' in wifs.url_legenda('temperatura')
    assert set(wifs.NIVEIS) == {50, 100, 140, 180, 240, 300, 340, 390, 450, 530}
