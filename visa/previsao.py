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
que aconteceu depois — a aeronave entrou numa área de risco observada perto do ponto de
encontro PREVISTO? — para
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

Quem usa: impacto.py chama `atualizar_nucleos`, `ciclo` e `pausar`; a tela (tela/, camadas/)
lê `situacao`; `./visa.sh avaliar` roda este arquivo como script (`avaliar`).
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
POSICAO_MAX_S = 60             # posição mais velha que isso não entra (a 450 kt, 60 s ≈ 14 km)
VELOCIDADE_MAX_NUCLEO_KMH = 120    # deslocamento maior que isso entre quadros = par errado
VIZINHANCA_KM = 300            # de onde vem o movimento "estimado" de um núcleo sem par
INTERVALO_MIN, INTERVALO_MAX = 10, 40   # minutos entre o quadro atual e o anterior usado no movimento
NUCLEO_VELHO_MIN = 25          # quadro mais velho que isso: a confiança cai um nível
NUCLEO_DESCARTE_MIN = 60       # quadro mais velho que isso: núcleo ignorado
TOLERANCIA_MIN = 10            # o alerta "vence" este tempo depois do encontro previsto
# Acerto: a aeronave entra numa área de risco OBSERVADA (o mapa de risco dos quadros que chegam
# depois do alerta) a até ACERTO_KM do PONTO DE ENCONTRO que a própria regra previu. Nenhuma
# regra é julgada pelo movimento estimado: antes (até 18/11), a referência era o núcleo movido
# pela estimativa da FABIA, e uma estimativa errada era conferida contra ela mesma. A área de
# risco dos raios inclui as células vizinhas (~11 km a mais) e a aeronave a 15 km/min erra o
# ponto com 1–2 min de diferença: 30 km. Entrada longe do ponto previsto não é acerto: é
# "não previsto" (com `alerta_pendente`), e o alerta continua valendo.
ACERTO_KM = 30
VERIFICACAO = 'encontro'       # gravado em cada evento: o `avaliar` separa os registros da regra anterior
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
    """Um núcleo de tempestade: células vizinhas de um mesmo quadro, com o seu movimento.

    Atributos:
        fonte: RADAR (eco forte) ou RAIOS.
        celulas: chaves da grade de 0,1° que formam o núcleo, em ordem.
        lat, lon: centro do núcleo (média dos centros das células), em graus.
        intensidade: radar: dBZ máximo; raios: nº de células com raios.
        quando: hora do quadro (UTC).
        raios: radar: células do núcleo com raios nelas ou ao lado (raios: igual a `intensidade`).
        vn: velocidade para o norte, km/min.
        ve: velocidade para o leste, km/min.
        movimento: de onde veio a velocidade (MEDIDO, VENTO, ESTIMADO ou SEM_MEDIDA).
    """
    fonte: str
    celulas: tuple[geo.Chave, ...]
    lat: float
    lon: float
    intensidade: int
    quando: datetime
    raios: int = 0
    vn: float = 0.0
    ve: float = 0.0
    movimento: str = SEM_MEDIDA

    @property
    def velocidade_kmh(self) -> float:
        """Velocidade do núcleo, em km/h."""
        return math.hypot(self.vn, self.ve) * 60

    @property
    def direcao(self) -> str:
        """Para onde o núcleo vai, em 8 direções (L = leste, O = oeste)."""
        rumo = math.degrees(math.atan2(self.ve, self.vn)) % 360
        return DIRECOES[round(rumo / 45) % 8]

    def descricao(self) -> str:
        """Texto do núcleo para a tela: fonte, intensidade e movimento.

        Retorna:
            Por exemplo 'núcleo de 50 dBZ com raios, vai para L a 30 km/h (pelo vento)'.
        """
        if self.fonte == RADAR:
            texto = f'núcleo de {self.intensidade} dBZ' + (' com raios' if self.raios else '')
        else:
            texto = f'núcleo com raios ({self.intensidade} célula(s) de 10 km)'
        if self.movimento == SEM_MEDIDA:
            return texto + ', movimento não medido'
        if self.velocidade_kmh < 5:
            return texto + ', quase parado'
        origem = {ESTIMADO: ' (estimado)', VENTO: ' (pelo vento)'}.get(self.movimento, '')
        return texto + f', vai para {self.direcao} a {self.velocidade_kmh:.0f} km/h{origem}'

    def distancia_km(self, lat: float, lon: float, agora: datetime) -> float:
        """Distância do ponto à célula mais próxima do núcleo, movido até `agora`.

        Em vez de mover o núcleo, move o ponto no sentido contrário (dá no mesmo).

        Parâmetros:
            lat, lon: o ponto, em graus.
            agora: até quando o núcleo anda, a partir da hora do quadro.

        Retorna:
            A distância, em km.
        """
        minutos = (agora - self.quando).total_seconds() / 60
        cosseno = max(math.cos(math.radians(self.lat)), 0.01)
        lat_relativa = lat - self.vn * minutos / geo.KM_POR_GRAU
        lon_relativa = lon - self.ve * minutos / (geo.KM_POR_GRAU * cosseno)
        return min(math.hypot((lat_relativa - centro[0]) * geo.KM_POR_GRAU,
                              (lon_relativa - centro[1]) * geo.KM_POR_GRAU * cosseno)
                   for centro in map(geo.centro, self.celulas))


def agrupar(chaves) -> list[list[geo.Chave]]:
    """Junta as células vizinhas (inclusive na diagonal) em grupos.

    Busca em largura, sem biblioteca.

    Parâmetros:
        chaves: chaves da grade de 0,1° (qualquer iterável).

    Retorna:
        Um grupo (lista ordenada de chaves) por conjunto de células vizinhas.
    """
    restantes = set(chaves)
    grupos = []
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
    """Os núcleos de um quadro, ainda sem movimento.

    Parâmetros:
        celulas: chave -> dBZ (radar) ou minutos ativa (raios).
        fonte: RADAR ou RAIOS.
        quando: hora do quadro (UTC).
        com_raios: células com raios (só para o radar: conta as células com raios ao lado).

    Retorna:
        Um Nucleo por grupo de células vizinhas.
    """
    saida = []
    for grupo in agrupar(celulas):
        centros = [geo.centro(chave) for chave in grupo]
        lat = sum(centro[0] for centro in centros) / len(centros)
        lon = sum(centro[1] for centro in centros) / len(centros)
        if fonte == RADAR:
            intensidade = max(celulas[chave] for chave in grupo)
            raios = sum(1 for chave in grupo if _tem_raios_ao_lado(chave, com_raios))
        else:
            intensidade = len(grupo)
            raios = len(grupo)
        saida.append(Nucleo(fonte, tuple(grupo), lat, lon, intensidade, quando, raios))
    return saida


