"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

Saúde dos dados: a idade e a situação de cada fonte, para a barra no topo da tela.

Funções puras: recebem o que cada camada sabe (horários, erros) e devolvem um Indicador.
Quem decide os limites (o que é "recente", "atrasado", "velho") é este arquivo.
Quem usa: `tela/barra_saude.py`.
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple

import config
import datas


class Nivel(StrEnum):
    """A situação de uma fonte (define a cor do indicador)."""
    OK = 'ok'
    ATENCAO = 'atencao'
    CRITICO = 'critico'
    SEM_DADOS = 'sem_dados'


OK, ATENCAO, CRITICO, SEM_DADOS = Nivel     # apelidos curtos: saude.OK, saude.CRITICO...
CORES = {OK: '#10b981', ATENCAO: '#fbbf24', CRITICO: '#ef4444', SEM_DADOS: '#64748b'}


@dataclass(frozen=True)
class Indicador:
    """Um item da barra de saúde.

    Atributos:
        fonte: nome da fonte ('Radar', 'Raios'...).
        texto: curto, vai no topo ('4 min', 'rodada 00Z').
        nivel: a situação (define a cor).
        detalhe: dica ao passar o mouse.
    """
    fonte: str
    texto: str
    nivel: Nivel
    detalhe: str

    @property
    def cor(self) -> str:
        """A cor do indicador (CORES), conforme o nível."""
        return CORES[self.nivel]


class _Prazos(NamedTuple):
    """Até que idade uma fonte está em dia e até quando está só atrasada.

    Atributos:
        atencao_s: até esta idade (s) está OK; acima, ATENCAO.
        critico_s: acima desta idade (s) é CRITICO.
    """
    atencao_s: float
    critico_s: float


# Satélite: imagem nova a cada 10 min, publicada com ~20–30 min de atraso
_PRAZOS_SATELITE = _Prazos(atencao_s=40 * 60, critico_s=70 * 60)
# Raios da API: quadro novo a cada 6–7 min
_PRAZOS_RAIOS_API = _Prazos(atencao_s=15 * 60, critico_s=30 * 60)
_PRAZOS_AERODROMOS = _Prazos(atencao_s=20 * 60, critico_s=60 * 60)
_PRAZOS_SEVERO = _Prazos(atencao_s=12 * 3600, critico_s=24 * 3600)
_PRAZOS_TRAFEGO = _Prazos(atencao_s=30, critico_s=120)


def idade_curta(segundos: float) -> str:
    """Idade em texto curto: 'agora', '4 min', '3 h', '2 d'.

    Parâmetros:
        segundos: a idade, s.

    Retorna:
        O texto.
    """
    if segundos < 60:
        return 'agora'
    if segundos < 3600:
        return f'{segundos / 60:.0f} min'
    if segundos < 48 * 3600:
        return f'{segundos / 3600:.0f} h'
    return f'{segundos / 86400:.0f} d'


def _nivel(segundos: float, prazos: _Prazos) -> Nivel:
    """OK, ATENCAO ou CRITICO conforme a idade (em s) e os prazos da fonte."""
    if segundos <= prazos.atencao_s:
        return OK
    if segundos <= prazos.critico_s:
        return ATENCAO
    return CRITICO


def _idade_s(quando: datetime, agora: datetime | None) -> float:
    """Segundos desde `quando` até `agora` (None = o relógio do sistema, em UTC)."""
    return ((agora or datetime.now(UTC)) - quando).total_seconds()


def _por_idade(fonte: str, segundos: float | None, erro: str, prazos: _Prazos, detalhe: str) -> Indicador:
    """Fonte com um horário só (satélite, raios da API, aeródromos): fora, aguardando ou a idade.

    Parâmetros:
        fonte: nome mostrado.
        segundos: idade do dado, s (None = ainda não chegou nada).
        erro: falha da última consulta ('' = nenhuma).
        prazos: limites de idade da fonte.
        detalhe: dica ao passar o mouse, quando há dado.

    Retorna:
        O indicador.
    """
    if erro:
        return Indicador(fonte, 'fora', CRITICO, erro)
    if segundos is None:
        return Indicador(fonte, '…', SEM_DADOS, 'Aguardando a REDEMET')
    return Indicador(fonte, idade_curta(segundos), _nivel(segundos, prazos), detalhe)


