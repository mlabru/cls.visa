import time
from datetime import UTC, datetime

import cache
import datas
import severo


def test_ler_iso_sempre_em_utc():
    esperado = datetime(2026, 11, 13, 16, 8, 7, tzinfo=UTC)
    assert datas.ler_iso('2026-11-13T16:08:07Z') == esperado
    assert datas.ler_iso('2026-11-13 16:08:07') == esperado                  # sem fuso = UTC
    assert datas.ler_iso('2026-11-13T13:08:07-03:00') == esperado            # convertido para UTC
    assert datas.ler_iso('2026-11-13T13:08:07-03:00').tzinfo is UTC
    assert datas.ler_iso('') is None and datas.ler_iso(None) is None
    assert datas.ler_iso('ontem') is None


def test_severo_com_validade_ilegivel_nao_quebra():
    assert severo.rotulo('ontem') == 'ontem'
    assert severo.situacao('ontem') == ('validade ilegível', 'warning')
    assert severo.situacao('2026-11-13T18:00:00Z', datetime(2026, 11, 13, 15, tzinfo=UTC))[0] == 'próxima (< 6 h)'


def test_ainda_vale():
    assert cache.ainda_vale(time.monotonic(), 60)
    assert not cache.ainda_vale(time.monotonic() - 61, 60)
    assert not cache.ainda_vale(None, 60)