def _tem_raios_ao_lado(chave: geo.Chave, com_raios: set[geo.Chave]) -> bool:
    """A célula tem raios nela ou numa vizinha? (geo.vizinhas inclui a própria célula)."""
    return any(vizinha in com_raios for vizinha in geo.vizinhas(chave))


def rastrear(atuais: list[Nucleo], anteriores: list[Nucleo]) -> list[Nucleo]:
    """Mede a velocidade de cada núcleo atual pelo par mais próximo no quadro anterior.

    Parâmetros:
        atuais: núcleos do quadro atual.
        anteriores: núcleos do quadro anterior (INTERVALO_MIN a INTERVALO_MAX min antes).

    Retorna:
        Os núcleos atuais; os que têm par a menos do alcance (VELOCIDADE_MAX_NUCLEO_KMH)
        ganham o movimento MEDIDO, os outros ficam como estavam.
    """
    if not anteriores or not atuais:
        return atuais
    intervalo_min = (atuais[0].quando - anteriores[0].quando).total_seconds() / 60
    if not INTERVALO_MIN <= intervalo_min <= INTERVALO_MAX:
        return atuais
    alcance = VELOCIDADE_MAX_NUCLEO_KMH * intervalo_min / 60
    saida = []
    for nucleo in atuais:
        par = min(anteriores, key=lambda candidato: geo.distancia_km(nucleo.lat, nucleo.lon,
                                                                     candidato.lat, candidato.lon))
        if geo.distancia_km(nucleo.lat, nucleo.lon, par.lat, par.lon) > alcance:
            saida.append(nucleo)
            continue
        vn = (nucleo.lat - par.lat) * geo.KM_POR_GRAU / intervalo_min
        ve = (nucleo.lon - par.lon) * geo.KM_POR_GRAU * math.cos(math.radians(nucleo.lat)) / intervalo_min
        saida.append(_com_movimento(nucleo, vn, ve, MEDIDO))
    return saida


def estimar(lista: list[Nucleo], vento=None) -> list[Nucleo]:
    """Dá movimento aos núcleos sem par e confere os pares medidos.

    - Com vento: núcleo sem par anda com o vento; par medido que difere do vento mais que
      DIVERGENCIA_KMH é tratado como par errado (fusão, divisão, crescimento) e anda com o vento.
    - Sem vento: núcleo sem par recebe a mediana dos medidos a até VIZINHANCA_KM. Tempestades
      vizinhas costumam andar juntas; a mediana ignora um par errado aqui e ali.

    Fluxo:
        1. Com vento, aplica o vento (_aplicar_vento).
        2. Quem continua sem medida (sem vento, ou sem vento naquele ponto) recebe a mediana
           dos vizinhos medidos (_aplicar_vizinhos).

    Parâmetros:
        lista: núcleos, já rastreados.
        vento: função (lat, lon) -> (vn, ve) em km/min ou None (vento.deslocamento), ou None
            se não houver arquivo de vento recente.

    Retorna:
        Os mesmos núcleos, na mesma ordem, com o movimento final.
    """
    if vento is not None:
        lista = _aplicar_vento(lista, vento)
    return _aplicar_vizinhos(lista)


def _aplicar_vento(lista: list[Nucleo], vento) -> list[Nucleo]:
    """Põe o vento nos núcleos sem medida e nos pares medidos que divergem dele.

    Parâmetros:
        lista: núcleos.
        vento: função (lat, lon) -> (vn, ve) em km/min, ou None onde não há vento.

    Retorna:
        Os núcleos, na mesma ordem; onde a função devolve None, o núcleo fica como estava.
    """
    saida = []
    for nucleo in lista:
        pelo_vento = vento(nucleo.lat, nucleo.lon)
        if pelo_vento is None:
            saida.append(nucleo)
            continue
        diferenca_kmh = math.hypot(nucleo.vn - pelo_vento[0], nucleo.ve - pelo_vento[1]) * 60
        if nucleo.movimento == SEM_MEDIDA or diferenca_kmh > DIVERGENCIA_KMH:
            saida.append(_com_movimento(nucleo, *pelo_vento, VENTO))
        else:
            saida.append(nucleo)
    return saida


def _aplicar_vizinhos(lista: list[Nucleo]) -> list[Nucleo]:
    """Dá aos núcleos sem medida a mediana do movimento dos medidos a até VIZINHANCA_KM.

    Retorna:
        Os núcleos, na mesma ordem; sem nenhum medido perto, o núcleo continua sem medida.
    """
    medidos = [nucleo for nucleo in lista if nucleo.movimento == MEDIDO]
    saida = []
    for nucleo in lista:
        if nucleo.movimento != SEM_MEDIDA:
            saida.append(nucleo)
            continue
        perto = [medido for medido in medidos
                 if geo.distancia_km(nucleo.lat, nucleo.lon, medido.lat, medido.lon) <= VIZINHANCA_KM]
        if not perto:
            saida.append(nucleo)
            continue
        vn = statistics.median(medido.vn for medido in perto)
        ve = statistics.median(medido.ve for medido in perto)
        saida.append(_com_movimento(nucleo, vn, ve, ESTIMADO))
    return saida


def _com_movimento(nucleo: Nucleo, vn: float, ve: float, movimento: str) -> Nucleo:
    """Cópia do núcleo com outra velocidade (km/min) e a origem dela."""
    return replace(nucleo, vn=vn, ve=ve, movimento=movimento)


def anterior(quadros: list, ultimo) -> object | None:
    """Escolhe o quadro para medir o movimento.

    Parâmetros:
        quadros: quadros disponíveis (qualquer objeto com `quando`).
        ultimo: o quadro atual.

    Retorna:
        O quadro mais recente entre INTERVALO_MIN e INTERVALO_MAX minutos antes do último,
        ou None se não houver.
    """
    candidatos = []
    for quadro in quadros:
        minutos_antes = (ultimo.quando - quadro.quando).total_seconds() / 60
        if INTERVALO_MIN <= minutos_antes <= INTERVALO_MAX:
            candidatos.append(quadro)
    if not candidatos:
        return None
    return max(candidatos, key=lambda quadro: quadro.quando)