# ---------------------------------------------------------------------------
# Uma função por fonte
# ---------------------------------------------------------------------------

def radar(ultimas: list[datetime], status_ok: bool, erro: str = '',
          agora: datetime | None = None) -> Indicador:
    """Idade da imagem mais nova entre os radares selecionados (mesmos limites dos botões).

    Parâmetros:
        ultimas: horário da imagem mais nova de cada radar selecionado.
        status_ok: se o GeoServer informou os horários.
        erro: falha da última consulta ('' = nenhuma).
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador.
    """
    if erro:
        return Indicador('Radar', 'fora', CRITICO, erro)
    if not status_ok:
        return Indicador('Radar', 'sem status', SEM_DADOS, 'O GeoServer não informou o horário das imagens')
    if not ultimas:
        return Indicador('Radar', 'sem dados', CRITICO, 'Nenhum radar selecionado tem imagem recente')
    mais_nova = max(ultimas)
    idade_s = _idade_s(mais_nova, agora)
    prazos = _Prazos(config.RADAR_VERDE_ATE_MIN * 60, config.RADAR_AMARELO_ATE_MIN * 60)
    return Indicador('Radar', idade_curta(idade_s), _nivel(idade_s, prazos),
                     f'Imagem mais nova: {mais_nova:%d/%m %H:%MZ}')


