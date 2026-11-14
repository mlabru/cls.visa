"""
FAB.ia — conflitos previstos: aeronaves em rota que vão cruzar um núcleo de tempestade
nos próximos 30 minutos.

Tudo determinístico (sem IA), em três passos:

1. **Núcleos e o seu movimento.** As células de 0,1° com eco forte (radar) ou com raios são
   agrupadas em núcleos (células vizinhas = mesmo núcleo). Cada núcleo do quadro atual é
   casado com o núcleo mais próximo do quadro anterior (10 a 40 min antes): o deslocamento
   dividido pelo tempo dá a velocidade. Núcleo sem par recebe o movimento mediano dos
   vizinhos ("estimado"); sem vizinhos, fica parado ("sem medida").
2. **Projeção.** A aeronave segue em linha reta, no rumo e na velocidade atuais; o núcleo
   segue no seu movimento, a partir da hora do quadro (que já tem alguns minutos de idade).
3. **Encontro.** Minuto a minuto, até 30 min: o primeiro instante em que a aeronave fica a
   menos de RAIO_CONFLITO_KM de uma célula do núcleo é o conflito.

Cada alerta é registrado (config.PREVISAO_DIR, um arquivo JSON Lines por dia) junto com o
que aconteceu depois — a aeronave entrou mesmo numa área de risco? — para medir acertos e
alarmes falsos desde o primeiro dia. Resumo: `./visa.sh avaliar`.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

import config
import geo

log = logging.getLogger(__name__)

GRADE = 0.1                    # graus: a mesma grade das células de raios da REDEMET
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

RADAR, RAIOS = 'radar', 'raios'
ALTA, MEDIA, BAIXA = 'alta', 'media', 'baixa'
CONFIANCAS = (ALTA, MEDIA, BAIXA)
TEXTO_CONFIANCA = {ALTA: 'confiança alta', MEDIA: 'confiança média', BAIXA: 'confiança baixa'}
COR_CONFIANCA = {ALTA: '#ef4444', MEDIA: '#f59e0b', BAIXA: '#3b82f6'}   # as cores de impacto.NIVEIS
MEDIDO, ESTIMADO, SEM_MEDIDA = 'medido', 'estimado', 'sem medida'
DIRECOES = ('N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO')


# ---------------------------------------------------------------------------
# Núcleos
# ---------------------------------------------------------------------------

Chave = tuple[int, int]


def centro(chave: Chave) -> tuple[float, float]:
    """Centro (lat, lon) da célula de 0,1°."""
    return (chave[0] + 0.5) * GRADE, (chave[1] + 0.5) * GRADE


@dataclass(frozen=True)
class Nucleo:
    fonte: str                          # RADAR (eco forte) ou RAIOS
    celulas: tuple[Chave, ...]
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
        estimado = ' (estimado)' if self.movimento == ESTIMADO else ''
        return texto + f', vai para {self.direcao} a {self.velocidade_kmh:.0f} km/h{estimado}'


def agrupar(chaves) -> list[list[Chave]]:
    """Células vizinhas (inclusive na diagonal) formam um grupo. Busca em largura, sem biblioteca."""
    restantes, grupos = set(chaves), []
    while restantes:
        fila = [restantes.pop()]
        grupo = []
        while fila:
            i, j = fila.pop()
            grupo.append((i, j))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    vizinha = (i + di, j + dj)
                    if vizinha in restantes:
                        restantes.remove(vizinha)
                        fila.append(vizinha)
        grupos.append(sorted(grupo))
    return grupos


def nucleos(celulas: dict[Chave, int], fonte: str, quando: datetime,
            com_raios: set[Chave] = frozenset()) -> list[Nucleo]:
    """Núcleos de um quadro. `celulas`: chave -> dBZ (radar) ou minutos ativa (raios)."""
    saida = []
    for grupo in agrupar(celulas):
        centros = [centro(c) for c in grupo]
        lat = sum(c[0] for c in centros) / len(centros)
        lon = sum(c[1] for c in centros) / len(centros)
        if fonte == RADAR:
            intensidade = max(celulas[c] for c in grupo)
            raios = sum(1 for i, j in grupo
                        if any((i + di, j + dj) in com_raios for di in (-1, 0, 1) for dj in (-1, 0, 1)))
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


def estimar(lista: list[Nucleo]) -> list[Nucleo]:
    """Núcleo sem movimento medido recebe a mediana dos medidos a até VIZINHANCA_KM.

    Tempestades vizinhas costumam andar juntas (o vento em altitude é o mesmo); a mediana
    ignora um par errado aqui e ali.
    """
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
    return Nucleo(n.fonte, n.celulas, n.lat, n.lon, n.intensidade, n.quando, n.raios, vn, ve, movimento)


def anterior(quadros: list, ultimo) -> object | None:
    """O quadro mais recente entre INTERVALO_MIN e INTERVALO_MAX minutos antes do último."""
    candidatos = [q for q in quadros
                  if INTERVALO_MIN <= (ultimo.quando - q.quando).total_seconds() / 60 <= INTERVALO_MAX]
    return max(candidatos, key=lambda q: q.quando) if candidatos else None


# ---------------------------------------------------------------------------
# Conflitos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
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


def conflito_da_aeronave(a, lista: list[Nucleo], agora: datetime) -> Conflito | None:
    """O primeiro encontro da aeronave com um núcleo nos próximos HORIZONTE_MIN minutos."""
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
            + len(n.celulas) * GRADE * geo.KM_POR_GRAU + RAIO_CONFLITO_KM
        if geo.distancia_km(a.lat, a.lon, n.lat, n.lon) > alcance:
            continue
        # Em vez de mover o núcleo, move-se a aeronave no sentido contrário (dá no mesmo)
        cosseno = max(math.cos(math.radians(n.lat)), 0.01)
        rlat = plat - n.vn * (idade + t) / geo.KM_POR_GRAU
        rlon = plon - n.ve * (idade + t) / (geo.KM_POR_GRAU * cosseno)
        centros = np.array([centro(c) for c in n.celulas])
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


def calcular(elegiveis: list, lista: list[Nucleo], agora: datetime) -> list[Conflito]:
    """Conflitos de todas as aeronaves elegíveis, do mais próximo ao mais distante no tempo."""
    if not lista:
        return []
    conflitos = [c for a in elegiveis if (c := conflito_da_aeronave(a, lista, agora))]
    return sorted(conflitos, key=lambda c: (c.minutos, CONFIANCAS.index(c.confianca), c.indicativo))


# ---------------------------------------------------------------------------
# Verificação: o alerta se confirmou?
# ---------------------------------------------------------------------------

@dataclass
class Alerta:
    conflito: Conflito
    prazo: datetime              # encontro previsto + TOLERANCIA_MIN


@dataclass
class Verificador:
    """Acompanha cada alerta até o desfecho, minuto a minuto.

    - acerto: a aeronave entrou numa área de risco (impacto.alerta_da_aeronave) até o prazo;
    - falso: o prazo venceu, a aeronave ainda é vista e não entrou;
    - indeterminado: o prazo venceu e a aeronave sumiu (pousou, saiu da cobertura...);
    - não previsto: entrou numa área de risco sem alerta antes. Inclui tempestade que se formou
      em cima dela (o que a extrapolação não tem como prever).
    """
    pendentes: dict[str, Alerta] = field(default_factory=dict)
    em_perigo: set[str] = field(default_factory=set)
    iniciado: bool = False

    def passo(self, agora: datetime, conflitos: list[Conflito], presentes: set[str],
              em_perigo: set[str]) -> list[dict]:
        eventos = []
        if not self.iniciado:            # 1º minuto: só aprende quem já está em área de risco
            self.iniciado, self.em_perigo = True, set(em_perigo)
        for chave in sorted(em_perigo - self.em_perigo):
            alerta = self.pendentes.pop(chave, None)
            if alerta:
                eventos.append(_evento('acerto', agora, alerta.conflito,
                                       antecedencia_min=round((agora - alerta.conflito.calculado).total_seconds() / 60)))
            else:
                eventos.append({'tipo': 'nao_previsto', 'quando': agora.isoformat(timespec='seconds'),
                                'aeronave': chave})
        self.em_perigo = set(em_perigo)
        for chave, alerta in list(self.pendentes.items()):
            if agora > alerta.prazo:
                del self.pendentes[chave]
                eventos.append(_evento('falso' if chave in presentes else 'indeterminado', agora, alerta.conflito))
        for c in conflitos:
            if c.chave not in self.pendentes and c.chave not in em_perigo:
                prazo = c.calculado + timedelta(minutes=c.minutos + TOLERANCIA_MIN)
                self.pendentes[c.chave] = Alerta(c, prazo)
                eventos.append(_evento('alerta', agora, c))
        return eventos


def _evento(tipo: str, agora: datetime, c: Conflito, **extra) -> dict:
    n = c.nucleo
    return {'tipo': tipo, 'quando': agora.isoformat(timespec='seconds'), 'aeronave': c.chave,
            'indicativo': c.indicativo, 'fl': c.fl, 'minutos': c.minutos, 'confianca': c.confianca,
            'calculado': c.calculado.isoformat(timespec='seconds'),
            'de': [round(c.lat0, 3), round(c.lon0, 3)], 'encontro': [round(c.lat, 3), round(c.lon, 3)],
            'nucleo': {'fonte': n.fonte, 'intensidade': n.intensidade, 'raios': n.raios,
                       'quadro': n.quando.isoformat(timespec='minutes'), 'movimento': n.movimento,
                       'vn': round(n.vn, 3), 've': round(n.ve, 3), 'celulas': len(n.celulas)},
            **extra}


def registrar(eventos: list[dict], pasta: Path = config.PREVISAO_DIR) -> None:
    """Acrescenta os eventos ao arquivo do dia (UTC). Falha de disco não derruba a previsão."""
    if not eventos:
        return
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / f'{eventos[0]["quando"][:10]}.jsonl', 'a', encoding='utf-8') as arquivo:
            arquivo.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in eventos)
    except OSError as erro:
        log.warning('previsão: não consegui registrar (%s)', erro)


def resumir(contagem: Counter) -> dict:
    """Taxas a partir da contagem dos desfechos (None quando não há casos)."""
    acertos, falsos, nao_previstos = contagem['acerto'], contagem['falso'], contagem['nao_previsto']

    def taxa(a: int, b: int) -> float | None:
        return round(a / (a + b), 2) if a + b else None

    return {'alertas': contagem['alerta'], 'acertos': acertos, 'falsos': falsos,
            'indeterminados': contagem['indeterminado'], 'nao_previstos': nao_previstos,
            'acerto': taxa(acertos, falsos),           # dos alertas com desfecho, quantos se confirmaram
            'deteccao': taxa(acertos, nao_previstos)}  # das entradas em área de risco, quantas foram previstas


# ---------------------------------------------------------------------------
# Estado (um só para todos os usuários; calculado em impacto.vigiar)
# ---------------------------------------------------------------------------

@dataclass
class Situacao:
    nucleos: list[Nucleo] = field(default_factory=list)
    conflitos: list[Conflito] = field(default_factory=list)
    calculado: datetime | None = None
    verificador: Verificador = field(default_factory=Verificador)
    placar: Counter = field(default_factory=Counter)        # desde que o servidor subiu
    desde: datetime = field(default_factory=lambda: datetime.now(UTC))
    versao: int = 0


situacao = Situacao()


def atualizar_nucleos(radar: tuple[datetime, dict] | None, radar_antes: tuple[datetime, dict] | None,
                      raios: tuple[datetime, dict] | None, raios_antes: tuple[datetime, dict] | None) -> None:
    """Recalcula os núcleos e o seu movimento (a cada quadro novo de radar ou de raios)."""
    com_raios = set(raios[1]) if raios else set()
    lista = []
    if radar:
        antes = nucleos(radar_antes[1], RADAR, radar_antes[0]) if radar_antes else []
        lista += rastrear(nucleos(radar[1], RADAR, radar[0], com_raios), antes)
    if raios:
        antes = nucleos(raios_antes[1], RAIOS, raios_antes[0]) if raios_antes else []
        lista += rastrear(nucleos(raios[1], RAIOS, raios[0]), antes)
    situacao.nucleos = estimar(lista)
    medidos = sum(1 for n in situacao.nucleos if n.movimento == MEDIDO)
    log.info('previsão: %d núcleos (%d com movimento medido)', len(situacao.nucleos), medidos)


def ciclo(agora: datetime, elegiveis: list, em_perigo: set[str]) -> None:
    """A cada minuto: conflitos das aeronaves agora, desfechos dos alertas, registro."""
    # Quem já está numa área de risco (mais larga que o núcleo) fica só no alerta de agora (âmbar)
    conflitos = [c for c in calcular(elegiveis, situacao.nucleos, agora) if c.chave not in em_perigo]
    eventos = situacao.verificador.passo(agora, conflitos, {a.chave for a in elegiveis}, em_perigo)
    registrar(eventos)
    situacao.placar.update(e['tipo'] for e in eventos)
    situacao.conflitos, situacao.calculado = conflitos, agora
    situacao.versao += 1


# ---------------------------------------------------------------------------
# Avaliação pela linha de comando: ./visa.sh avaliar [AAAA-MM-DD ...]
# ---------------------------------------------------------------------------

def avaliar(pasta: Path, dias: list[str]) -> None:
    arquivos = sorted(pasta.glob('*.jsonl')) if not dias else [pasta / f'{d}.jsonl' for d in dias]
    if not arquivos:
        print(f'nenhum registro em {pasta} (o servidor grava a partir do primeiro alerta)')
        return
    total = Counter()
    for arquivo in arquivos:
        if not arquivo.exists():
            print(f'{arquivo.stem}: sem registro')
            continue
        contagem = Counter(json.loads(linha)['tipo'] for linha in arquivo.read_text(encoding='utf-8').splitlines()
                           if linha.strip())
        total += contagem
        print(f'{arquivo.stem}: {_linha(resumir(contagem))}')
    if len(arquivos) > 1:
        print(f'total:      {_linha(resumir(total))}')


def _linha(r: dict) -> str:
    def pct(x: float | None) -> str:
        return '—' if x is None else f'{x:.0%}'
    return (f"{r['alertas']} alertas · {r['acertos']} acertos · {r['falsos']} falsos · "
            f"{r['indeterminados']} indeterminados · {r['nao_previstos']} não previstos · "
            f"acerto {pct(r['acerto'])} · detecção {pct(r['deteccao'])}")


if __name__ == '__main__':
    avaliar(config.PREVISAO_DIR, sys.argv[1:])