# ---------------------------------------------------------------------------
# Conflitos
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Conflito:
    """Um encontro previsto entre uma aeronave e um núcleo.

    Atributos:
        chave: da aeronave (aeronaves.Aeronave.chave).
        indicativo: indicativo de chamada da aeronave.
        fl: nível de voo no cálculo (None se desconhecido).
        minutos: até o encontro, contados de `calculado`.
        lat, lon: ponto de encontro: onde a aeronave estará (PROXIMIDADE: a célula do núcleo
            mais perto dela). É a referência do acerto.
        lat0, lon0: onde a aeronave estava no cálculo.
        nucleo: o núcleo do encontro (com o movimento dele, mesmo na regra PARADO).
        confianca: ALTA, MEDIA ou BAIXA.
        calculado: quando o conflito foi calculado (UTC).
    """
    chave: str
    indicativo: str
    fl: int | None
    minutos: int
    lat: float
    lon: float
    lat0: float
    lon0: float
    nucleo: Nucleo
    confianca: str
    calculado: datetime

    @property
    def prazo(self) -> datetime:
        """Até quando a aeronave pode entrar na área de risco para o alerta contar como acerto."""
        return self.calculado + timedelta(minutes=self.minutos + TOLERANCIA_MIN)


def para_a_tela(conflitos: list[Conflito], mostrar_baixa: bool) -> list[Conflito]:
    """Filtra os conflitos que a pessoa quer ver.

    Parâmetros:
        conflitos: os conflitos da regra FABIA.
        mostrar_baixa: preferência fabia_baixa (mostrar também os de confiança baixa).

    Retorna:
        Todos, ou só os de confiança alta e média.
    """
    if mostrar_baixa:
        return conflitos
    return [conflito for conflito in conflitos if conflito.confianca != BAIXA]


def posicao_recente(aeronave, relogio: float) -> bool:
    """A posição da aeronave é de agora?

    Sem isto, uma aeronave que saiu da cobertura (e fica até AERONAVE_EXPIRA_S no tráfego)
    seria projetada da última posição como se fosse a atual: a 450 kt, 5 min são ~70 km.

    Parâmetros:
        aeronave: objeto com `visto_em`, em time.monotonic().
        relogio: agora, em time.monotonic().

    Retorna:
        True se a posição tem até POSICAO_MAX_S segundos.
    """
    return relogio - aeronave.visto_em <= POSICAO_MAX_S


def em_rota(aeronave) -> bool:
    """A aeronave entra na previsão? (em rota: nível e velocidade conhecidos e altos o bastante).

    Parâmetros:
        aeronave: objeto com `nivel_voo` e `velocidade_kt` (podem ser None).
    """
    nivel = aeronave.nivel_voo
    velocidade = aeronave.velocidade_kt
    return nivel is not None and nivel >= FL_MIN and velocidade is not None and velocidade >= VELOCIDADE_MIN_KT


def confianca(minutos: int, movimento: str, idade_min: float) -> str:
    """Confiança de um conflito: cai com a antecedência, com o movimento não medido e com o quadro velho.

    Parâmetros:
        minutos: até o encontro.
        movimento: origem do movimento do núcleo (MEDIDO, VENTO, ESTIMADO, SEM_MEDIDA).
        idade_min: idade do quadro do núcleo, em minutos.

    Retorna:
        ALTA, MEDIA ou BAIXA.
    """
    if minutos <= 10:
        nivel = 0
    elif minutos <= 20:
        nivel = 1
    else:
        nivel = 2
    if movimento != MEDIDO:
        nivel += 1
    if idade_min > NUCLEO_VELHO_MIN:
        nivel += 1
    return CONFIANCAS[min(nivel, 2)]


def conflito_da_aeronave(aeronave, lista: list[Nucleo], agora: datetime, parado: bool = False) -> Conflito | None:
    """O primeiro encontro da aeronave com um núcleo nos próximos HORIZONTE_MIN minutos.

    Fluxo:
        1. Projeta a aeronave em linha reta, minuto a minuto (minuto 0 = agora).
        2. Para cada núcleo recente e ao alcance, procura o primeiro minuto de encontro
           (_primeiro_encontro).
        3. Fica com o encontro mais cedo.

    Parâmetros:
        aeronave: a aeronave (aeronaves.Aeronave ou objeto com chave, indicativo, nivel_voo,
            velocidade_kt, lat, lon e rumo).
        lista: os núcleos.
        agora: hora do cálculo (UTC).
        parado: projeta como se os núcleos não andassem (regra PARADO, só para comparação). O
            Conflito guarda o núcleo com o movimento dele, para o acerto ser conferido igual nas regras.

    Retorna:
        O Conflito, ou None se não houver encontro (ou se a aeronave já está num núcleo).
    """
    minutos = np.arange(HORIZONTE_MIN + 1)
    km_por_min = aeronave.velocidade_kt * 1.852 / 60
    trajeto_lat, trajeto_lon = geo.deslocar(aeronave.lat, aeronave.lon, aeronave.rumo, km_por_min * minutos)
    melhor = None
    for nucleo in lista:
        idade = (agora - nucleo.quando).total_seconds() / 60
        if idade > NUCLEO_DESCARTE_MIN:
            continue
        if geo.distancia_km(aeronave.lat, aeronave.lon, nucleo.lat, nucleo.lon) > _alcance_km(km_por_min, nucleo, idade):
            continue
        minuto = _primeiro_encontro(nucleo, idade, minutos, trajeto_lat, trajeto_lon, parado)
        if minuto is None:
            continue
        if melhor is None or minuto < melhor.minutos:
            melhor = Conflito(aeronave.chave, aeronave.indicativo, aeronave.nivel_voo, minuto,
                              float(trajeto_lat[minuto]), float(trajeto_lon[minuto]), aeronave.lat, aeronave.lon,
                              nucleo, confianca(minuto, nucleo.movimento, idade), agora)
    return melhor


def _alcance_km(km_por_min: float, nucleo: Nucleo, idade: float) -> float:
    """Distância máxima para haver encontro (filtro rápido antes da conta minuto a minuto).

    Soma o que a aeronave anda no horizonte, o que o núcleo anda (desde o quadro até o fim do
    horizonte), o tamanho do núcleo e o raio do conflito.

    Parâmetros:
        km_por_min: velocidade da aeronave.
        nucleo: o núcleo.
        idade: idade do quadro do núcleo, em minutos.

    Retorna:
        A distância, em km, entre a aeronave e o centro do núcleo além da qual não há encontro.
    """
    return km_por_min * HORIZONTE_MIN + math.hypot(nucleo.vn, nucleo.ve) * (idade + HORIZONTE_MIN) \
        + len(nucleo.celulas) * geo.GRADE * geo.KM_POR_GRAU + RAIO_CONFLITO_KM


