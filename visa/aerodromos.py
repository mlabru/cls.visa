"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Aeródromos: status meteorológico (METAR/TAF/avisos) vindo da API da REDEMET.

O servidor consulta a REDEMET (a api_key fica só aqui) e guarda o resultado por
alguns minutos, para que vários usuários abertos não multipliquem as consultas.

Formato de cada item em `data`:
    [código ICAO, nome, latitude, longitude, status, texto]
O status é uma letra de cor (g, y, r, b) que pode vir seguida de 'w' — pelos textos
da própria API, entendemos o 'w' como "há aviso de aeródromo" (a confirmar).

Usado pela camada de aeródromos, pelo impacto, pela sonda e pela tela (legenda, barra de saúde).
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

import cache
import config

log = logging.getLogger('aerodromos')

CORES = {
    'g': '#10b981', 'verde': '#10b981',
    'y': '#fbbf24', 'amarelo': '#fbbf24',
    'r': '#ef4444', 'vermelho': '#ef4444',
    'b': '#3b82f6', 'azul': '#3b82f6',
}
COR_SEM_DADOS = '#94a3b8'

LEGENDA = [('#10b981', 'boas condições'), ('#fbbf24', 'atenção'), ('#ef4444', 'restrição'),
           ('#3b82f6', 'outro'), (COR_SEM_DADOS, 'sem METAR'), ('#ffffff', 'borda branca: há aviso')]


@dataclass(frozen=True, slots=True)
class Aerodromo:
    """Um aeródromo com o seu status meteorológico.

    Atributos:
        icao: código ICAO (ex.: 'SBGR').
        nome: nome do aeródromo.
        lat: latitude, em graus.
        lon: longitude, em graus.
        status: letra de cor (g, y, r, b), talvez seguida de 'w' (aviso); '' = sem METAR.
        texto: METAR/TAF/avisos como a API mandou (texto puro).
    """
    icao: str
    nome: str
    lat: float
    lon: float
    status: str
    texto: str

    @property
    def cor(self) -> str:
        """Cor do marcador no mapa (hex), a partir do status; COR_SEM_DADOS se desconhecido."""
        status = self.status.lower()
        if status in CORES:
            return CORES[status]
        # 'gw', 'yw', 'rw'...: cor + aviso
        return CORES.get(status.removesuffix('w'), COR_SEM_DADOS)

    @property
    def tem_aviso(self) -> bool:
        """Diz se há aviso de aeródromo (status de duas letras terminando em 'w')."""
        status = self.status.lower()
        return len(status) == 2 and status.endswith('w')

    def para_mapa(self) -> dict:
        """Monta o dict que static/camadas.js espera. O texto vai como texto puro (nunca HTML).

        Retorna:
            Dict com lat, lon, cor, aviso, titulo e texto.
        """
        return {'lat': self.lat, 'lon': self.lon, 'cor': self.cor, 'aviso': self.tem_aviso,
                'titulo': f'{self.icao} – {self.nome}', 'texto': self.texto}


def converter(item: list) -> Aerodromo | None:
    """Converte um item de `data` da API num Aerodromo.

    Parâmetros:
        item: [código ICAO, nome, latitude, longitude, status, texto].

    Retorna:
        O Aerodromo, ou None se o item estiver malformado.
    """
    try:
        icao, nome, lat, lon, status, texto = item
        return Aerodromo(str(icao), str(nome), float(lat), float(lon), str(status or ''), str(texto or ''))
    except (TypeError, ValueError):
        return None


@dataclass
class Situacao:
    """Última consulta à REDEMET, compartilhada por todos os usuários.

    Atributos:
        aerodromos: a última lista boa (fica mesmo se a consulta seguinte falhar).
        consultado_em: instante da última consulta boa, em time.monotonic().
        erro: mensagem para a tela se a última consulta falhou; '' = tudo certo.
    """
    aerodromos: list[Aerodromo]
    consultado_em: float = 0.0
    erro: str = ''


situacao = Situacao(aerodromos=[])
_cliente = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
_trava = asyncio.Lock()


def _cache_valido() -> bool:
    """Diz se há lista guardada e ela tem menos de AERODROMOS_CACHE_S segundos."""
    return bool(situacao.aerodromos) and cache.ainda_vale(situacao.consultado_em, config.AERODROMOS_CACHE_S)


async def obter() -> list[Aerodromo]:
    """Devolve a lista de aeródromos, consultando a REDEMET só se o cache tiver vencido.

    Retorna:
        A lista atual (a última boa, se a REDEMET falhar; vazia se nunca respondeu).
    """
    if _cache_valido():
        return situacao.aerodromos
    async with _trava:          # quem chegar enquanto outro consulta, espera e usa o resultado
        if _cache_valido():
            return situacao.aerodromos
        return await _consultar()


async def _consultar() -> list[Aerodromo]:
    """Consulta a REDEMET e atualiza `situacao`.

    Retorna:
        A lista nova; se a consulta falhar, a última lista boa (e `situacao.erro` preenchido).
    """
    try:
        resposta = await _cliente.get(config.REDEMET_STATUS_URL, params={'api_key': config.REDEMET_API_KEY})
        resposta.raise_for_status()
        itens = resposta.json().get('data') or []
    except (httpx.HTTPError, ValueError) as erro:
        situacao.erro = f'REDEMET indisponível ({type(erro).__name__})'
        log.warning('%s', situacao.erro)
        return situacao.aerodromos          # mantém a última lista boa, se houver
    convertidos = [converter(item) for item in itens]
    situacao.aerodromos = [aerodromo for aerodromo in convertidos if aerodromo]
    situacao.consultado_em = time.monotonic()
    situacao.erro = ''
    return situacao.aerodromos
