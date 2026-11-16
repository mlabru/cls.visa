"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

FAB.ia — conflitos previstos: aeronaves em rota que vão cruzar um núcleo de tempestade
nos próximos 30 minutos.

Tudo determinístico (sem IA), em três passos:

1. **Núcleos e o seu movimento.** As células de 0,1° com eco forte (radar) ou com raios são
   agrupadas em núcleos (células vizinhas = mesmo núcleo). Cada núcleo do quadro atual é
   casado com o núcleo mais próximo do quadro anterior (10 a 40 min antes): o deslocamento
   dividido pelo tempo dá a velocidade. Núcleo sem par recebe o vento médio 850–300 hPa no
   lugar ("vento", vento.deslocamento); sem arquivo de vento recente, o movimento mediano dos
   vizinhos ("estimado"); sem nada disso, fica parado ("sem medida"). Um par medido muito
   diferente do vento é tratado como par errado e também recebe o vento.
   Núcleos de radar de uma célula só são descartados (o registro mostrou mais falsos que acertos).
2. **Projeção.** A aeronave segue em linha reta, no rumo e na velocidade atuais; o núcleo
   segue no seu movimento, a partir da hora do quadro (que já tem alguns minutos de idade).
3. **Encontro.** Minuto a minuto, até 30 min: o primeiro instante em que a aeronave fica a
   menos de RAIO_CONFLITO_KM de uma célula do núcleo é o conflito.

Cada alerta é registrado (config.PREVISAO_DIR, um arquivo JSON Lines por dia) junto com o
que aconteceu depois — a aeronave entrou numa área de risco perto do núcleo PREVISTO? — para
medir acertos e alarmes falsos desde o primeiro dia. Duas regras simples rodam junto, só no
registro, para comparação (REGRAS): o mesmo cálculo com o núcleo parado, e "aeronave a menos
de PROXIMIDADE_KM de um núcleo". Se o FAB.ia não ganhar delas, a extrapolação não está
ajudando. Resumo: `./visa.sh avaliar`.

Sem quadro recente de radar nem de raios (impacto.fontes_validas), o cálculo para (`pausar`):
alertas sobre dado velho enganariam a tela e o registro.

Limites conhecidos (ver DECISOES.md, 6.1): sem plano de voo, a projeção é uma reta (ruim
em subida, descida e chegada); sem topo das nuvens, um voo muito acima de um núcleo baixo
também é alertado; o núcleo é extrapolado sem crescer nem dissipar.

