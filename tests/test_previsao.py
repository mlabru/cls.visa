import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import geo
import previsao as p

T0 = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)


@dataclass
class Aviao:
    """O mínimo de aeronaves.Aeronave que a previsão usa."""
    chave: str
    lat: float
    lon: float
    rumo: float
    velocidade_kt: float | None = 480        # 480 kt ≈ 14,8 km/min
    nivel_voo: int | None = 350
    indicativo: str = ''

    def __post_init__(self):
        self.indicativo = self.indicativo or self.chave


def _nucleo(lat=0.05, lon=0.05, celulas=None, quando=T0, vn=0.0, ve=0.0, movimento=p.MEDIDO, dbz=50):
    """Núcleo de uma célula (por padrão a que tem centro em 0,05°/0,05°)."""
    celulas = celulas or ((0, 0),)
    return p.Nucleo(p.RADAR, tuple(celulas), lat, lon, dbz, quando, 0, vn, ve, movimento)


# --- geometria ----------------------------------------------------------------

def test_deslocar_e_distancias():
    lat, lon = geo.deslocar(0, 0, 0, geo.KM_POR_GRAU)          # 1 grau para o norte
    assert lat == pytest.approx(1) and lon == pytest.approx(0)
    lat, lon = geo.deslocar(0, 0, 90, geo.KM_POR_GRAU)         # 1 grau para leste
    assert lat == pytest.approx(0) and lon == pytest.approx(1)
    assert geo.distancias_km(0, 0, [0, 1], [1, 0]) == pytest.approx([geo.KM_POR_GRAU] * 2)


# --- núcleos -----------------------------------------------------------------

def test_agrupar_inclui_diagonal_e_separa_distantes():
    grupos = p.agrupar({(0, 0), (1, 1), (5, 5)})
    assert sorted(grupos) == [[(0, 0), (1, 1)], [(5, 5)]]


def test_nucleos_do_radar_marcam_raios_vizinhos():
    lista = p.nucleos({(0, 0): 45, (0, 1): 55}, p.RADAR, T0, com_raios={(1, 2)})
    assert len(lista) == 1
    n = lista[0]
    assert n.intensidade == 55 and n.raios == 1                 # (0,1) tem raios ao lado
    assert (n.lat, n.lon) == pytest.approx((0.05, 0.1))


def test_rastrear_mede_velocidade_e_estimar_usa_vizinhos():
    antes = p.nucleos({(0, 0): 50}, p.RADAR, T0 - timedelta(minutes=20))
    agora = p.nucleos({(0, 1): 50, (3, 3): 45, (200, 200): 50}, p.RADAR, T0)
    lista = p.estimar(p.rastrear(agora, antes))
    por_celula = {n.celulas[0]: n for n in lista}
    andou = por_celula[(0, 1)]                                  # 0,1° para leste em 20 min
    assert andou.movimento == p.MEDIDO
    assert andou.ve == pytest.approx(geo.KM_POR_GRAU * 0.1 / 20, rel=1e-3) and andou.vn == pytest.approx(0)
    assert andou.direcao == 'L' and andou.velocidade_kmh == pytest.approx(33.4, abs=0.1)
    assert por_celula[(3, 3)].movimento == p.ESTIMADO            # sem par, vizinho a ~40 km
    assert por_celula[(3, 3)].ve == pytest.approx(andou.ve)
    assert por_celula[(200, 200)].movimento == p.SEM_MEDIDA      # longe de tudo


def test_rastrear_ignora_par_longe_demais_ou_intervalo_ruim():
    antes = p.nucleos({(0, 0): 50}, p.RADAR, T0 - timedelta(minutes=20))
    longe = p.nucleos({(0, 10): 50}, p.RADAR, T0)               # 111 km em 20 min = 334 km/h
    assert p.rastrear(longe, antes)[0].movimento == p.SEM_MEDIDA
    cedo = p.nucleos({(0, 0): 50}, p.RADAR, T0 - timedelta(minutes=5))
    assert p.rastrear(p.nucleos({(0, 1): 50}, p.RADAR, T0), cedo)[0].movimento == p.SEM_MEDIDA


def test_anterior_escolhe_quadro_entre_10_e_40_min():
    @dataclass
    class Q:
        quando: datetime
    quadros = [Q(T0 - timedelta(minutes=m)) for m in (60, 40, 20, 7, 0)]
    assert p.anterior(quadros, quadros[-1]).quando == T0 - timedelta(minutes=20)
    assert p.anterior(quadros[-2:], quadros[-1]) is None


# --- conflitos ---------------------------------------------------------------

def test_encontro_com_nucleo_parado():
    aviao = Aviao('A', 0.05, -0.95, 90)                          # 111 km a oeste, indo para leste
    c = p.conflito_da_aeronave(aviao, [_nucleo()], T0)
    assert c.minutos == 7                                        # (111 - 10) / 14,8 ≈ 6,8
    assert c.lon == pytest.approx(-0.95 + 7 * 14.816 / geo.KM_POR_GRAU, abs=1e-3)
    assert c.confianca == p.ALTA


def test_nucleo_vindo_ao_encontro_adianta_o_conflito():
    vindo = _nucleo(ve=-14.816)                                   # mesma velocidade, sentido oposto
    assert p.conflito_da_aeronave(Aviao('A', 0.05, -0.95, 90), [vindo], T0).minutos == 4


