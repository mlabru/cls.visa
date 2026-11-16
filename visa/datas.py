# -*- coding: utf-8 -*-
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
    """'2026-11-13T16:08:07Z', '2026-11-13 16:08:07', '2026-11-13T13:08-03:00'... -> datetime UTC.

    Desde o Python 3.11, `fromisoformat` aceita o sufixo 'Z' e o espaço no lugar do 'T'.
    """
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
