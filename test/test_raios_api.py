import io
from datetime import UTC, datetime, timedelta

import numpy as np
from PIL import Image

import animacao
import redemet
import saude


def _pontos(*celulas):
    return [{'la': str(la), 'lo': str(lo)} for la, lo in celulas]


A, B, C = (-15.05, -47.95), (-15.15, -47.95), (-23.45, -46.55)


def test_persistencia_das_celulas():
    dados = {'info': {'ultima_ocorrencia': '2026-11-13 17:01:59'},
             'anima': ['16:40', '16:47', '16:54', '17:01'],
             'stsc': [_pontos(A), _pontos(A, B), _pontos(A, B), _pontos(A, C)]}   # B some no último
    quadros = redemet.converter_raios(dados)
    assert [f'{q.quando:%H:%M}' for q in quadros] == ['16:40', '16:47', '16:54', '17:01']
    idades = {(la, lo): m for la, lo, m in quadros[-1].celulas}
    assert idades == {A: 21, C: 0}                     # A ativa desde 16:40; C acabou de surgir
    assert {(la, lo): m for la, lo, m in quadros[2].celulas}[B] == 7
    assert quadros[-1].chave == '20261113T1701' and quadros[-1].url() == '/redemet/raios?q=20261113T1701'


def test_quadros_que_passam_da_meia_noite():
    dados = {'info': {'ultima_ocorrencia': '2026-11-14 00:03:10'}, 'anima': ['23:56', '00:03'],
             'stsc': [_pontos(A), _pontos(A)]}
    antes, depois = redemet.converter_raios(dados)
    assert antes.quando == datetime(2026, 11, 13, 23, 56, tzinfo=UTC)
    assert depois.quando.day == 14 and depois.celulas[0][2] == 7


def test_resposta_estranha():
    assert redemet.converter_raios({}) == []
    assert redemet.converter_raios({'info': {'ultima_ocorrencia': '2026-11-13 17:01:59'},
                                    'anima': ['17:01'], 'stsc': []}) == []        # tamanhos diferentes
    q = redemet.converter_raios({'info': {'ultima_ocorrencia': '2026-11-13 17:01:59'}, 'anima': ['17:01'],
                                 'stsc': [[{'la': 'x', 'lo': '1'}, {'lo': '2'}] + _pontos(A)]})
    assert len(q[0].celulas) == 1


def test_classes_e_imagem():
    assert [redemet.classe_raio(m) for m in (0, 14, 15, 45, 60, 300)] == [0, 0, 1, 2, 3, 3]
    quando = datetime(2026, 11, 13, 17, 1, tzinfo=UTC)
    quadro = redemet.QuadroRaios(quando, ((A[0], A[1], 0), (C[0], C[1], 90)))
    img = np.array(Image.open(io.BytesIO(redemet.desenhar_raios(quadro))))
    assert img.shape[1] == 1500                       # 2 px por célula de 0,1° em 75° de longitude
    x = round((A[1] - redemet.RAIOS_OESTE) / 0.1 * 2)
    coluna = img[:, x]
    assert set(np.unique(coluna)) == {0, 1}           # só a célula A nessa coluna, na classe 0 (+1)
    assert 2 <= (coluna == 1).sum() <= 3               # ~2 px por célula (um pouco mais em Mercator)
    xc = round((C[1] - redemet.RAIOS_OESTE) / 0.1 * 2)
    assert 4 in np.unique(img[:, xc])                   # célula C: classe "mais de 1 h"


def test_raios_na_linha_do_tempo_e_na_saude():
    t0 = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)
    raios = [redemet.QuadroRaios(t0 + timedelta(minutes=m), ()) for m in (0, 7, 13, 20, 27)]
    passos = animacao.linha_do_tempo([], [], 30, raios)
    assert [(f'{p.quando:%H:%M}', f'{p.raios.quando:%H:%M}') for p in passos] == \
        [('16:00', '16:00'), ('16:10', '16:07'), ('16:20', '16:20'), ('16:27', '16:27')]
    assert saude.raios_api(t0, agora=t0 + timedelta(minutes=10)).nivel == saude.OK
    assert saude.raios_api(t0, agora=t0 + timedelta(minutes=40)).nivel == saude.CRITICO
    assert saude.raios_api(None, 'API da REDEMET indisponível').texto == 'fora'