def _primeiro_encontro(nucleo: Nucleo, idade: float, minutos: np.ndarray,
                       trajeto_lat: np.ndarray, trajeto_lon: np.ndarray, parado: bool) -> int | None:
    """O primeiro minuto em que a aeronave fica a até RAIO_CONFLITO_KM de uma célula do núcleo.

    Em vez de mover o núcleo, move-se a aeronave no sentido contrário (dá no mesmo). Conta
    vetorizada: uma linha por minuto, uma coluna por célula.

    Parâmetros:
        nucleo: o núcleo.
        idade: idade do quadro do núcleo, em minutos (o núcleo já andou esse tempo).
        minutos: 0, 1, ..., HORIZONTE_MIN.
        trajeto_lat, trajeto_lon: posição da aeronave em cada minuto, em graus.
        parado: ignora o movimento do núcleo (regra PARADO).

    Retorna:
        O minuto do encontro, ou None se não houver ou se a aeronave já está no núcleo
        (minuto 0: é o alerta de agora, âmbar, não previsão).
    """
    cosseno = max(math.cos(math.radians(nucleo.lat)), 0.01)
    if parado:
        vn, ve = 0.0, 0.0
    else:
        vn, ve = nucleo.vn, nucleo.ve
    lat_relativa = trajeto_lat - vn * (idade + minutos) / geo.KM_POR_GRAU
    lon_relativa = trajeto_lon - ve * (idade + minutos) / (geo.KM_POR_GRAU * cosseno)
    centros = np.array([geo.centro(chave) for chave in nucleo.celulas])
    distancia_norte = (lat_relativa[:, None] - centros[None, :, 0]) * geo.KM_POR_GRAU
    distancia_leste = (lon_relativa[:, None] - centros[None, :, 1]) * geo.KM_POR_GRAU * cosseno
    menor_distancia = np.hypot(distancia_leste, distancia_norte).min(axis=1)   # por minuto
    if menor_distancia[0] <= RAIO_CONFLITO_KM:
        return None
    encontros = np.nonzero(menor_distancia <= RAIO_CONFLITO_KM)[0]
    if not encontros.size:
        return None
    return int(minutos[encontros[0]])


def proximidade(aeronave, lista: list[Nucleo], agora: datetime) -> Conflito | None:
    """Regra PROXIMIDADE (só para comparação): núcleo a menos de PROXIMIDADE_KM, em qualquer rumo.

    Sem rumo não há ponto de encontro: o "encontro" é a célula do núcleo (onde foi observada)
    mais perto da aeronave, e o prazo é o horizonte inteiro (HORIZONTE_MIN).

    Parâmetros:
        aeronave: a aeronave (como em conflito_da_aeronave).
        lista: os núcleos.
        agora: hora do cálculo (UTC).

    Retorna:
        O Conflito com o núcleo mais perto (confiança BAIXA), ou None se nenhum estiver perto
        (ou se a aeronave já está dentro dele).
    """
    melhor = None
    menor = PROXIMIDADE_KM
    for nucleo in lista:
        if (agora - nucleo.quando).total_seconds() / 60 > NUCLEO_DESCARTE_MIN:
            continue
        alcance = PROXIMIDADE_KM + len(nucleo.celulas) * geo.GRADE * geo.KM_POR_GRAU
        if geo.distancia_km(aeronave.lat, aeronave.lon, nucleo.lat, nucleo.lon) > alcance:
            continue                                         # filtro rápido
        distancia = nucleo.distancia_km(aeronave.lat, aeronave.lon, agora)
        if RAIO_CONFLITO_KM < distancia <= menor:            # já dentro: é o alerta de agora
            melhor = nucleo
            menor = distancia
    if melhor is None:
        return None
    lat, lon = min(map(geo.centro, melhor.celulas),
                   key=lambda centro: geo.distancia_km(aeronave.lat, aeronave.lon, *centro))
    return Conflito(aeronave.chave, aeronave.indicativo, aeronave.nivel_voo, HORIZONTE_MIN, lat, lon,
                    aeronave.lat, aeronave.lon, melhor, BAIXA, agora)


def calcular(elegiveis: list, lista: list[Nucleo], agora: datetime, regra: str = FABIA) -> list[Conflito]:
    """Conflitos de todas as aeronaves elegíveis, segundo uma regra.

    Parâmetros:
        elegiveis: aeronaves em rota e com posição recente.
        lista: os núcleos.
        agora: hora do cálculo (UTC).
        regra: FABIA, PARADO ou PROXIMIDADE.

    Retorna:
        No máximo um conflito por aeronave, do mais próximo ao mais distante no tempo.
    """
    if not lista:
        return []
    conflitos = []
    for aeronave in elegiveis:
        if regra == PROXIMIDADE:
            conflito = proximidade(aeronave, lista, agora)
        else:
            conflito = conflito_da_aeronave(aeronave, lista, agora, regra == PARADO)
        if conflito is not None:
            conflitos.append(conflito)
    return sorted(conflitos, key=lambda conflito: (conflito.minutos, CONFIANCAS.index(conflito.confianca),
                                                   conflito.indicativo))


# ---------------------------------------------------------------------------
# Verificação: o alerta se confirmou?
# ---------------------------------------------------------------------------

