"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Tráfego aéreo em tempo real (ciop-tracker do DECEA, via Pusher).

O servidor mantém UMA conexão com o Pusher, compartilhada por todos os navegadores
(DECISOES 2.4). O token de acesso fica só aqui — o navegador recebe apenas as posições.
main.py inicia `manter_conexao()`; a camada de tráfego, o impacto, a gaveta e a barra de
saúde leem `trafego`.

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
    """Uma aeronave, como a última mensagem do tracker a descreveu.

    Atributos:
        chave: identificador único (id_plan, indicativo, matrícula ou id).
        indicativo: indicativo de chamada (ou matrícula), em maiúsculas.
        lat: latitude, em graus.
        lon: longitude, em graus.
        rumo: em graus, 0 = norte, sentido horário.
        altitude_ft: altitude, em pés; None se o transponder não mandou.
        velocidade_kt: velocidade, em nós; None se não veio.
        origem: aeródromo de partida (ICAO; '' se desconhecido).
        destino: aeródromo de destino (ICAO; '' se desconhecido).
        fonte: de onde o tracker tirou a posição.
        visto_em: instante da mensagem, em time.monotonic().
    """
    chave: str
    indicativo: str
    lat: float
    lon: float
    rumo: float
    altitude_ft: float | None
    velocidade_kt: float | None
    origem: str
    destino: str
    fonte: str
    visto_em: float = field(default_factory=time.monotonic)

    @property
    def nivel_voo(self) -> int | None:
        """Nível de voo (altitude em centenas de pés); None se a altitude não veio."""
        if self.altitude_ft is None:
            return None
        return round(self.altitude_ft / 100)


@dataclass
class Trafego:
    """Estado compartilhado: as aeronaves conhecidas e a situação da conexão.

    Atributos:
        aeronaves: aeronaves por chave.
        situacao: texto da situação da conexão, para a tela.
        conectado: True enquanto a inscrição no canal estiver valendo.
        versao: aumenta a cada mensagem; os navegadores só reenviam a lista se mudou.
        recebido_em: hora (UTC) da última mensagem do Pusher (barra de saúde dos dados).
    """
    aeronaves: dict[str, Aeronave] = field(default_factory=dict)
    situacao: str = 'iniciando'
    conectado: bool = False
    versao: int = 0
    recebido_em: datetime | None = None

    def atualizar(self, lista: list[dict]) -> None:
        """Aplica uma mensagem do tracker: atualiza as aeronaves, tira as sumidas e sobe a versão.

        Parâmetros:
            lista: aeronaves da mensagem, como vieram (itens inválidos são ignorados).
        """
        for bruto in lista:
            aeronave = converter(bruto)
            if aeronave:
                self.aeronaves[aeronave.chave] = aeronave
        self.expirar()
        self.versao += 1
        self.recebido_em = datetime.now(UTC)

    def expirar(self) -> None:
        """Tira as aeronaves não vistas há mais de AERONAVE_EXPIRA_S segundos."""
        limite = time.monotonic() - config.AERONAVE_EXPIRA_S
        sumidas = [chave for chave, aeronave in self.aeronaves.items() if aeronave.visto_em < limite]
        for chave in sumidas:
            del self.aeronaves[chave]


trafego = Trafego()