Este módulo não importa impacto.py: quem calcula as células (impacto.py) entrega a este as
chaves da grade de 0,1° — floor(lat*10), floor(lon*10) — e as aeronaves elegíveis.
"""
import json
import logging
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

import numpy as np

import config
import geo

log = logging.getLogger(__name__)

HORIZONTE_MIN = 30             # até quanto tempo à frente se procura conflito
RAIO_CONFLITO_KM = 10          # distância ao centro da célula que conta como "dentro" (meia célula ≈ 5,5 km + folga)
FL_MIN = 100                   # só aeronaves em rota: abaixo disso, subida e descida tornam a reta inútil
VELOCIDADE_MIN_KT = 120        # helicópteros e aeronaves paradas ficam de fora
VELOCIDADE_MAX_NUCLEO_KMH = 120    # deslocamento maior que isso entre quadros = par errado
VIZINHANCA_KM = 300            # de onde vem o movimento "estimado" de um núcleo sem par
INTERVALO_MIN, INTERVALO_MAX = 10, 40   # minutos entre o quadro atual e o anterior usado no movimento
NUCLEO_VELHO_MIN = 25          # quadro mais velho que isso: a confiança cai um nível
NUCLEO_DESCARTE_MIN = 60       # quadro mais velho que isso: núcleo ignorado
TOLERANCIA_MIN = 10            # o alerta "vence" este tempo depois do encontro previsto
# Acerto: a aeronave está numa área de risco a até ACERTO_KM de uma célula do núcleo previsto
# (movido até aquele minuto). A área de risco dos raios inclui as células vizinhas (~11 km a
# mais) e o movimento erra alguns km: 30 km. Entrada em área de risco longe do núcleo previsto
# não é acerto: é "não previsto" (com `alerta_pendente`), e o alerta continua valendo.
ACERTO_KM = 30
NUCLEO_RADAR_MIN_CELULAS = 2   # núcleo de radar de 1 célula: em 15/11, 94 acertos × 172 falsos
DIVERGENCIA_KMH = 50           # par medido que difere mais que isso do vento = par errado
MAX_CONFLITOS = 30             # na tela: no painel FAB.ia e no mapa (os mais próximos no tempo)
REDESENHO_S = 15               # na tela: painel FAB.ia, contador do cabeçalho e linhas do mapa

# Regras do registro. Só a FABIA aparece na tela; as outras existem para comparação.
FABIA, PARADO, PROXIMIDADE = 'fabia', 'parado', 'proximidade'
REGRAS = (FABIA, PARADO, PROXIMIDADE)
PROXIMIDADE_KM = 50            # regra boba: aeronave a menos disso de um núcleo, em qualquer rumo

RADAR, RAIOS = 'radar', 'raios'
ALTA, MEDIA, BAIXA = 'alta', 'media', 'baixa'
CONFIANCAS = (ALTA, MEDIA, BAIXA)
TEXTO_CONFIANCA = {ALTA: 'confiança alta', MEDIA: 'confiança média', BAIXA: 'confiança baixa'}
COR_CONFIANCA = {ALTA: '#ef4444', MEDIA: '#f59e0b', BAIXA: '#3b82f6'}   # as cores de impacto.NIVEIS
MEDIDO, VENTO, ESTIMADO, SEM_MEDIDA = 'medido', 'vento', 'estimado', 'sem medida'
DIRECOES = ('N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO')


class Desfecho(StrEnum):
    """Tipos de evento do registro (o campo 'tipo' do JSON). StrEnum: grava e compara como texto."""
    ALERTA = 'alerta'
    ACERTO = 'acerto'
    FALSO = 'falso'
    INDETERMINADO = 'indeterminado'
    NAO_PREVISTO = 'nao_previsto'


# ---------------------------------------------------------------------------
# Núcleos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Nucleo:
    fonte: str                          # RADAR (eco forte) ou RAIOS
    celulas: tuple[geo.Chave, ...]
    lat: float                          # centro do núcleo (média dos centros das células)
    lon: float
    intensidade: int                    # radar: dBZ máximo; raios: nº de células com raios
    quando: datetime                    # hora do quadro
    raios: int = 0                      # radar: células do núcleo com raios nelas ou ao lado
    vn: float = 0.0                     # velocidade para o norte, km/min
    ve: float = 0.0                     # velocidade para o leste, km/min
    movimento: str = SEM_MEDIDA

    @property
    def velocidade_kmh(self) -> float:
        return math.hypot(self.vn, self.ve) * 60

    @property
    def direcao(self) -> str:
        """Para onde o núcleo vai (L = leste, O = oeste)."""
        rumo = math.degrees(math.atan2(self.ve, self.vn)) % 360
        return DIRECOES[round(rumo / 45) % 8]

    def descricao(self) -> str:
        if self.fonte == RADAR:
            texto = f'núcleo de {self.intensidade} dBZ' + (' com raios' if self.raios else '')
        else:
            texto = f'núcleo com raios ({self.intensidade} célula(s) de 10 km)'
        if self.movimento == SEM_MEDIDA:
            return texto + ', movimento não medido'
        if self.velocidade_kmh < 5:
            return texto + ', quase parado'
        estimado = {ESTIMADO: ' (estimado)', VENTO: ' (pelo vento)'}.get(self.movimento, '')
        return texto + f', vai para {self.direcao} a {self.velocidade_kmh:.0f} km/h{estimado}'

    def distancia_km(self, lat: float, lon: float, agora: datetime) -> float:
        """Distância do ponto à célula mais próxima do núcleo, movido até `agora`."""
        minutos = (agora - self.quando).total_seconds() / 60
        cosseno = max(math.cos(math.radians(self.lat)), 0.01)
        rlat = lat - self.vn * minutos / geo.KM_POR_GRAU          # move o ponto no sentido contrário
        rlon = lon - self.ve * minutos / (geo.KM_POR_GRAU * cosseno)
        return min(math.hypot((rlat - c[0]) * geo.KM_POR_GRAU, (rlon - c[1]) * geo.KM_POR_GRAU * cosseno)
                   for c in map(geo.centro, self.celulas))


def agrupar(chaves) -> list[list[geo.Chave]]:
    """Células vizinhas (inclusive na diagonal) formam um grupo. Busca em largura, sem biblioteca."""
    restantes, grupos = set(chaves), []
    while restantes:
        fila = [restantes.pop()]
        grupo = []
        while fila:
            atual = fila.pop()
            grupo.append(atual)
            for vizinha in geo.vizinhas(atual):
                if vizinha in restantes:
                    restantes.remove(vizinha)
                    fila.append(vizinha)
        grupos.append(sorted(grupo))
    return grupos


def nucleos(celulas: dict[geo.Chave, int], fonte: str, quando: datetime,
            com_raios: set[geo.Chave] = frozenset()) -> list[Nucleo]:
    """Núcleos de um quadro. `celulas`: chave -> dBZ (radar) ou minutos ativa (raios)."""
    saida = []
    for grupo in agrupar(celulas):
        centros = [geo.centro(c) for c in grupo]
        lat = sum(c[0] for c in centros) / len(centros)
        lon = sum(c[1] for c in centros) / len(centros)
        if fonte == RADAR:
            intensidade = max(celulas[c] for c in grupo)
            raios = sum(1 for c in grupo if any(v in com_raios for v in geo.vizinhas(c)))
        else:
            intensidade = raios = len(grupo)
        saida.append(Nucleo(fonte, tuple(grupo), lat, lon, intensidade, quando, raios))
    return saida


def rastrear(atuais: list[Nucleo], anteriores: list[Nucleo]) -> list[Nucleo]:
    """Velocidade de cada núcleo atual pelo par mais próximo no quadro anterior."""
    if not anteriores:
        return atuais
    dt = (atuais[0].quando - anteriores[0].quando).total_seconds() / 60 if atuais else 0
    if not INTERVALO_MIN <= dt <= INTERVALO_MAX:
        return atuais
    alcance = VELOCIDADE_MAX_NUCLEO_KMH * dt / 60
    saida = []
    for n in atuais:
        par = min(anteriores, key=lambda a: geo.distancia_km(n.lat, n.lon, a.lat, a.lon))
        if geo.distancia_km(n.lat, n.lon, par.lat, par.lon) > alcance:
            saida.append(n)
            continue
        vn = (n.lat - par.lat) * geo.KM_POR_GRAU / dt
        ve = (n.lon - par.lon) * geo.KM_POR_GRAU * math.cos(math.radians(n.lat)) / dt
        saida.append(_com_movimento(n, vn, ve, MEDIDO))
    return saida


def estimar(lista: list[Nucleo], vento=None) -> list[Nucleo]:
    """Movimento dos núcleos sem par, e conferência dos pares medidos.

    `vento`: função (lat, lon) -> (vn, ve) em km/min ou None (vento.deslocamento), ou None se
    não houver arquivo de vento recente.
    - Com vento: núcleo sem par anda com o vento; par medido que difere do vento mais que
      DIVERGENCIA_KMH é tratado como par errado (fusão, divisão, crescimento) e anda com o vento.
    - Sem vento: núcleo sem par recebe a mediana dos medidos a até VIZINHANCA_KM. Tempestades
      vizinhas costumam andar juntas; a mediana ignora um par errado aqui e ali.
    """
    if vento is not None:
        saida = []
        for n in lista:
            pelo_vento = vento(n.lat, n.lon)
            if pelo_vento is not None:
                diferenca = math.hypot(n.vn - pelo_vento[0], n.ve - pelo_vento[1]) * 60
                if n.movimento == SEM_MEDIDA or diferenca > DIVERGENCIA_KMH:
                    n = _com_movimento(n, *pelo_vento, VENTO)
            saida.append(n)
        lista = saida
    medidos = [n for n in lista if n.movimento == MEDIDO]
    saida = []
    for n in lista:
        if n.movimento == SEM_MEDIDA:
            perto = [m for m in medidos if geo.distancia_km(n.lat, n.lon, m.lat, m.lon) <= VIZINHANCA_KM]
            if perto:
                n = _com_movimento(n, statistics.median(m.vn for m in perto),
                                   statistics.median(m.ve for m in perto), ESTIMADO)
        saida.append(n)
    return saida


def _com_movimento(n: Nucleo, vn: float, ve: float, movimento: str) -> Nucleo:
    return replace(n, vn=vn, ve=ve, movimento=movimento)


def anterior(quadros: list, ultimo) -> object | None:
    """O quadro mais recente entre INTERVALO_MIN e INTERVALO_MAX minutos antes do último."""
    candidatos = [q for q in quadros
                  if INTERVALO_MIN <= (ultimo.quando - q.quando).total_seconds() / 60 <= INTERVALO_MAX]
    return max(candidatos, key=lambda q: q.quando) if candidatos else None


# ---------------------------------------------------------------------------
# Conflitos
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Conflito:
    chave: str                  # da aeronave (aeronaves.Aeronave.chave)
    indicativo: str
    fl: int | None
    minutos: int                # até o encontro, contados de `calculado`
    lat: float                  # onde a aeronave estará no encontro
    lon: float
    lat0: float                 # onde estava no cálculo
    lon0: float
    nucleo: Nucleo
    confianca: str
    calculado: datetime

    @property
    def prazo(self) -> datetime:
        """Até quando a aeronave pode entrar na área de risco para o alerta contar como acerto."""
        return self.calculado + timedelta(minutes=self.minutos + TOLERANCIA_MIN)


def para_a_tela(conflitos: list[Conflito], mostrar_baixa: bool) -> list[Conflito]:
    """Os conflitos que a pessoa quer ver: os de confiança baixa só se pediu (preferência fabia_baixa)."""
    return conflitos if mostrar_baixa else [c for c in conflitos if c.confianca != BAIXA]


def em_rota(a) -> bool:
    """A aeronave entra na previsão? (em rota: nível e velocidade conhecidos e altos o bastante)."""
    fl, v = a.nivel_voo, a.velocidade_kt
    return fl is not None and fl >= FL_MIN and v is not None and v >= VELOCIDADE_MIN_KT


def confianca(minutos: int, movimento: str, idade_min: float) -> str:
    """Cai com a antecedência, com o movimento não medido e com o quadro velho."""
    nivel = 0 if minutos <= 10 else 1 if minutos <= 20 else 2
    if movimento != MEDIDO:
        nivel += 1
    if idade_min > NUCLEO_VELHO_MIN:
        nivel += 1
    return CONFIANCAS[min(nivel, 2)]


def conflito_da_aeronave(a, lista: list[Nucleo], agora: datetime, parado: bool = False) -> Conflito | None:
    """O primeiro encontro da aeronave com um núcleo nos próximos HORIZONTE_MIN minutos.

    `parado`: projeta como se os núcleos não andassem (regra PARADO, só para comparação). O
    Conflito guarda o núcleo com o movimento dele, para o acerto ser conferido igual nas regras.
    """
    t = np.arange(HORIZONTE_MIN + 1)                             # minutos a partir de agora
    km_por_min = a.velocidade_kt * 1.852 / 60
    plat, plon = geo.deslocar(a.lat, a.lon, a.rumo, km_por_min * t)
    melhor = None
    for n in lista:
        idade = (agora - n.quando).total_seconds() / 60
        if idade > NUCLEO_DESCARTE_MIN:
            continue
        # Filtro rápido: nem a aeronave nem o núcleo andam o bastante para se encontrar
        alcance = km_por_min * HORIZONTE_MIN + math.hypot(n.vn, n.ve) * (idade + HORIZONTE_MIN) \
            + len(n.celulas) * geo.GRADE * geo.KM_POR_GRAU + RAIO_CONFLITO_KM
        if geo.distancia_km(a.lat, a.lon, n.lat, n.lon) > alcance:
            continue
        # Em vez de mover o núcleo, move-se a aeronave no sentido contrário (dá no mesmo)
        cosseno = max(math.cos(math.radians(n.lat)), 0.01)
        vn, ve = (0.0, 0.0) if parado else (n.vn, n.ve)
        rlat = plat - vn * (idade + t) / geo.KM_POR_GRAU
        rlon = plon - ve * (idade + t) / (geo.KM_POR_GRAU * cosseno)
        centros = np.array([geo.centro(c) for c in n.celulas])
        dy = (rlat[:, None] - centros[None, :, 0]) * geo.KM_POR_GRAU
        dx = (rlon[:, None] - centros[None, :, 1]) * geo.KM_POR_GRAU * cosseno
        dmin = np.hypot(dx, dy).min(axis=1)
        if dmin[0] <= RAIO_CONFLITO_KM:
            continue                         # já está nele: é o alerta de agora (âmbar), não previsão
        encontros = np.nonzero(dmin <= RAIO_CONFLITO_KM)[0]
        if not encontros.size:
            continue
        m = int(t[encontros[0]])
        if melhor is None or m < melhor.minutos:
            melhor = Conflito(a.chave, a.indicativo, a.nivel_voo, m, float(plat[m]), float(plon[m]),
                              a.lat, a.lon, n, confianca(m, n.movimento, idade), agora)
    return melhor


def proximidade(a, lista: list[Nucleo], agora: datetime) -> Conflito | None:
    """Regra PROXIMIDADE (só para comparação): núcleo a menos de PROXIMIDADE_KM, em qualquer rumo.

    O "encontro" é a posição atual e o prazo é o horizonte inteiro (HORIZONTE_MIN).
    """
    melhor, menor = None, PROXIMIDADE_KM
    for n in lista:
        if (agora - n.quando).total_seconds() / 60 > NUCLEO_DESCARTE_MIN:
            continue
        if geo.distancia_km(a.lat, a.lon, n.lat, n.lon) > PROXIMIDADE_KM + len(n.celulas) * geo.GRADE * geo.KM_POR_GRAU:
            continue                                   # filtro rápido
        d = n.distancia_km(a.lat, a.lon, agora)
        if RAIO_CONFLITO_KM < d <= menor:              # já dentro: é o alerta de agora
            melhor, menor = n, d
    if melhor is None:
        return None
    return Conflito(a.chave, a.indicativo, a.nivel_voo, HORIZONTE_MIN, a.lat, a.lon, a.lat, a.lon,
                    melhor, BAIXA, agora)


def calcular(elegiveis: list, lista: list[Nucleo], agora: datetime, regra: str = FABIA) -> list[Conflito]:
    """Conflitos de todas as aeronaves elegíveis, do mais próximo ao mais distante no tempo."""
    if not lista:
        return []
    if regra == PROXIMIDADE:
        conflitos = [c for a in elegiveis if (c := proximidade(a, lista, agora))]
    else:
        parado = regra == PARADO
        conflitos = [c for a in elegiveis if (c := conflito_da_aeronave(a, lista, agora, parado))]
    return sorted(conflitos, key=lambda c: (c.minutos, CONFIANCAS.index(c.confianca), c.indicativo))


# ---------------------------------------------------------------------------
# Verificação: o alerta se confirmou?
# ---------------------------------------------------------------------------

@dataclass
class Verificador:
    """Acompanha cada alerta de uma regra até o desfecho, minuto a minuto.

    - acerto: a aeronave está numa área de risco (impacto.alerta_da_aeronave) a até ACERTO_KM
      do núcleo previsto, antes do prazo;
    - falso: o prazo venceu, a aeronave ainda é vista e não acertou;
    - indeterminado: o prazo venceu e a aeronave sumiu (pousou, saiu da cobertura...), ou o
      cálculo parou por falta de dado recente (`interromper`);
    - não previsto: entrou numa área de risco sem acerto. Inclui tempestade que se formou em
      cima dela, e a entrada numa área longe do núcleo previsto (`alerta_pendente`: o alerta
      continua valendo até o prazo).
    """
    regra: str = FABIA
    pendentes: dict[str, Conflito] = field(default_factory=dict)    # aeronave -> alerta em aberto
    em_perigo: set[str] = field(default_factory=set)
    iniciado: bool = False

    def passo(self, agora: datetime, conflitos: list[Conflito], presentes: set[str],
              em_perigo: dict[str, tuple[float, float]]) -> list[dict]:
        """`em_perigo`: aeronave -> (lat, lon), das que estão numa área de risco agora."""
        eventos = []
        if not self.iniciado:            # 1º minuto (ou depois de mudar as fontes): só aprende quem já está em risco
            self.iniciado, self.em_perigo = True, set(em_perigo)
        acertaram = set()
        for chave in sorted(set(em_perigo) & set(self.pendentes)):
            alerta = self.pendentes[chave]
            lat, lon = em_perigo[chave]
            distancia = alerta.nucleo.distancia_km(lat, lon, agora)
            if distancia <= ACERTO_KM:
                del self.pendentes[chave]
                acertaram.add(chave)
                eventos.append(self._evento(Desfecho.ACERTO, agora, alerta,
                                            antecedencia_min=round((agora - alerta.calculado).total_seconds() / 60),
                                            entrada=[round(lat, 3), round(lon, 3)], distancia_km=round(distancia)))
        for chave in sorted(set(em_perigo) - self.em_perigo - acertaram):
            lat, lon = em_perigo[chave]
            evento = {'tipo': Desfecho.NAO_PREVISTO, 'regra': self.regra, 'quando': agora.isoformat(timespec='seconds'),
                      'aeronave': chave, 'entrada': [round(lat, 3), round(lon, 3)]}
            if chave in self.pendentes:
                evento['alerta_pendente'] = True
            eventos.append(evento)
        self.em_perigo = set(em_perigo)
        for chave, alerta in list(self.pendentes.items()):
            if agora > alerta.prazo:
                del self.pendentes[chave]
                desfecho = Desfecho.FALSO if chave in presentes else Desfecho.INDETERMINADO
                eventos.append(self._evento(desfecho, agora, alerta))
        for c in conflitos:
            if c.chave not in self.pendentes and c.chave not in em_perigo:
                self.pendentes[c.chave] = c
                eventos.append(self._evento(Desfecho.ALERTA, agora, c))
        return eventos

    def interromper(self, agora: datetime, motivo: str) -> list[dict]:
        """Sem dado recente: os alertas em aberto ficam sem desfecho e o próximo passo reaprende."""
        eventos = [self._evento(Desfecho.INDETERMINADO, agora, alerta, motivo=motivo)
                   for _, alerta in sorted(self.pendentes.items())]
        self.pendentes, self.em_perigo, self.iniciado = {}, set(), False
        return eventos

    def _evento(self, tipo: Desfecho, agora: datetime, c: Conflito, **extra) -> dict:
        return _evento(tipo, agora, c, regra=self.regra, **extra)


def _evento(tipo: Desfecho, agora: datetime, c: Conflito, **extra) -> dict:
    n = c.nucleo
    return {'tipo': tipo, 'quando': agora.isoformat(timespec='seconds'), 'aeronave': c.chave,
            'indicativo': c.indicativo, 'fl': c.fl, 'minutos': c.minutos, 'confianca': c.confianca,
            'calculado': c.calculado.isoformat(timespec='seconds'),
            'de': [round(c.lat0, 3), round(c.lon0, 3)], 'encontro': [round(c.lat, 3), round(c.lon, 3)],
            'nucleo': {'fonte': n.fonte, 'intensidade': n.intensidade, 'raios': n.raios,
                       'quadro': n.quando.isoformat(timespec='minutes'), 'movimento': n.movimento,
                       'vn': round(n.vn, 3), 've': round(n.ve, 3), 'celulas': len(n.celulas)},
            **extra}


def registrar(eventos: list[dict], pasta: Path | None = None) -> None:
    """Acrescenta os eventos ao arquivo do dia (UTC). Falha de disco não derruba a previsão."""
    if not eventos:
        return
    pasta = pasta or config.PREVISAO_DIR
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / f'{eventos[0]["quando"][:10]}.jsonl', 'a', encoding='utf-8') as arquivo:
            arquivo.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in eventos)
    except OSError as erro:
        log.warning('previsão: não consegui registrar (%s)', erro)


def resumir(contagem: Counter) -> dict:
    """Taxas a partir da contagem dos desfechos (None quando não há casos)."""
    acertos, falsos, nao_previstos = contagem[Desfecho.ACERTO], contagem[Desfecho.FALSO], contagem[Desfecho.NAO_PREVISTO]

    def taxa(a: int, b: int) -> float | None:
        return round(a / (a + b), 2) if a + b else None

    return {'alertas': contagem[Desfecho.ALERTA], 'acertos': acertos, 'falsos': falsos,
            'indeterminados': contagem[Desfecho.INDETERMINADO], 'nao_previstos': nao_previstos,
            'acerto': taxa(acertos, falsos),           # dos alertas com desfecho, quantos se confirmaram
            'deteccao': taxa(acertos, nao_previstos)}  # das entradas em área de risco, quantas foram previstas


# ---------------------------------------------------------------------------
# Estado (um só para todos os usuários; calculado em impacto.vigiar)
# ---------------------------------------------------------------------------

@dataclass
class Situacao:
    nucleos: list[Nucleo] = field(default_factory=list)
    conflitos: list[Conflito] = field(default_factory=list)     # da regra FABIA: os que vão para a tela
    calculado: datetime | None = None
    pausa: str = ''                                             # por que o cálculo está parado ('' = rodando)
    fontes: frozenset = frozenset()                             # RADAR e/ou RAIOS com quadro recente
    verificadores: dict[str, Verificador] = field(
        default_factory=lambda: {r: Verificador(regra=r) for r in REGRAS})
    placar: Counter = field(default_factory=Counter)        # da regra FABIA, desde que o servidor subiu
    desde: datetime = field(default_factory=lambda: datetime.now(UTC))


situacao = Situacao()


class QuadroCelulas(NamedTuple):
    """As células de 0,1° de um quadro (radar ou raios), já calculadas por impacto.py."""
    quando: datetime
    celulas: dict[geo.Chave, int]       # radar: dBZ; raios: minutos ativa


def _grandes(lista: list[Nucleo]) -> list[Nucleo]:
    """Sem os núcleos de radar menores que NUCLEO_RADAR_MIN_CELULAS."""
    return [n for n in lista if n.fonte != RADAR or len(n.celulas) >= NUCLEO_RADAR_MIN_CELULAS]


def atualizar_nucleos(radar: QuadroCelulas | None, radar_antes: QuadroCelulas | None,
                      raios: QuadroCelulas | None, raios_antes: QuadroCelulas | None, vento=None) -> None:
    """Recalcula os núcleos e o seu movimento (a cada quadro novo de radar ou de raios).

    `vento`: função (lat, lon) -> (vn, ve) km/min, de vento.deslocamento (None = sem vento recente).
    """
    com_raios = set(raios.celulas) if raios else set()
    lista = []
    if radar:
        antes = _grandes(nucleos(radar_antes.celulas, RADAR, radar_antes.quando)) if radar_antes else []
        lista += rastrear(_grandes(nucleos(radar.celulas, RADAR, radar.quando, com_raios)), antes)
    if raios:
        antes = nucleos(raios_antes.celulas, RAIOS, raios_antes.quando) if raios_antes else []
        lista += rastrear(nucleos(raios.celulas, RAIOS, raios.quando), antes)
    situacao.nucleos = estimar(lista, vento)
    contagem = Counter(n.movimento for n in situacao.nucleos)
    log.info('previsão: %d núcleos (%d medidos, %d pelo vento, %d estimados)%s', len(situacao.nucleos),
             contagem[MEDIDO], contagem[VENTO], contagem[ESTIMADO], '' if vento else ' — sem vento recente')


def ciclo(agora: datetime, elegiveis: list, em_perigo: dict[str, tuple[float, float]],
          fontes: frozenset = frozenset({RADAR, RAIOS})) -> None:
    """A cada minuto: conflitos das aeronaves agora, desfechos dos alertas, registro.

    `em_perigo`: aeronave -> (lat, lon), das que estão numa área de risco (só de fontes recentes).
    `fontes`: as fontes com quadro recente; núcleos das outras ficam de fora.
    """
    if fontes != situacao.fontes:        # mudou o que entra no mapa de risco: reaprende quem já está nele
        for v in situacao.verificadores.values():
            v.iniciado = False
    lista = [n for n in situacao.nucleos if n.fonte in fontes]
    presentes = {a.chave for a in elegiveis}
    eventos = []
    for regra, verificador in situacao.verificadores.items():
        # Quem já está numa área de risco (mais larga que o núcleo) fica só no alerta de agora (âmbar)
        conflitos = [c for c in calcular(elegiveis, lista, agora, regra) if c.chave not in em_perigo]
        novos = verificador.passo(agora, conflitos, presentes, em_perigo)
        eventos += novos
        if regra == FABIA:
            situacao.placar.update(e['tipo'] for e in novos)
            situacao.conflitos = conflitos
    registrar(eventos)
    situacao.calculado, situacao.pausa, situacao.fontes = agora, '', fontes


def pausar(agora: datetime, motivo: str) -> None:
    """Sem quadro recente de nenhuma fonte: nada na tela e os alertas em aberto ficam sem desfecho."""
    if situacao.pausa:
        return                           # já parado
    eventos = []
    for regra, verificador in situacao.verificadores.items():
        novos = verificador.interromper(agora, motivo)
        eventos += novos
        if regra == FABIA:
            situacao.placar.update(e['tipo'] for e in novos)
    registrar(eventos)
    situacao.conflitos, situacao.pausa, situacao.fontes = [], motivo, frozenset()
    log.warning('previsão parada: %s', motivo)


# ---------------------------------------------------------------------------
# Avaliação pela linha de comando: ./visa.sh avaliar [AAAA-MM-DD ...]
# ---------------------------------------------------------------------------

ANTIGA = 'antiga'      # registros de antes de 16/11/2026: acerto = qualquer área de risco (sem `regra`)


def avaliar(pasta: Path, dias: list[str]) -> None:
    arquivos = sorted(pasta.glob('*.jsonl')) if not dias else [pasta / f'{d}.jsonl' for d in dias]
    if not arquivos:
        print(f'nenhum registro em {pasta} (o servidor grava a partir do primeiro alerta)')
        return
    total: dict[str, Counter] = {}
    for arquivo in arquivos:
        if not arquivo.exists():
            print(f'{arquivo.stem}: sem registro')
            continue
        por_regra: dict[str, Counter] = {}
        for linha in arquivo.read_text(encoding='utf-8').splitlines():
            if linha.strip():
                e = json.loads(linha)
                por_regra.setdefault(e.get('regra', ANTIGA), Counter())[e['tipo']] += 1
        print(arquivo.stem)
        for regra, contagem in _ordenadas(por_regra):
            print(f'  {regra:<12} {_linha(resumir(contagem))}')
            total.setdefault(regra, Counter()).update(contagem)
    if len(arquivos) > 1:
        print('total')
        for regra, contagem in _ordenadas(total):
            print(f'  {regra:<12} {_linha(resumir(contagem))}')
    print(f'\nfabia = o que a tela mostra; parado = o mesmo com os núcleos parados; proximidade = '
          f'núcleo a menos de {PROXIMIDADE_KM} km em qualquer rumo; antiga = regra de acerto anterior a 16/11.')


def _ordenadas(por_regra: dict[str, Counter]) -> list[tuple[str, Counter]]:
    ordem = (*REGRAS, ANTIGA)
    return sorted(por_regra.items(), key=lambda item: ordem.index(item[0]) if item[0] in ordem else len(ordem))


def _linha(r: dict) -> str:
    def pct(x: float | None) -> str:
        return '—' if x is None else f'{x:.0%}'
    return (f"{r['alertas']} alertas · {r['acertos']} acertos · {r['falsos']} falsos · "
            f"{r['indeterminados']} indeterminados · {r['nao_previstos']} não previstos · "
            f"acerto {pct(r['acerto'])} · detecção {pct(r['deteccao'])}")


if __name__ == '__main__':
    avaliar(config.PREVISAO_DIR, sys.argv[1:])