@dataclass
class Verificador:
    """Acompanha cada alerta de uma regra até o desfecho, minuto a minuto.

    - acerto: a aeronave está numa área de risco (impacto.alerta_da_aeronave) a até ACERTO_KM
      do ponto de encontro previsto (Conflito.lat/lon), antes do prazo;
    - falso: o prazo venceu, a aeronave ainda é vista e não acertou;
    - indeterminado: o prazo venceu e a aeronave sumiu (pousou, saiu da cobertura...), ou o
      cálculo parou por falta de dado recente (`interromper`);
    - não previsto: entrou numa área de risco sem acerto. Inclui tempestade que se formou em
      cima dela, e a entrada numa área longe do ponto previsto (`alerta_pendente`: o alerta
      continua valendo até o prazo).
    O acerto guarda também `distancia_nucleo_km` (até o núcleo movido pela estimativa), só
    como informação: a decisão não depende do movimento estimado.

    Atributos:
        regra: a regra conferida (FABIA, PARADO ou PROXIMIDADE).
        pendentes: aeronave -> alerta em aberto.
        em_perigo: aeronaves que estavam numa área de risco no passo anterior.
        iniciado: False no 1º passo (ou depois de mudar as fontes): o passo só aprende quem
            já está em risco, sem contar "entradas".
    """
    regra: str = FABIA
    pendentes: dict[str, Conflito] = field(default_factory=dict)
    em_perigo: set[str] = field(default_factory=set)
    iniciado: bool = False

    def passo(self, agora: datetime, conflitos: list[Conflito], presentes: set[str],
              em_perigo: dict[str, tuple[float, float]]) -> list[dict]:
        """Um minuto de verificação: desfechos dos alertas em aberto e alertas novos.

        Fluxo (a ordem importa):
            1. acertos: entrou numa área de risco perto do ponto previsto;
            2. não previstos: entrou numa área de risco sem acerto;
            3. vencidos: o prazo passou (falso ou indeterminado);
            4. alertas novos: conflitos de aeronaves sem alerta em aberto e fora de área de risco.

        Parâmetros:
            agora: hora do passo (UTC).
            conflitos: os conflitos desta regra agora.
            presentes: aeronaves elegíveis agora (as outras "sumiram").
            em_perigo: aeronave -> (lat, lon), das que estão numa área de risco agora.

        Retorna:
            Os eventos do passo, para o registro.
        """
        if not self.iniciado:
            self.iniciado = True
            self.em_perigo = set(em_perigo)
        eventos, acertaram = self._acertos(agora, em_perigo)
        eventos += self._nao_previstos(agora, em_perigo, acertaram)
        self.em_perigo = set(em_perigo)
        eventos += self._vencidos(agora, presentes)
        eventos += self._novos_alertas(agora, conflitos, em_perigo)
        return eventos

    def _acertos(self, agora: datetime, em_perigo: dict[str, tuple[float, float]]) -> tuple[list[dict], set[str]]:
        """Fecha como acerto os alertas cuja aeronave entrou em risco a até ACERTO_KM do ponto previsto.

        Retorna:
            Os eventos de acerto e as aeronaves que acertaram.
        """
        eventos = []
        acertaram = set()
        for chave in sorted(set(em_perigo) & set(self.pendentes)):
            alerta = self.pendentes[chave]
            lat, lon = em_perigo[chave]
            distancia = geo.distancia_km(lat, lon, alerta.lat, alerta.lon)
            if distancia > ACERTO_KM:
                continue
            del self.pendentes[chave]
            acertaram.add(chave)
            antecedencia_min = round((agora - alerta.calculado).total_seconds() / 60)
            distancia_nucleo = alerta.nucleo.distancia_km(lat, lon, agora)
            eventos.append(self._evento(Desfecho.ACERTO, agora, alerta,
                                        antecedencia_min=antecedencia_min,
                                        entrada=[round(lat, 3), round(lon, 3)], distancia_km=round(distancia),
                                        distancia_nucleo_km=round(distancia_nucleo)))
        return eventos, acertaram

    def _nao_previstos(self, agora: datetime, em_perigo: dict[str, tuple[float, float]],
                       acertaram: set[str]) -> list[dict]:
        """Eventos das aeronaves que acabaram de entrar numa área de risco sem acerto.

        Com alerta em aberto (entrou longe do ponto previsto), o evento leva `alerta_pendente`
        e o alerta continua valendo.
        """
        eventos = []
        for chave in sorted(set(em_perigo) - self.em_perigo - acertaram):
            lat, lon = em_perigo[chave]
            evento = {'tipo': Desfecho.NAO_PREVISTO, 'regra': self.regra, 'verificacao': VERIFICACAO,
                      'quando': agora.isoformat(timespec='seconds'),
                      'aeronave': chave, 'entrada': [round(lat, 3), round(lon, 3)]}
            if chave in self.pendentes:
                evento['alerta_pendente'] = True
            eventos.append(evento)
        return eventos

    def _vencidos(self, agora: datetime, presentes: set[str]) -> list[dict]:
        """Fecha os alertas com o prazo vencido: falso se a aeronave ainda é vista, senão indeterminado."""
        eventos = []
        for chave, alerta in list(self.pendentes.items()):
            if agora <= alerta.prazo:
                continue
            del self.pendentes[chave]
            desfecho = Desfecho.FALSO if chave in presentes else Desfecho.INDETERMINADO
            eventos.append(self._evento(desfecho, agora, alerta))
        return eventos

    def _novos_alertas(self, agora: datetime, conflitos: list[Conflito],
                       em_perigo: dict[str, tuple[float, float]]) -> list[dict]:
        """Abre um alerta para cada conflito de aeronave sem alerta em aberto e fora de área de risco."""
        eventos = []
        for conflito in conflitos:
            if conflito.chave in self.pendentes or conflito.chave in em_perigo:
                continue
            self.pendentes[conflito.chave] = conflito
            eventos.append(self._evento(Desfecho.ALERTA, agora, conflito))
        return eventos

    def interromper(self, agora: datetime, motivo: str) -> list[dict]:
        """Sem dado recente: os alertas em aberto ficam sem desfecho e o próximo passo reaprende.

        Parâmetros:
            agora: hora da interrupção (UTC).
            motivo: texto gravado em cada evento.

        Retorna:
            Um evento INDETERMINADO por alerta em aberto.
        """
        eventos = [self._evento(Desfecho.INDETERMINADO, agora, alerta, motivo=motivo)
                   for _, alerta in sorted(self.pendentes.items())]
        self.pendentes = {}
        self.em_perigo = set()
        self.iniciado = False
        return eventos

    def _evento(self, tipo: Desfecho, agora: datetime, conflito: Conflito, **extra) -> dict:
        """Evento de um alerta, marcado com a regra e o critério de verificação atual."""
        return _evento(tipo, agora, conflito, regra=self.regra, verificacao=VERIFICACAO, **extra)


