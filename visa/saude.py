"""
Saúde dos dados: a idade e a situação de cada fonte, para a barra no topo da tela.

Funções puras: recebem o que cada camada sabe (horários, erros) e devolvem um Indicador.
Quem decide os limites (o que é "recente", "atrasado", "velho") é este arquivo.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

import config

OK, ATENCAO, CRITICO, SEM_DADOS = 'ok', 'atencao', 'critico', 'sem_dados'
CORES = {OK: '#10b981', ATENCAO: '#fbbf24', CRITICO: '#ef4444', SEM_DADOS: '#64748b'}


@dataclass(frozen=True)
class Indicador:
    fonte: str       # 'Radar', 'Raios'...
    texto: str       # curto, vai no topo: '4 min', 'rodada 00Z'
    nivel: str       # OK, ATENCAO, CRITICO ou SEM_DADOS
    detalhe: str     # dica ao passar o mouse

    @property
    def cor(self) -> str:
        return CORES[self.nivel]


def idade_curta(segundos: float) -> str:
    """'agora', '4 min', '3 h', '2 d'."""
    if segundos < 60:
        return 'agora'
    if segundos < 3600:
        return f'{segundos / 60:.0f} min'
    if segundos < 48 * 3600:
        return f'{segundos / 3600:.0f} h'
    return f'{segundos / 86400:.0f} d'


def _nivel(segundos: float, atencao_s: float, critico_s: float) -> str:
    if segundos <= atencao_s:
        return OK
    return ATENCAO if segundos <= critico_s else CRITICO


def _agora(agora: datetime | None) -> datetime:
    return agora or datetime.now(UTC)


# ---------------------------------------------------------------------------
# Uma função por fonte
# ---------------------------------------------------------------------------

def radar(ultimas: list[datetime], status_ok: bool, erro: str = '',
          agora: datetime | None = None) -> Indicador:
    """Idade da imagem mais nova entre os radares selecionados (mesmos limites dos botões)."""
    if erro:
        return Indicador('Radar', 'fora', CRITICO, erro)
    if not status_ok:
        return Indicador('Radar', 'sem status', SEM_DADOS, 'O GeoServer não informou o horário das imagens')
    if not ultimas:
        return Indicador('Radar', 'sem dados', CRITICO, 'Nenhum radar selecionado tem imagem recente')
    mais_nova = max(ultimas)
    s = (_agora(agora) - mais_nova).total_seconds()
    return Indicador('Radar', idade_curta(s), _nivel(s, config.RADAR_VERDE_ATE_MIN * 60, config.RADAR_AMARELO_ATE_MIN * 60),
                     f'Imagem mais nova: {mais_nova:%d/%m %H:%MZ}')


def satelite(quando: datetime | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """Imagem nova a cada 10 min, publicada com ~20–30 min de atraso: até 40 min está em dia."""
    if erro:
        return Indicador('Satélite', 'fora', CRITICO, erro)
    if quando is None:
        return Indicador('Satélite', '…', SEM_DADOS, 'Aguardando a REDEMET')
    s = (_agora(agora) - quando).total_seconds()
    return Indicador('Satélite', idade_curta(s), _nivel(s, 40 * 60, 70 * 60),
                     f'Imagem de {quando:%d/%m %H:%MZ}')


def geoserver(fonte: str, ok: bool | None, texto_ok: str, detalhe_ok: str) -> Indicador:
    """Camadas sem horário próprio (raios ao vivo, WIFS vigente): vale a situação do GeoServer."""
    if ok is None:
        return Indicador(fonte, '…', SEM_DADOS, 'GeoServer ainda não testado')
    if not ok:
        return Indicador(fonte, 'fora', CRITICO, 'GeoServer da REDEMET inacessível')
    return Indicador(fonte, texto_ok, OK, detalhe_ok)


def raios_api(quando: datetime | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """Quadro novo a cada 6–7 min: até 15 min está em dia."""
    if erro:
        return Indicador('Raios', 'fora', CRITICO, erro)
    if quando is None:
        return Indicador('Raios', '…', SEM_DADOS, 'Aguardando a REDEMET')
    s = (_agora(agora) - quando).total_seconds()
    return Indicador('Raios', idade_curta(s), _nivel(s, 15 * 60, 30 * 60),
                     f'Quadro de {quando:%d/%m %H:%MZ} (API da REDEMET)')


def raios(geoserver_ok: bool | None, fim: datetime | None) -> Indicador:
    if fim is not None and geoserver_ok:   # o usuário voltou no tempo: avisa que não é ao vivo
        return Indicador('Raios', f'até {fim:%H:%MZ}', ATENCAO, 'Janela escolhida no passado (não é ao vivo)')
    return geoserver('Raios', geoserver_ok, 'ao vivo', 'Últimos 60 min, atualiza a cada 2 min')


def severo(rodada: str | None, validade: str | None, erro: str = '', agora: datetime | None = None) -> Indicador:
    """A idade que importa é a da rodada do modelo; a validade vai na dica."""
    if erro:
        return Indicador('Severo', 'fora', CRITICO, erro)
    if not rodada:
        return Indicador('Severo', '…', SEM_DADOS, 'Catálogo ainda não carregado')
    try:
        inicio = datetime.fromisoformat(rodada)  # Python 3.11+ aceita o 'Z'
    except ValueError:                           # catálogo com data estranha: avisa, não derruba a barra
        return Indicador('Severo', '?', ATENCAO, f'Rodada ilegível no catálogo: {rodada[:30]}')
    s = (_agora(agora) - inicio).total_seconds()
    detalhe = f'Rodada {inicio:%d/%m %H:%MZ}'
    if validade:
        detalhe += f' · válida para {validade[8:10]}/{validade[5:7]} {validade[11:16]}Z'
    return Indicador('Severo', f'{inicio:%H}Z', _nivel(s, 12 * 3600, 24 * 3600), detalhe)


def vento(referencia: datetime | None, fl: int, agora: datetime | None = None) -> Indicador:
    if referencia is None:
        return Indicador('Vento', 'sem arquivo', CRITICO, f'Sem arquivo de vento para FL{fl:03d}')
    s = (_agora(agora) - referencia).total_seconds()
    nivel = _nivel(s, config.VENTO_IDADE_ALERTA_H * 3600, config.VENTO_IDADE_VELHO_H * 3600)
    return Indicador('Vento', f'{referencia:%H}Z', nivel,
                     f'FL{fl:03d} · referência {referencia:%d/%m %H:%MZ} ({idade_curta(s)})')


def trafego(conectado: bool, recebido_em: datetime | None, situacao: str,
            agora: datetime | None = None) -> Indicador:
    if not conectado or recebido_em is None:
        return Indicador('Tráfego', 'fora', CRITICO, f'Fonte DECEA: {situacao}')
    s = (_agora(agora) - recebido_em).total_seconds()
    return Indicador('Tráfego', 'ao vivo' if s < 30 else idade_curta(s), _nivel(s, 30, 120),
                     f'Fonte DECEA: {situacao}')


def aerodromos(idade_s: float | None, erro: str = '') -> Indicador:
    """idade_s: segundos desde a última consulta boa (None = nunca)."""
    if erro:
        return Indicador('Aeródromos', 'fora', CRITICO, erro)
    if idade_s is None:
        return Indicador('Aeródromos', '…', SEM_DADOS, 'Aguardando a REDEMET')
    return Indicador('Aeródromos', idade_curta(idade_s), _nivel(idade_s, 20 * 60, 60 * 60),
                     'Status METAR/TAF da REDEMET')


def airac(validades: dict[str, str | None], hoje: str) -> Indicador:
    """validades: camada -> próxima emenda ('2026-11-26') ou None (arquivo sem data)."""
    vencidas = [c for c, proxima in validades.items() if proxima and hoje >= proxima]
    if vencidas:
        return Indicador('AIRAC', 'vencido', CRITICO, 'Baixe de novo: ' + ', '.join(sorted(vencidas)))
    datas = sorted(p for p in validades.values() if p)
    if not datas:
        return Indicador('AIRAC', '?', SEM_DADOS, 'Arquivos sem data de validade (ou imagens do DECEA)')
    return Indicador('AIRAC', f'até {datas[0][8:10]}/{datas[0][5:7]}', OK, 'Espaço aéreo dentro do ciclo')