def satelite(quando: datetime | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """Imagem nova a cada 10 min, publicada com ~20–30 min de atraso: até 40 min está em dia.

    Parâmetros:
        quando: horário da imagem mais nova (None = nenhuma ainda).
        erro: falha da última consulta ('' = nenhuma).
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador.
    """
    if quando is None:
        return _por_idade('Satélite', None, erro, _PRAZOS_SATELITE, '')
    detalhe = f'Imagem de {quando:%d/%m %H:%MZ}'
    return _por_idade('Satélite', _idade_s(quando, agora), erro, _PRAZOS_SATELITE, detalhe)


def geoserver(fonte: str, ok: bool | None, texto_ok: str, detalhe_ok: str) -> Indicador:
    """Camadas sem horário próprio (raios ao vivo, WIFS vigente): vale a situação do GeoServer.

    Parâmetros:
        fonte: nome mostrado.
        ok: situação do GeoServer (None = ainda não testado).
        texto_ok: texto quando o GeoServer está no ar.
        detalhe_ok: dica quando o GeoServer está no ar.

    Retorna:
        O indicador.
    """
    if ok is None:
        return Indicador(fonte, '…', SEM_DADOS, 'GeoServer ainda não testado')
    if not ok:
        return Indicador(fonte, 'fora', CRITICO, 'GeoServer da REDEMET inacessível')
    return Indicador(fonte, texto_ok, OK, detalhe_ok)


def raios_api(quando: datetime | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """Quadro novo a cada 6–7 min: até 15 min está em dia.

    Parâmetros:
        quando: horário do quadro mais novo (None = nenhum ainda).
        erro: falha da última consulta ('' = nenhuma).
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador.
    """
    if quando is None:
        return _por_idade('Raios', None, erro, _PRAZOS_RAIOS_API, '')
    detalhe = f'Quadro de {quando:%d/%m %H:%MZ} (API da REDEMET)'
    return _por_idade('Raios', _idade_s(quando, agora), erro, _PRAZOS_RAIOS_API, detalhe)


def raios(geoserver_ok: bool | None, fim: datetime | None) -> Indicador:
    """Raios do GeoServer (ao vivo ou numa janela escolhida no passado).

    Parâmetros:
        geoserver_ok: situação do GeoServer (None = ainda não testado).
        fim: fim da janela escolhida pelo usuário (None = ao vivo).

    Retorna:
        O indicador.
    """
    if fim is not None and geoserver_ok:   # o usuário voltou no tempo: avisa que não é ao vivo
        return Indicador('Raios', f'até {fim:%H:%MZ}', ATENCAO, 'Janela escolhida no passado (não é ao vivo)')
    return geoserver('Raios', geoserver_ok, 'ao vivo', 'Últimos 60 min, atualiza a cada 2 min')


def severo(rodada: str | None, validade: str | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """A idade que importa é a da rodada do modelo; a validade vai na dica.

    Parâmetros:
        rodada: início da rodada, ISO 8601 (None ou '' = catálogo ainda não carregado).
        validade: horário de validade, ISO 8601 (None = sem validade).
        erro: falha da última consulta ('' = nenhuma).
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador.
    """
    if erro:
        return Indicador('Severo', 'fora', CRITICO, erro)
    if not rodada:
        return Indicador('Severo', '…', SEM_DADOS, 'Catálogo ainda não carregado')
    inicio = datas.ler_iso(rodada)
    if inicio is None:                           # catálogo com data estranha: avisa, não derruba a barra
        return Indicador('Severo', '?', ATENCAO, f'Rodada ilegível no catálogo: {rodada[:30]}')
    detalhe = f'Rodada {inicio:%d/%m %H:%MZ}'
    if validade:
        detalhe += f' · válida para {validade[8:10]}/{validade[5:7]} {validade[11:16]}Z'
    return Indicador('Severo', f'{inicio:%H}Z', _nivel(_idade_s(inicio, agora), _PRAZOS_SEVERO), detalhe)


def vento(referencia: datetime | None, fl: int, agora: datetime | None = None) -> Indicador:
    """Idade do arquivo de vento do nível escolhido.

    Parâmetros:
        referencia: horário de referência do arquivo (None = não há arquivo).
        fl: nível de voo escolhido (ex.: 340).
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador.
    """
    if referencia is None:
        return Indicador('Vento', 'sem arquivo', CRITICO, f'Sem arquivo de vento para FL{fl:03d}')
    idade_s = _idade_s(referencia, agora)
    prazos = _Prazos(config.VENTO_IDADE_ALERTA_H * 3600, config.VENTO_IDADE_VELHO_H * 3600)
    return Indicador('Vento', f'{referencia:%H}Z', _nivel(idade_s, prazos),
                     f'FL{fl:03d} · referência {referencia:%d/%m %H:%MZ} ({idade_curta(idade_s)})')


def trafego(conectado: bool, recebido_em: datetime | None, situacao: str,
            agora: datetime | None = None) -> Indicador:
    """Situação da fonte de tráfego do DECEA.

    Parâmetros:
        conectado: se a conexão está aberta.
        recebido_em: horário da última mensagem (None = nenhuma).
        situacao: texto da situação da conexão, para a dica.
        agora: o momento de referência (None = agora, UTC).

    Retorna:
        O indicador ('ao vivo' até 30 s sem mensagem).
    """
    if not conectado or recebido_em is None:
        return Indicador('Tráfego', 'fora', CRITICO, f'Fonte DECEA: {situacao}')
    idade_s = _idade_s(recebido_em, agora)
    if idade_s < 30:
        texto = 'ao vivo'
    else:
        texto = idade_curta(idade_s)
    return Indicador('Tráfego', texto, _nivel(idade_s, _PRAZOS_TRAFEGO), f'Fonte DECEA: {situacao}')


def aerodromos(idade_s: float | None, erro: str = '') -> Indicador:
    """Situação da lista de aeródromos (status METAR/TAF).

    Parâmetros:
        idade_s: segundos desde a última consulta boa (None = nunca).
        erro: falha da última consulta ('' = nenhuma).

    Retorna:
        O indicador.
    """
    return _por_idade('Aeródromos', idade_s, erro, _PRAZOS_AERODROMOS, 'Status METAR/TAF da REDEMET')


def airac(validades: dict[str, str | None], hoje: str) -> Indicador:
    """Se os arquivos de espaço aéreo ainda valem no ciclo AIRAC.

    Parâmetros:
        validades: camada -> próxima emenda ('2026-11-26') ou None (arquivo sem data).
        hoje: a data de hoje, 'AAAA-MM-DD'.

    Retorna:
        O indicador.
    """
    vencidas = [camada for camada, proxima in validades.items() if proxima and hoje >= proxima]
    if vencidas:
        return Indicador('AIRAC', 'vencido', CRITICO, 'Baixe de novo: ' + ', '.join(sorted(vencidas)))
    proximas = sorted(proxima for proxima in validades.values() if proxima)
    if not proximas:
        return Indicador('AIRAC', '?', SEM_DADOS, 'Arquivos sem data de validade (ou imagens do DECEA)')
    primeira = proximas[0]
    return Indicador('AIRAC', f'até {primeira[8:10]}/{primeira[5:7]}', OK, 'Espaço aéreo dentro do ciclo')