def test_idade_do_quadro_move_o_nucleo_antes_de_comecar():
    # Quadro de 10 min atrás, núcleo indo para oeste a 6 km/min: hoje já está 60 km mais perto
    velho = _nucleo(quando=T0 - timedelta(minutes=10), ve=-6.0)
    c = p.conflito_da_aeronave(Aviao('A', 0.05, -0.95, 90), [velho], T0)
    assert c.minutos == 2                                          # (111 - 60 - 10) / (14,8 + 6) ≈ 1,98


def test_sem_conflito_indo_para_longe_ja_dentro_ou_quadro_velho():
    assert p.conflito_da_aeronave(Aviao('A', 0.05, -0.95, 270), [_nucleo()], T0) is None
    assert p.conflito_da_aeronave(Aviao('A', 0.05, 0.05, 90), [_nucleo()], T0) is None      # já está nele
    descartado = _nucleo(quando=T0 - timedelta(minutes=p.NUCLEO_DESCARTE_MIN + 1))
    assert p.conflito_da_aeronave(Aviao('A', 0.05, -0.95, 90), [descartado], T0) is None


def test_calcular_ordena_pelo_tempo_e_escolhe_o_primeiro_nucleo():
    perto, longe = _nucleo(), _nucleo(lon=1.05, celulas=((0, 10),))
    a = Aviao('A', 0.05, -0.95, 90)                                # encontra `perto` antes de `longe`
    b = Aviao('B', 0.05, -0.55, 90)                                # (66,8 - 10) / 14,8 ≈ 3,8
    conflitos = p.calcular([a, b], [longe, perto], T0)
    assert [(c.chave, c.minutos) for c in conflitos] == [('B', 4), ('A', 7)]
    assert conflitos[1].nucleo is perto


def test_em_rota_e_confianca():
    assert p.em_rota(Aviao('A', 0, 0, 0))
    assert not p.em_rota(Aviao('A', 0, 0, 0, nivel_voo=50))
    assert not p.em_rota(Aviao('A', 0, 0, 0, nivel_voo=None))
    assert not p.em_rota(Aviao('A', 0, 0, 0, velocidade_kt=80))
    assert p.confianca(8, p.MEDIDO, 5) == p.ALTA
    assert p.confianca(15, p.MEDIDO, 5) == p.MEDIA
    assert p.confianca(8, p.ESTIMADO, 5) == p.MEDIA
    assert p.confianca(8, p.MEDIDO, 30) == p.MEDIA
    assert p.confianca(25, p.SEM_MEDIDA, 30) == p.BAIXA


def test_descricao_do_nucleo():
    assert _nucleo(ve=0.5).descricao() == 'núcleo de 50 dBZ, vai para L a 30 km/h'
    assert _nucleo(movimento=p.SEM_MEDIDA).descricao() == 'núcleo de 50 dBZ, movimento não medido'
    raios = p.Nucleo(p.RAIOS, ((0, 0),), 0, 0, 1, T0, 1)
    assert raios.descricao() == 'núcleo com raios (1 célula(s) de 10 km), movimento não medido'


# --- verificação --------------------------------------------------------------

def _conflito(chave='A', minutos=5, quando=T0):
    return p.Conflito(chave, chave, 350, minutos, 0, 0, 0, 0, _nucleo(), p.ALTA, quando)


def test_verificador_acerto_falso_indeterminado_e_nao_previsto():
    v = p.Verificador()
    assert v.passo(T0, [], {'X'}, {'X'}) == []                     # 1º minuto: X já estava em risco
    ev = v.passo(T0, [_conflito('A'), _conflito('B'), _conflito('C')], {'A', 'B', 'C', 'X'}, {'X'})
    assert [e['tipo'] for e in ev] == ['alerta'] * 3
    assert v.passo(T0 + timedelta(minutes=1), [_conflito('A')], {'A', 'B', 'C'}, set()) == []   # nada novo
    ev = v.passo(T0 + timedelta(minutes=4), [], {'A', 'B', 'C', 'D'}, {'A', 'D'})
    assert [(e['tipo'], e['aeronave']) for e in ev] == [('acerto', 'A'), ('nao_previsto', 'D')]
    assert ev[0]['antecedencia_min'] == 4
    ev = v.passo(T0 + timedelta(minutes=16), [], {'B'}, set())     # prazo: 5 + 10 min
    assert sorted((e['tipo'], e['aeronave']) for e in ev) == [('falso', 'B'), ('indeterminado', 'C')]
    assert v.pendentes == {}


def test_registrar_e_resumir(tmp_path):
    v = p.Verificador()
    v.passo(T0, [], set(), set())
    eventos = v.passo(T0, [_conflito('A')], {'A'}, set())
    eventos += v.passo(T0 + timedelta(minutes=3), [], {'A'}, {'A'})
    p.registrar(eventos, tmp_path)
    linhas = (tmp_path / '2026-11-14.jsonl').read_text(encoding='utf-8').splitlines()
    assert [json.loads(linha)['tipo'] for linha in linhas] == ['alerta', 'acerto']
    r = p.resumir(Counter(alerta=10, acerto=6, falso=2, indeterminado=2, nao_previsto=6))
    assert r['acerto'] == 0.75 and r['deteccao'] == 0.5
    assert p.resumir(Counter())['acerto'] is None
