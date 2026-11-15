import asyncio

import httpx

import aerodromos

# Itens reais da API (12/11/2026), mais variações de status
SBIC = ['SBIC', 'Aeroporto de Itacoatiara', '-3.12722', '-58.4811', 'cinza',
        'METAR não disponível para SBIC\n\nTAF não disponível para SBIC']


def test_converte_item_real():
    a = aerodromos.converter(SBIC)
    assert a.icao == 'SBIC' and a.lat == -3.12722 and a.cor == aerodromos.COR_SEM_DADOS
    assert not a.tem_aviso


def test_cores_e_aviso():
    def status(s):
        return aerodromos.converter([*SBIC[:4], s, ''])
    assert status('g').cor == status('gw').cor == '#10b981'
    assert status('yw').cor == '#fbbf24'           # o HTML antigo pintava 'yw' de cinza
    assert status('gw').tem_aviso and not status('g').tem_aviso
    assert status('???').cor == aerodromos.COR_SEM_DADOS


def test_item_invalido_e_ignorado():
    assert aerodromos.converter(['SBXX', 'x', 'abc', '1', 'g', '']) is None
    assert aerodromos.converter(['curto']) is None


def test_texto_vai_como_texto_puro():
    a = aerodromos.converter([*SBIC[:5], '<img src=x onerror=alert(1)>'])
    assert a.para_mapa()['texto'] == '<img src=x onerror=alert(1)>'   # escapado no navegador (textContent)


def test_cache_e_falha_mantem_ultima_lista(monkeypatch):
    chamadas = []

    def redemet(pedido):
        chamadas.append(pedido.url.params['api_key'])
        if len(chamadas) == 1:
            return httpx.Response(200, json={'data': [SBIC]})
        return httpx.Response(500)

    monkeypatch.setattr(aerodromos, '_cliente', httpx.AsyncClient(transport=httpx.MockTransport(redemet)))
    monkeypatch.setattr(aerodromos, 'situacao', aerodromos.Situacao(aerodromos=[]))
    assert len(asyncio.run(aerodromos.obter())) == 1
    assert len(asyncio.run(aerodromos.obter())) == 1 and len(chamadas) == 1     # veio do cache
    aerodromos.situacao.consultado_em = 0                                       # força vencer o cache
    assert len(asyncio.run(aerodromos.obter())) == 1                            # REDEMET falhou: mantém
    assert 'indisponível' in aerodromos.situacao.erro