def _numero(valor) -> float | None:
    """Converte para float; None se não for número ou não for finito (NaN, infinito)."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    return numero


def _campo(bruto: dict, preferido: str, alternativo: str):
    """Devolve bruto[preferido] se ele vier (não None); senão, bruto[alternativo] (ou None)."""
    valor = bruto.get(preferido)
    if valor is not None:
        return valor
    return bruto.get(alternativo)


def _rumo(bruto: dict) -> float:
    """Lê o rumo da aeronave, em graus (0 a 360).

    `heading` vem em RADIANOS (conferido em 12/11/2026: GLO1835 SBGL→SBBR com
    heading 5,69 rad = 326°, coerente com a rota). Outras fontes podem mandar
    `track`/`true_track` em graus.

    Retorna:
        O rumo; 0.0 se nenhum dos campos vier.
    """
    heading = _numero(bruto.get('heading'))
    track = _numero(_campo(bruto, 'true_track', 'track'))
    if heading is not None:
        return math.degrees(heading) % 360
    if track is not None:
        return track % 360
    return 0.0


def converter(bruto: dict) -> Aeronave | None:
    """Converte uma aeronave da mensagem do tracker.

    Parâmetros:
        bruto: o dict da aeronave, como veio do Pusher.

    Retorna:
        A Aeronave; None se faltar posição ou identificador.
    """
    lat = _numero(bruto.get('latitude'))
    lon = _numero(bruto.get('longitude'))
    chave = bruto.get('id_plan') or bruto.get('callsign') or bruto.get('registration') or bruto.get('id')
    if lat is None or lon is None or not chave:
        return None

    return Aeronave(
        chave=str(chave),
        indicativo=str(bruto.get('callsign') or bruto.get('registration') or chave).upper(),
        lat=lat, lon=lon, rumo=_rumo(bruto),
        altitude_ft=_numero(bruto.get('altitude')),
        velocidade_kt=_numero(_campo(bruto, 'speed', 'ground_speed')),
        origem=str(bruto.get('adep') or ''),
        destino=str(bruto.get('ades') or ''),
        fonte=str(bruto.get('source') or ''),
    )


def _para_mapa(aeronave: Aeronave, nivel_voo: int | None) -> dict:
    """Monta o dict que static/aeronaves.js espera (valores arredondados para encurtar o envio)."""
    velocidade = None if aeronave.velocidade_kt is None else round(aeronave.velocidade_kt)
    return {
        'lat': round(aeronave.lat, 5), 'lon': round(aeronave.lon, 5), 'rumo': round(aeronave.rumo),
        'indicativo': aeronave.indicativo, 'fl': nivel_voo, 'vel': velocidade,
        'origem': aeronave.origem, 'destino': aeronave.destino, 'fonte': aeronave.fonte,
    }


def filtrar(prefixos: list[str], fl_min: int, fl_max: int) -> list[dict]:
    """Lista as aeronaves que passam no filtro, já no formato que o mapa (aeronaves.js) espera.

    Aeronave sem altitude informada passa sempre pelo filtro de nível: escondê-la faria
    sumir do mapa um tráfego real só porque o transponder não mandou o dado.

    Parâmetros:
        prefixos: prefixos de indicativo aceitos (ex.: ['GLO', 'TAM']); vazio = todos.
        fl_min: nível de voo mínimo (FL).
        fl_max: nível de voo máximo (FL).

    Retorna:
        Lista de dicts (ver `_para_mapa`).
    """
    saida = []
    for aeronave in trafego.aeronaves.values():
        if prefixos and not any(aeronave.indicativo.startswith(prefixo) for prefixo in prefixos):
            continue
        nivel_voo = aeronave.nivel_voo
        if nivel_voo is not None and not fl_min <= nivel_voo <= fl_max:
            continue
        saida.append(_para_mapa(aeronave, nivel_voo))
    return saida


# ---------------------------------------------------------------------------
# Conexão com o Pusher
# ---------------------------------------------------------------------------

async def _assinar_canal(cliente: httpx.AsyncClient, socket_id: str) -> str:
    """Pede ao ciop-tracker a assinatura que autoriza a inscrição no canal privado.

    Parâmetros:
        cliente: cliente HTTP da conexão.
        socket_id: o id que o Pusher mandou em `pusher:connection_established`.

    Retorna:
        A assinatura ('auth'). Levanta httpx.HTTPError se o tracker recusar.
    """
    resposta = await cliente.post(
        config.TRACKER_AUTH_URL,
        data={'socket_id': socket_id, 'channel_name': config.PUSHER_CANAL},
        headers={'Authorization': f'Bearer {config.TRACKER_TOKEN}', 'Accept': 'application/json'},
    )
    resposta.raise_for_status()
    return resposta.json()['auth']


def _ler_mensagem(texto: str) -> tuple[str | None, object]:
    """Decodifica uma mensagem do Pusher.

    O Pusher manda `data` como JSON dentro de JSON: se ela for um texto JSON válido,
    é decodificada também; senão, fica como veio.

    Retorna:
        (nome do evento, dados).
    """
    mensagem = json.loads(texto)
    evento = mensagem.get('event')
    dados = mensagem.get('data')
    if isinstance(dados, str):
        with contextlib.suppress(json.JSONDecodeError):
            dados = json.loads(dados)
    return evento, dados


async def _tratar_evento(conexao, cliente: httpx.AsyncClient, evento: str | None, dados) -> None:
    """Reage a um evento do Pusher (passos 1 a 5 do protocolo, no topo do módulo).

    Levanta RuntimeError se o Pusher mandar `pusher:error` (a sessão cai e é refeita).

    Parâmetros:
        conexao: o WebSocket aberto.
        cliente: cliente HTTP (para pedir a assinatura do canal).
        evento: nome do evento.
        dados: dados do evento, já decodificados.
    """
    if evento == 'pusher:connection_established':
        assinatura = await _assinar_canal(cliente, dados['socket_id'])
        await conexao.send(json.dumps({'event': 'pusher:subscribe',
                                       'data': {'channel': config.PUSHER_CANAL, 'auth': assinatura}}))
    elif evento == 'pusher_internal:subscription_succeeded':
        trafego.conectado = True
        trafego.situacao = 'conectado'
        log.info('inscrito em %s', config.PUSHER_CANAL)
    elif evento == 'pusher:ping':
        await conexao.send(json.dumps({'event': 'pusher:pong', 'data': {}}))
    elif evento == 'pusher:error':
        raise RuntimeError(f'Pusher recusou: {dados}')
    elif evento == config.PUSHER_EVENTO:
        lista = dados.get('aircraft') if isinstance(dados, dict) else dados
        if isinstance(lista, list):
            trafego.atualizar(lista)


async def _sessao(cliente: httpx.AsyncClient) -> None:
    """Abre o WebSocket com o Pusher e trata as mensagens até a conexão cair.

    Parâmetros:
        cliente: cliente HTTP (para pedir a assinatura do canal).
    """
    url = f'wss://{config.PUSHER_HOST}:443/app/{config.PUSHER_CHAVE}?protocol=7&client=cls-visa&version=0.1'
    async with websockets.connect(url, max_size=2**24) as conexao:
        async for texto in conexao:
            evento, dados = _ler_mensagem(texto)
            await _tratar_evento(conexao, cliente, evento, dados)


async def manter_conexao() -> None:
    """Roda para sempre: conecta e, se cair, tenta de novo com espera crescente (2 s a 60 s)."""
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
