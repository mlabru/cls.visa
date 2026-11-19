"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Leitura de datas vindas de fora (API da REDEMET, GeoServer, arquivos de vento, preferências).

Regra única (DECISOES 2.6): tudo em UTC. Uma data sem fuso é tratada como UTC; uma com fuso
é convertida para UTC. Texto vazio ou ilegível devolve None: quem chama decide o que mostrar,
e uma data estranha numa fonte nunca derruba a tela.
"""
from datetime import UTC, datetime


def ler_iso(texto) -> datetime | None:
    """Lê uma data ISO 8601 vinda de fora e a devolve em UTC.

    Desde o Python 3.11, `fromisoformat` aceita o sufixo 'Z' e o espaço no lugar do 'T'.

    Parâmetros:
        texto: ex.: '2026-11-13T16:08:07Z', '2026-11-13 16:08:07', '2026-11-13T13:08-03:00'.
            Qualquer valor; não-texto é convertido com str().

    Retorna:
        datetime com fuso UTC (sem fuso no texto = UTC); None se vazio ou ilegível.
    """
    if not texto:
        return None
    try:
        quando = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    if quando.tzinfo is None:
        return quando.replace(tzinfo=UTC)
    return quando.astimezone(UTC)
