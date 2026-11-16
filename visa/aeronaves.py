"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Tráfego aéreo em tempo real (ciop-tracker do DECEA, via Pusher).

O servidor mantém UMA conexão com o Pusher, compartilhada por todos os navegadores.
O token de acesso fica só aqui — o navegador recebe apenas as posições.

Protocolo Pusher (o que o laravel-echo fazia no visualizador.html):
  1. abre o WebSocket e recebe `pusher:connection_established` com um socket_id;
  2. pede ao ciop-tracker uma assinatura para o canal privado (POST com o Bearer);
  3. envia `pusher:subscribe` com essa assinatura;
  4. recebe eventos `aircraft-update` — cada um traz a lista completa de aeronaves;
  5. responde `pusher:ping` com `pusher:pong` para manter a conexão.
"""
import asyncio
import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import websockets

import config

log = logging.getLogger('aeronaves')


@dataclass(frozen=True, slots=True)
class Aeronave:
    chave: str
    indicativo: str
    lat: float
    lon: float
    rumo: float             # graus, 0 = norte, sentido horário
    altitude_ft: float | None
    velocidade_kt: float | None
    origem: str
    destino: str
    fonte: str
    visto_em: float = field(default_factory=time.monotonic)

    @property
    def nivel_voo(self) -> int | None:
        return None if self.altitude_ft is None else round(self.altitude_ft / 100)


@dataclass
class Trafego:
    """Estado compartilhado: as aeronaves conhecidas e a situação da conexão."""
    aeronaves: dict[str, Aeronave] = field(default_factory=dict)
    situacao: str = 'iniciando'
    conectado: bool = False
    versao: int = 0          # aumenta a cada mensagem; os navegadores só reenviam se mudou
    recebido_em: datetime | None = None   # última mensagem do Pusher (barra de saúde dos dados)

    def atualizar(self, lista: list[dict]) -> None:
        for bruto in lista:
            aeronave = converter(bruto)
            if aeronave:
                self.aeronaves[aeronave.chave] = aeronave
        self.expirar()
        self.versao += 1
        self.recebido_em = datetime.now(UTC)

    def expirar(self) -> None:
        limite = time.monotonic() - config.AERONAVE_EXPIRA_S
        for chave in [c for c, a in self.aeronaves.items() if a.visto_em < limite]:
            del self.aeronaves[chave]


trafego = Trafego()


def _numero(valor) -> float | None:
    try:
        x = float(valor)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def converter(bruto: dict) -> Aeronave | None:
    """Converte uma aeronave da mensagem do tracker. Devolve None se faltar posição."""
    lat, lon = _numero(bruto.get('latitude')), _numero(bruto.get('longitude'))
    chave = bruto.get('id_plan') or bruto.get('callsign') or bruto.get('registration') or bruto.get('id')
    if lat is None or lon is None or not chave:
        return None

    # Rumo: `heading` vem em RADIANOS (conferido em 12/11/2026: GLO1835 SBGL→SBBR com
    # heading 5,69 rad = 326°, coerente com a rota). Outras fontes podem mandar
    # `track`/`true_track` em graus.
    heading = _numero(bruto.get('heading'))
    track = _numero(bruto.get('true_track') if bruto.get('true_track') is not None else bruto.get('track'))
    if heading is not None:
        rumo = math.degrees(heading) % 360
    elif track is not None:
        rumo = track % 360
    else:
        rumo = 0.0

    return Aeronave(
        chave=str(chave),
        indicativo=str(bruto.get('callsign') or bruto.get('registration') or chave).upper(),
        lat=lat, lon=lon, rumo=rumo,
        altitude_ft=_numero(bruto.get('altitude')),
        velocidade_kt=_numero(bruto.get('speed') if bruto.get('speed') is not None else bruto.get('ground_speed')),
        origem=str(bruto.get('adep') or ''),
        destino=str(bruto.get('ades') or ''),
        fonte=str(bruto.get('source') or ''),
    )


def filtrar(prefixos: list[str], fl_min: int, fl_max: int) -> list[dict]:
    """Aeronaves que passam no filtro, já no formato que o mapa (aeronaves.js) espera.

    Aeronave sem altitude informada passa sempre pelo filtro de nível: escondê-la faria
    sumir do mapa um tráfego real só porque o transponder não mandou o dado.
    """
    saida = []
    for a in trafego.aeronaves.values():
        if prefixos and not any(a.indicativo.startswith(p) for p in prefixos):
            continue
        fl = a.nivel_voo
        if fl is not None and not fl_min <= fl <= fl_max:
            continue
        saida.append({
            'lat': round(a.lat, 5), 'lon': round(a.lon, 5), 'rumo': round(a.rumo),
            'indicativo': a.indicativo, 'fl': fl,
            'vel': None if a.velocidade_kt is None else round(a.velocidade_kt),
            'origem': a.origem, 'destino': a.destino, 'fonte': a.fonte,
        })
    return saida


# ---------------------------------------------------------------------------
# Conexão com o Pusher
# ---------------------------------------------------------------------------

async def _assinar_canal(cliente: httpx.AsyncClient, socket_id: str) -> str:
    resposta = await cliente.post(
        config.TRACKER_AUTH_URL,
        data={'socket_id': socket_id, 'channel_name': config.PUSHER_CANAL},
        headers={'Authorization': f'Bearer {config.TRACKER_TOKEN}', 'Accept': 'application/json'},
    )
    resposta.raise_for_status()
    return resposta.json()['auth']


async def _sessao(cliente: httpx.AsyncClient) -> None:
    url = f'wss://{config.PUSHER_HOST}:443/app/{config.PUSHER_CHAVE}?protocol=7&client=cls-visa&version=0.1'
    async with websockets.connect(url, max_size=2**24) as ws:
        async for texto in ws:
            msg = json.loads(texto)
            evento = msg.get('event')
            dados = msg.get('data')
            if isinstance(dados, str):      # o Pusher manda `data` como JSON dentro de JSON
                with contextlib.suppress(json.JSONDecodeError):
                    dados = json.loads(dados)

            if evento == 'pusher:connection_established':
                assinatura = await _assinar_canal(cliente, dados['socket_id'])
                await ws.send(json.dumps({'event': 'pusher:subscribe',
                                          'data': {'channel': config.PUSHER_CANAL, 'auth': assinatura}}))
            elif evento == 'pusher_internal:subscription_succeeded':
                trafego.conectado, trafego.situacao = True, 'conectado'
                log.info('inscrito em %s', config.PUSHER_CANAL)
            elif evento == 'pusher:ping':
                await ws.send(json.dumps({'event': 'pusher:pong', 'data': {}}))
            elif evento == 'pusher:error':
                raise RuntimeError(f'Pusher recusou: {dados}')
            elif evento == config.PUSHER_EVENTO:
                lista = dados.get('aircraft') if isinstance(dados, dict) else dados
                if isinstance(lista, list):
                    trafego.atualizar(lista)


async def manter_conexao() -> None:
    """Roda para sempre: conecta e, se cair, tenta de novo com espera crescente."""
    espera = 2
    async with httpx.AsyncClient(timeout=15) as cliente:
        while True:
            try:
                trafego.situacao = 'conectando'
                await _sessao(cliente)
                trafego.situacao = 'conexão encerrada pelo servidor'
            except asyncio.CancelledError:
                raise
            except Exception as erro:  # noqa: BLE001 — rede, autenticação, protocolo: o laço nunca pode morrer
                trafego.situacao = f'erro: {type(erro).__name__}'
                log.warning('Pusher: %s — nova tentativa em %ss', erro, espera)
            if trafego.conectado:   # chegou a funcionar: a próxima tentativa é rápida
                espera = 2
            trafego.conectado = False
            await asyncio.sleep(espera)
            espera = min(espera * 2, 60)