def _evento(tipo: Desfecho, agora: datetime, conflito: Conflito, **extra) -> dict:
    """Monta o registro (JSON) de um evento ligado a um alerta.

    Os nomes e a ordem dos campos são os do arquivo JSONL: o `avaliar` e a avaliação em
    produção dependem deles.

    Parâmetros:
        tipo: o desfecho (ou ALERTA).
        agora: hora do evento (UTC).
        conflito: o alerta.
        extra: campos a mais (regra, verificacao, motivo, entrada...).
    """
    nucleo = conflito.nucleo
    return {'tipo': tipo, 'quando': agora.isoformat(timespec='seconds'), 'aeronave': conflito.chave,
            'indicativo': conflito.indicativo, 'fl': conflito.fl, 'minutos': conflito.minutos,
            'confianca': conflito.confianca, 'calculado': conflito.calculado.isoformat(timespec='seconds'),
            'de': [round(conflito.lat0, 3), round(conflito.lon0, 3)],
            'encontro': [round(conflito.lat, 3), round(conflito.lon, 3)],
            'nucleo': {'fonte': nucleo.fonte, 'intensidade': nucleo.intensidade, 'raios': nucleo.raios,
                       'quadro': nucleo.quando.isoformat(timespec='minutes'), 'movimento': nucleo.movimento,
                       'vn': round(nucleo.vn, 3), 've': round(nucleo.ve, 3), 'celulas': len(nucleo.celulas)},
            **extra}


def registrar(eventos: list[dict], pasta: Path | None = None) -> None:
    """Acrescenta os eventos ao arquivo do dia (UTC). Falha de disco não derruba a previsão.

    Parâmetros:
        eventos: os eventos (o dia vem do 'quando' do primeiro).
        pasta: onde gravar (None = config.PREVISAO_DIR).
    """
    if not eventos:
        return
    pasta = pasta or config.PREVISAO_DIR
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / f'{eventos[0]["quando"][:10]}.jsonl', 'a', encoding='utf-8') as arquivo:
            arquivo.writelines(json.dumps(evento, ensure_ascii=False) + '\n' for evento in eventos)
    except OSError as erro:
        log.warning('previsão: não consegui registrar (%s)', erro)


COM_ALERTA = 'nao_previsto_com_alerta'   # chave extra na contagem do avaliar: não previsto com alerta_pendente


def _taxa(favoraveis: int, contrarios: int) -> float | None:
    """Fração de favoráveis no total, com 2 casas (None quando não há casos)."""
    if not favoraveis + contrarios:
        return None
    return round(favoraveis / (favoraveis + contrarios), 2)


def resumir(contagem: Counter) -> dict:
    """Taxas a partir da contagem dos desfechos (None quando não há casos).

    Os não previstos se dividem em `com_alerta` (havia um alerta em aberto para a aeronave, mas
    ela entrou na área de risco longe do ponto previsto: aviso certo, lugar errado) e `sem_aviso`.
    `aviso` conta os dois primeiros como avisados; `deteccao`, só os acertos.

    Parâmetros:
        contagem: tipo do evento (Desfecho ou COM_ALERTA) -> quantidade.

    Retorna:
        Dicionário com as contagens e as taxas `acerto`, `deteccao` e `aviso` (0 a 1).
    """
    acertos = contagem[Desfecho.ACERTO]
    falsos = contagem[Desfecho.FALSO]
    nao_previstos = contagem[Desfecho.NAO_PREVISTO]
    com_alerta = contagem[COM_ALERTA]
    return {'alertas': contagem[Desfecho.ALERTA], 'acertos': acertos, 'falsos': falsos,
            'indeterminados': contagem[Desfecho.INDETERMINADO], 'nao_previstos': nao_previstos,
            'com_alerta': com_alerta, 'sem_aviso': nao_previstos - com_alerta,
            'acerto': _taxa(acertos, falsos),            # dos alertas com desfecho, quantos se confirmaram
            'deteccao': _taxa(acertos, nao_previstos),   # das entradas em área de risco, quantas foram previstas
            'aviso': _taxa(acertos + com_alerta, nao_previstos - com_alerta)}   # ... quantas tinham algum alerta


# ---------------------------------------------------------------------------
# Estado (um só para todos os usuários; calculado em impacto.vigiar)
# ---------------------------------------------------------------------------

def _um_verificador_por_regra() -> dict[str, Verificador]:
    """Um Verificador novo para cada regra de REGRAS."""
    return {regra: Verificador(regra=regra) for regra in REGRAS}


def _agora_utc() -> datetime:
    """Hora atual em UTC."""
    return datetime.now(UTC)


@dataclass
class Situacao:
    """O estado do FAB.ia que a tela lê.

    Atributos:
        nucleos: os núcleos atuais, com movimento.
        conflitos: da regra FABIA: os que vão para a tela.
        calculado: hora do último ciclo (None = ainda não calculou).
        pausa: por que o cálculo está parado ('' = rodando).
        fontes: RADAR e/ou RAIOS com quadro recente.
        verificadores: regra -> Verificador.
        placar: contagem dos eventos da regra FABIA, desde que o servidor subiu.
        desde: quando o servidor subiu (UTC).
    """
    nucleos: list[Nucleo] = field(default_factory=list)
    conflitos: list[Conflito] = field(default_factory=list)
    calculado: datetime | None = None
    pausa: str = ''
    fontes: frozenset = frozenset()
    verificadores: dict[str, Verificador] = field(default_factory=_um_verificador_por_regra)
    placar: Counter = field(default_factory=Counter)
    desde: datetime = field(default_factory=_agora_utc)


situacao = Situacao()


class QuadroCelulas(NamedTuple):
    """As células de 0,1° de um quadro (radar ou raios), já calculadas por impacto.py.

    Atributos:
        quando: hora do quadro (UTC).
        celulas: chave -> dBZ (radar) ou minutos ativa (raios).
    """
    quando: datetime
    celulas: dict[geo.Chave, int]


def _grandes(lista: list[Nucleo]) -> list[Nucleo]:
    """Sem os núcleos de radar menores que NUCLEO_RADAR_MIN_CELULAS."""
    return [nucleo for nucleo in lista if nucleo.fonte != RADAR or len(nucleo.celulas) >= NUCLEO_RADAR_MIN_CELULAS]


