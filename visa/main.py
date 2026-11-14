"""
CLS Visa — visualizador meteorológico para quem acompanha o tráfego aéreo.

Para rodar:   .venv/bin/python main.py      e abra  http://localhost:8090

Este arquivo só liga as peças: as rotas HTTP que o navegador chama diretamente (imagens
e GeoJSON, que não passam pelo websocket do NiceGUI), as tarefas de fundo do servidor e
a página. O que cada módulo faz está na tabela "Onde está cada coisa" do README.md, e os
porquês das escolhas, em DECISOES.md.
"""
import asyncio
import logging

from fastapi import HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from nicegui import app, ui

import aeronaves
import config
import espaco
import impacto
import interface
import radar
import redemet

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)  # o httpx loga a URL inteira, com a api_key

app.add_static_files('/static', config.RAIZ / 'static')

# ---------------------------------------------------------------------------
# Rotas HTTP do servidor (o navegador pede as imagens e os GeoJSON aqui)
#
# Rotas `async def` só esperam a rede (o trabalho pesado vai para uma thread lá dentro);
# rotas `def` comuns fazem trabalho de CPU ou disco, e o FastAPI as roda numa thread
# sozinho — assim nenhuma delas trava o laço que atende os outros usuários.
# ---------------------------------------------------------------------------

app.add_middleware(GZipMiddleware, minimum_size=1000)   # GeoJSONs grandes vão comprimidos


@app.get('/espaco/{chave}.geojson')
def espaco_geojson(chave: str) -> Response:
    caminho = espaco.arquivo_local(chave) if chave in espaco.POR_CHAVE else None
    if caminho is None:
        raise HTTPException(404)
    return Response(espaco.geojson_preparado(caminho), media_type='application/geo+json')


@app.get('/radar/wms')
async def radar_wms(request: Request) -> Response:
    status, conteudo, tipo = await radar.buscar_tile(dict(request.query_params))
    return Response(conteudo, status_code=status, media_type=tipo)


@app.get('/redemet/imagem')
async def redemet_imagem(c: str, s: float, n: float, dbz: int = 0) -> Response:
    status, conteudo, tipo = await redemet.buscar_imagem(c, s, n, dbz)
    cabecalhos = {'Cache-Control': 'max-age=3600'} if status == 200 else {}   # o caminho já tem a data
    return Response(conteudo, status_code=status, media_type=tipo, headers=cabecalhos)


@app.get('/redemet/raios')
def redemet_raios(q: str) -> Response:
    status, conteudo, tipo = redemet.imagem_raios(q)
    cabecalhos = {'Cache-Control': 'max-age=3600'} if status == 200 else {}   # um quadro não muda
    return Response(conteudo, status_code=status, media_type=tipo, headers=cabecalhos)


async def iniciar() -> None:
    """Tarefas que rodam enquanto o servidor estiver de pé, uma só para todos os usuários.

    As tarefas ficam guardadas em `app.state` de propósito: o asyncio só mantém uma
    referência fraca às tarefas criadas, e uma tarefa sem referência pode ser recolhida
    pelo coletor de lixo no meio da execução (aviso da documentação de create_task).
    """
    app.state.tarefa_pusher = asyncio.create_task(aeronaves.manter_conexao())
    app.state.tarefa_radar = asyncio.create_task(radar.vigiar())
    app.state.tarefa_impacto = asyncio.create_task(impacto.vigiar())


app.on_startup(iniciar)

# reload=False: o recarregamento automático do NiceGUI reiniciaria as tarefas de fundo (e a
# conexão com o Pusher) a cada arquivo salvo; show=False: é um servidor, não abre navegador.
ui.run(interface.pagina, title='CLS Visa', port=config.PORTA, dark=True, reload=False, show=False,
       storage_secret=config.SEGREDO_SESSAO, favicon='🛩')
