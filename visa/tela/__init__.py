"""
As áreas da tela, uma classe por área (montadas por interface.pagina()):

    ┌──────────────────────────── Cabecalho ─────────────────────────────┐
    │ ☰ CLS Visa  [modos] [terminal]      [Impacto] [Sonda] BarraSaude   │
    ├──────────────┬───────────────────────────────────┬─────────────────┤
    │PainelCamadas │            AreaDoMapa             │ Gaveta:         │
    │ (gaveta da   │                        ┌────────┐ │  PainelImpacto  │
    │  esquerda)   │                        │Legenda │ │  PainelSonda    │
    │              │    LinhaDoTempo        └────────┘ │ (abas)          │
    └──────────────┴───────────────────────────────────┴─────────────────┘

  mapa.py           AreaDoMapa: o mapa, o fundo, e os lugares da legenda e da linha do tempo
  cabecalho.py      Cabecalho: modos de trabalho, aeródromo do modo Terminal, botões Impacto e Sonda
  barra_saude.py    BarraSaude: idade e situação de cada fonte de dados ligada
  gaveta.py         Gaveta (à direita, com abas), PainelSonda, PainelImpacto
  legenda.py        Legenda: só das camadas ligadas
  linha_tempo.py    LinhaDoTempo: controles da animação
  painel_camadas.py PainelCamadas: um cartão por camada, em três grupos
  pecas.py          peças pequenas usadas por várias áreas (cartão, bolinhas, título de seção)

Regras comuns:
  - cada classe recebe as preferências (`prefs`, o dicionário do usuário) e as camadas
    (`camadas.CamadasDaPagina`), e cria os SEUS controles e os SEUS relógios (ui.timer);
  - as áreas não mexem nos controles umas das outras: chamam métodos (ex.: o Cabecalho
    chama `PainelSonda.alternar_modo()`, e não muda o cursor do mapa ele mesmo);
  - os relógios são criados fora de qualquer contêiner que seja limpo com `.clear()`
    (um relógio dentro de um contêiner limpo seria apagado junto).
"""
