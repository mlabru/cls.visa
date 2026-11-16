"""
Centro Logístico de Simulação Meteorológica
Copyright (c) 1995-2026, Instituto de Controle do Espaço Aéreo

BarraSaude: no canto direito do cabeçalho, a idade e a situação de cada
fonte de dados ligada.

As regras (o que é recente, atrasado, crítico) estão em saude.py; aqui só se
junta o que cada camada sabe e se desenha.
"""
import time
from datetime import UTC, datetime

from nicegui import ui

import aerodromos
import aeronaves
import camadas
import espaco
import radar
import saude


class BarraSaude:
    """Recalcula a cada INTERVALO_S e só redesenha se algum indicador mudou."""

    INTERVALO_S = 5

    def __init__(self, prefs: dict, camadas_: camadas.CamadasDaPagina) -> None:
        self.prefs, self.c = prefs, camadas_
        self.barra = ui.row().classes('items-center no-wrap gap-1 saude')
        self._mostrados: list[saude.Indicador] = []

    def ligar_relogio(self) -> None:
        """Fica fora do __init__ porque a barra é criada dentro do cabeçalho, e o relógio não."""
        ui.timer(self.INTERVALO_S, self.atualizar)

    def indicadores(self) -> list[saude.Indicador]:
        p, c = self.prefs, self.c
        lista = []
        agora = datetime.now(UTC)
        if p['satelite_ligado']:
            imagem = c.satelite.imagem
            lista.append(saude.satelite(imagem.quando if imagem else None, c.satelite.erro, agora))
        if p['radar_ligado']:
            ultimas = [c.radar.ultimas[r] for r in p['radares'] if r in c.radar.ultimas]
            erro = c.radar.erro if c.radar.pela_api() else ''
            lista.append(saude.radar(ultimas, c.radar.status_ok, erro, agora))
        if p['raios_ligado'] and c.raios.pela_api():
            quadro = c.raios.quadro
            lista.append(saude.raios_api(quadro.quando if quadro else None, c.raios.erro, agora))
        elif p['raios_ligado']:
            lista.append(saude.raios(radar.estado.ok, c.raios.fim))
        if p['severo_ligado']:
            e = c.severo.escolha
            lista.append(saude.severo(e['rodada'], e['validade'], c.severo.erro, agora))
        if any(p['wifs'].values()):
            lista.append(saude.geoserver('WIFS', radar.estado.ok, f"FL{p['wifs_fl']:03d}",
                                         'Previsão vigente do WIFS'))
        if p['vento_ligado']:
            lista.append(saude.vento(c.vento.referencia, p['vento_fl'], agora))
        if p['aeronaves_ligado']:
            t = aeronaves.trafego
            lista.append(saude.trafego(t.conectado, t.recebido_em, t.situacao, agora))
        if p['aerodromos_ligado']:
            s = aerodromos.situacao
            idade = time.monotonic() - s.consultado_em if s.consultado_em else None
            lista.append(saude.aerodromos(idade, s.erro))
        ligadas = [chave for chave, ligada in p['espaco'].items() if ligada]
        if ligadas:
            validades = {}
            for chave in ligadas:
                caminho = espaco.arquivo_local(chave)
                validades[chave] = espaco.validade(caminho)[1] if caminho else None
            lista.append(saude.airac(validades, agora.date().isoformat()))
        return lista

    def atualizar(self) -> None:
        lista = self.indicadores()
        if lista == self._mostrados:     # os Indicadores são dataclasses: comparam por valor
            return
        self._mostrados = lista
        self.barra.clear()
        with self.barra:
            for ind in lista:
                with ui.element('div').classes('saude-item'):
                    ui.element('span').classes('saude-ponto').style(f'background: {ind.cor}')
                    ui.label(ind.fonte).classes('saude-fonte')
                    ui.label(ind.texto).classes('saude-texto')
                    ui.tooltip(ind.detalhe)