def atualizar_nucleos(radar: QuadroCelulas | None, radar_antes: QuadroCelulas | None,
                      raios: QuadroCelulas | None, raios_antes: QuadroCelulas | None, vento=None) -> None:
    """Recalcula os núcleos e o seu movimento (a cada quadro novo de radar ou de raios).

    Parâmetros:
        radar, raios: o quadro atual de cada fonte (None = sem quadro).
        radar_antes, raios_antes: o quadro anterior de cada fonte, para medir o movimento
            (previsao.anterior; None = sem quadro no intervalo).
        vento: função (lat, lon) -> (vn, ve) km/min, de vento.deslocamento (None = sem vento recente).
    """
    com_raios = set(raios.celulas) if raios else set()
    lista = []
    if radar:
        nucleos_antes = []
        if radar_antes:
            nucleos_antes = _grandes(nucleos(radar_antes.celulas, RADAR, radar_antes.quando))
        lista += rastrear(_grandes(nucleos(radar.celulas, RADAR, radar.quando, com_raios)), nucleos_antes)
    if raios:
        nucleos_antes = []
        if raios_antes:
            nucleos_antes = nucleos(raios_antes.celulas, RAIOS, raios_antes.quando)
        lista += rastrear(nucleos(raios.celulas, RAIOS, raios.quando), nucleos_antes)
    situacao.nucleos = estimar(lista, vento)
    contagem = Counter(nucleo.movimento for nucleo in situacao.nucleos)
    log.info('previsão: %d núcleos (%d medidos, %d pelo vento, %d estimados)%s', len(situacao.nucleos),
             contagem[MEDIDO], contagem[VENTO], contagem[ESTIMADO], '' if vento else ' — sem vento recente')


def ciclo(agora: datetime, elegiveis: list, em_perigo: dict[str, tuple[float, float]],
          fontes: frozenset = frozenset({RADAR, RAIOS})) -> None:
    """A cada minuto: conflitos das aeronaves agora, desfechos dos alertas, registro.

    Parâmetros:
        agora: hora do ciclo (UTC).
        elegiveis: aeronaves em rota e com posição recente.
        em_perigo: aeronave -> (lat, lon), das que estão numa área de risco (só de fontes recentes).
        fontes: as fontes com quadro recente; núcleos das outras ficam de fora.
    """
    if fontes != situacao.fontes:        # mudou o que entra no mapa de risco: reaprende quem já está nele
        for verificador in situacao.verificadores.values():
            verificador.iniciado = False
    lista = [nucleo for nucleo in situacao.nucleos if nucleo.fonte in fontes]
    presentes = {aeronave.chave for aeronave in elegiveis}
    eventos = []
    for regra, verificador in situacao.verificadores.items():
        # Quem já está numa área de risco (mais larga que o núcleo) fica só no alerta de agora (âmbar)
        conflitos = [conflito for conflito in calcular(elegiveis, lista, agora, regra)
                     if conflito.chave not in em_perigo]
        novos = verificador.passo(agora, conflitos, presentes, em_perigo)
        eventos += novos
        if regra == FABIA:
            situacao.placar.update(evento['tipo'] for evento in novos)
            situacao.conflitos = conflitos
    registrar(eventos)
    situacao.calculado = agora
    situacao.pausa = ''
    situacao.fontes = fontes


def pausar(agora: datetime, motivo: str) -> None:
    """Sem quadro recente de nenhuma fonte: nada na tela e os alertas em aberto ficam sem desfecho.

    Parâmetros:
        agora: hora da pausa (UTC).
        motivo: por que parou (aparece na tela e no registro).
    """
    if situacao.pausa:
        return                           # já parado
    eventos = []
    for regra, verificador in situacao.verificadores.items():
        novos = verificador.interromper(agora, motivo)
        eventos += novos
        if regra == FABIA:
            situacao.placar.update(evento['tipo'] for evento in novos)
    registrar(eventos)
    situacao.conflitos = []
    situacao.pausa = motivo
    situacao.fontes = frozenset()
    log.warning('previsão parada: %s', motivo)


# ---------------------------------------------------------------------------
# Avaliação pela linha de comando: ./visa.sh avaliar [AAAA-MM-DD ...]
# ---------------------------------------------------------------------------

ANTIGA = 'antiga'      # registros de antes de 16/11/2026: acerto = qualquer área de risco (sem `regra`)
NUCLEO = '·núcleo'     # registros de 16 a 18/11/2026: acerto conferido contra o núcleo movido (sem `verificacao`)
ANTECEDENCIAS = ((0, 5), (6, 10), (11, 20), (21, HORIZONTE_MIN))    # faixas de minutos até o encontro
NA_TELA = 'na tela (alta e média)'     # linha extra da tabela por confiança


def rotulo(e: dict) -> str:
    """A regra do evento, marcada pelo critério de acerto com que foi conferida.

    Parâmetros:
        e: um evento do registro.

    Retorna:
        A regra ('fabia'...), a regra + NUCLEO (registros de 16 a 18/11) ou ANTIGA.
    """
    regra = e.get('regra')
    if regra is None:
        return ANTIGA
    if e.get('verificacao') == VERIFICACAO:
        return regra
    return regra + NUCLEO


def faixa(minutos: int) -> str:
    """A faixa de antecedência (ANTECEDENCIAS) dos minutos até o encontro, como '6–10 min'."""
    return next(f'{inicio}–{fim} min' for inicio, fim in ANTECEDENCIAS if minutos <= fim)


def avaliar(pasta: Path, dias: list[str]) -> None:
    """Imprime o relatório de `./visa.sh avaliar`: acertos e alarmes falsos de cada regra.

    Fluxo:
        1. Para cada dia: conta os eventos por regra e imprime uma linha por regra.
        2. Com mais de um dia: imprime o total.
        3. Imprime as tabelas de acerto por confiança e por antecedência, regras lado a lado.
        4. Imprime a legenda.

    Parâmetros:
        pasta: onde estão os arquivos AAAA-MM-DD.jsonl.
        dias: dias pedidos ('AAAA-MM-DD'); vazio = todos os da pasta.
    """
    if dias:
        arquivos = [pasta / f'{dia}.jsonl' for dia in dias]
    else:
        arquivos = sorted(pasta.glob('*.jsonl'))
    if not arquivos:
        print(f'nenhum registro em {pasta} (o servidor grava a partir do primeiro alerta)')
        return
    total: dict[str, Counter] = {}
    # (quadro, linha, regra) -> acertos e falsos, para as tabelas por confiança e antecedência
    detalhe: dict[tuple[str, str, str], Counter] = {}
    for arquivo in arquivos:
        if not arquivo.exists():
            print(f'{arquivo.stem}: sem registro')
            continue
        por_regra = _contar_arquivo(arquivo, detalhe)
        _imprimir_contagens(arquivo.stem, por_regra)
        for regra, contagem in _ordenadas(por_regra):
            total.setdefault(regra, Counter()).update(contagem)
    if len(arquivos) > 1:
        _imprimir_contagens('total', total)
    regras = [regra for regra, _ in _ordenadas(total) if regra != ANTIGA]
    for quadro in ('confiança', 'antecedência'):
        _imprimir_tabela(quadro, regras, detalhe)
    _imprimir_legenda()


