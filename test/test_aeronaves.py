import aeronaves

# Exemplo real recebido do ciop-tracker em 12/11/2026
GLO1835 = {
    'adep': 'SBGL', 'ades': 'SBBR', 'altitude': 32000, 'callsign': 'GLO1835',
    'heading': 5.68723726272583, 'id_plan': 'J260MTN7', 'latitude': -21.17863052174158,
    'longitude': -43.8229513374762, 'source': 'MONTREAL', 'speed': 385.0,
}


def test_heading_em_radianos_vira_graus():
    a = aeronaves.converter(GLO1835)
    assert round(a.rumo) == 326          # Rio -> Brasília: noroeste
    assert a.nivel_voo == 320 and a.indicativo == 'GLO1835'


def test_track_em_graus_quando_nao_ha_heading():
    a = aeronaves.converter({**GLO1835, 'heading': None, 'track': 90})
    assert a.rumo == 90


def test_sem_posicao_e_descartada():
    assert aeronaves.converter({**GLO1835, 'latitude': None}) is None


def test_filtro_por_prefixo_e_nivel():
    aeronaves.trafego.aeronaves.clear()
    aeronaves.trafego.atualizar([
        GLO1835,
        {**GLO1835, 'callsign': 'TAM3000', 'id_plan': 'B', 'altitude': 5000},
    ])
    assert [a['indicativo'] for a in aeronaves.filtrar(['GLO'], 0, 600)] == ['GLO1835']
    assert [a['indicativo'] for a in aeronaves.filtrar([], 0, 100)] == ['TAM3000']
