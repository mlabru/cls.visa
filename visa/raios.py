"""
Raios (descargas atmosféricas): camada WMS `lightning_data` do GeoServer da REDEMET.

As imagens passam pelo mesmo proxy do radar (/radar/wms). O GeoServer desenha os raios
de uma janela de 60 min com um dos dois estilos (SLD) abaixo; os estilos recebem, pelo
parâmetro `env`, o raio do círculo e os instantes que separam as classes de tempo.
"""
from datetime import UTC, datetime, timedelta

JANELA_MIN = 60
PASSO_MIN = 15          # botões ◀ ▶ andam de 15 em 15 min

CLASSIFICACOES = {'tempo': 'Tempo (última hora)', 'intensidade': 'Intensidade'}
ESTILOS = {'tempo': 'redemet:lightning_time_classification',
           'intensidade': 'redemet:lightning_by_intensity'}

# Cores do estilo "tempo" (definidas no SLD do GeoServer)
LEGENDA_TEMPO = [('#ef4444', '0 a 15 min'), ('#fff000', '15 a 30 min'),
                 ('#22c55e', '30 a 45 min'), ('#1d4ed8', '45 a 60 min')]
# Cores do estilo "intensidade": só o GeoServer conhece (sem VPN não há como conferir)


def agora() -> datetime:
    return datetime.now(UTC).replace(second=0, microsecond=0)


def _iso(d: datetime) -> str:
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def _compacto(d: datetime) -> str:
    # O SLD lê as datas com dateParse yyyyMMdd'T'HHmmss'Z' (sem '-' e ':', que quebrariam o env)
    return d.strftime('%Y%m%dT%H%M%SZ')


def parametros(fim: datetime, classificacao: str, raio_km: int) -> dict:
    """Parâmetros do WMS para a janela de 60 min que termina em `fim`."""
    inicio = fim - timedelta(minutes=JANELA_MIN)
    marcos = {f't{m}': _compacto(fim - timedelta(minutes=m)) for m in (0, 15, 30, 45, 60)}
    env = ';'.join([f'radius:{int(raio_km) * 1000}'] + [f'{k}:{v}' for k, v in marcos.items()])
    return {
        'TIME': f'{_iso(inicio)}/{_iso(fim)}',
        'env': env,
        'styles': ESTILOS.get(classificacao, ESTILOS['tempo']),
    }


def deslocar(fim: datetime | None, minutos: int, referencia: datetime | None = None) -> datetime | None:
    """Anda a janela. Devolve None ("ao vivo") se o resultado chegar ao horário atual."""
    referencia = referencia or agora()
    novo = (fim or referencia) + timedelta(minutes=minutos)
    return None if novo >= referencia else novo


def rotulo_janela(fim: datetime) -> str:
    inicio = fim - timedelta(minutes=JANELA_MIN)
    return f'{inicio:%H:%M} – {fim:%H:%M} UTC · {fim:%d/%m}'