def _contar_arquivo(arquivo: Path, detalhe: dict[tuple[str, str, str], Counter]) -> dict[str, Counter]:
    """Conta os eventos de um arquivo do registro, por regra.

    Parâmetros:
        arquivo: um arquivo AAAA-MM-DD.jsonl.
        detalhe: (quadro, linha, regra) -> Counter; recebe os acertos e falsos (alterado aqui).

    Retorna:
        regra (rotulo) -> contagem dos tipos de evento (e de COM_ALERTA).
    """
    por_regra: dict[str, Counter] = {}
    for linha in arquivo.read_text(encoding='utf-8').splitlines():
        if not linha.strip():
            continue
        evento = json.loads(linha)
        regra = rotulo(evento)
        tipo = evento['tipo']
        contagem = por_regra.setdefault(regra, Counter())
        contagem[tipo] += 1
        if tipo == Desfecho.NAO_PREVISTO and evento.get('alerta_pendente'):
            contagem[COM_ALERTA] += 1
        if tipo in (Desfecho.ACERTO, Desfecho.FALSO) and 'confianca' in evento:
            for quadro, nome in _linhas_do_detalhe(evento):
                detalhe.setdefault((quadro, nome, regra), Counter())[tipo] += 1
    return por_regra


def _linhas_do_detalhe(evento: dict) -> list[tuple[str, str]]:
    """Em que linhas das tabelas (quadro, linha) um acerto ou falso entra.

    Retorna:
        A linha da confiança, a da antecedência e, se apareceu na tela, a linha NA_TELA.
    """
    linhas = [('confiança', TEXTO_CONFIANCA.get(evento['confianca'], evento['confianca'])),
              ('antecedência', faixa(evento['minutos']))]
    if evento['confianca'] != BAIXA:
        linhas.append(('confiança', NA_TELA))
    return linhas


def _imprimir_contagens(titulo: str, por_regra: dict[str, Counter]) -> None:
    """Imprime o título (o dia ou 'total') e uma linha de resumo por regra."""
    print(titulo)
    for regra, contagem in _ordenadas(por_regra):
        print(f'  {regra:<18} {_linha(resumir(contagem))}')


def _imprimir_tabela(quadro: str, regras: list[str], detalhe: dict[tuple[str, str, str], Counter]) -> None:
    """Imprime uma tabela de acerto (por confiança ou por antecedência), uma coluna por regra.

    Parâmetros:
        quadro: 'confiança' ou 'antecedência'.
        regras: as colunas, na ordem.
        detalhe: (quadro, linha, regra) -> acertos e falsos.
    """
    chaves_do_quadro = [chave for chave in sorted(detalhe, key=_ordem_detalhe) if chave[0] == quadro]
    nomes = list(dict.fromkeys(nome for _, nome, _ in chaves_do_quadro))
    if not nomes:
        return
    print(f'\nacerto por {quadro}'.ljust(26) + ''.join(f'{regra:>20}' for regra in regras))
    for nome in nomes:
        celulas = [_celula_de_acerto(detalhe.get((quadro, nome, regra), Counter())) for regra in regras]
        print(f'  {nome:<23}' + ''.join(f'{celula:>20}' for celula in celulas))


def _celula_de_acerto(contagem: Counter) -> str:
    """Texto de uma célula da tabela: '62% de 701', ou '—' sem casos."""
    acertos = contagem[Desfecho.ACERTO]
    falsos = contagem[Desfecho.FALSO]
    casos = acertos + falsos
    if not casos:
        return '—'
    return f'{_taxa(acertos, falsos):.0%} de {casos}'


def _imprimir_legenda() -> None:
    """Imprime o que significa cada regra e cada taxa do relatório."""
    print(f'\nfabia = o que a tela mostra; parado = o mesmo com os núcleos parados; proximidade = '
          f'núcleo a menos de {PROXIMIDADE_KM} km em qualquer rumo.\n'
          f'acerto = entrou numa área de risco observada a até {ACERTO_KM} km do ponto de encontro previsto; '
          f'{NUCLEO} = conferido contra o núcleo movido pela estimativa (16 a 18/11); '
          f'antiga = qualquer área de risco (antes de 16/11).\n'
          'não previstos: "com alerta" = havia alerta em aberto, mas a entrada foi longe do ponto previsto; '
          'aviso = entradas com algum alerta (acertos + com alerta).')


def _ordem_detalhe(chave: tuple[str, str, str]) -> tuple:
    """Chave de ordenação das linhas das tabelas: confianças, NA_TELA, faixas; o resto no fim."""
    _, nome, _ = chave
    ordem = [*TEXTO_CONFIANCA.values(), NA_TELA, *(faixa(fim) for _, fim in ANTECEDENCIAS)]
    posicao = ordem.index(nome) if nome in ordem else len(ordem)
    return (posicao, nome)


def _ordenadas(por_regra: dict[str, Counter]) -> list[tuple[str, Counter]]:
    """As regras na ordem do relatório: REGRAS, as mesmas com NUCLEO, ANTIGA; desconhecidas no fim."""
    ordem = (*REGRAS, *(regra + NUCLEO for regra in REGRAS), ANTIGA)

    def posicao(item: tuple[str, Counter]) -> int:
        regra = item[0]
        return ordem.index(regra) if regra in ordem else len(ordem)

    return sorted(por_regra.items(), key=posicao)


def _linha(resumo: dict) -> str:
    """Uma linha do relatório a partir do `resumir` de uma regra."""
    def porcentagem(taxa: float | None) -> str:
        return '—' if taxa is None else f'{taxa:.0%}'
    return (f"{resumo['alertas']} alertas · {resumo['acertos']} acertos · {resumo['falsos']} falsos · "
            f"{resumo['indeterminados']} indeterminados · {resumo['nao_previstos']} não previstos "
            f"({resumo['com_alerta']} com alerta, {resumo['sem_aviso']} sem aviso) · "
            f"acerto {porcentagem(resumo['acerto'])} · detecção {porcentagem(resumo['deteccao'])} · "
            f"aviso {porcentagem(resumo['aviso'])}")


if __name__ == '__main__':
    avaliar(config.PREVISAO_DIR, sys.argv[1:])
