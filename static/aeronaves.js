/*
 * Camada de aeronaves desenhada em <canvas> (o outro JavaScript próprio é camadas.js).
 *
 * Não tem lógica de negócio: o Python decide QUAIS aeronaves mostrar e manda a
 * lista pronta com  setDados([{lat, lon, rumo, indicativo, fl, vel, origem, destino, fonte, alerta}, ...]).
 * Esta camada só desenha e, ao clicar perto de uma aeronave, mostra os dados dela.
 * `alerta` (texto ou null) também vem pronto do Python: aeronave em área de raios/eco forte
 * é desenhada em destaque e, a partir do zoom 6, sempre com o indicativo.
 *
 * Quem usa, do Python (visa/camadas/contexto.py, classe CamadaAeronaves):
 *   - cria a camada:  mapa.generic_layer(name='aeronavesLayer', args=[{cores, tamanho, rotulos}])
 *   - setDados(lista)    em CamadaAeronaves.enviar (relógio de config.AERONAVE_ENVIO_S)
 *   - setOpcoes({...})   em CamadaAeronaves.mudar_rotulos (chave "rótulos" do painel)
 * O botão "Ver / ocultar rota" do popup avisa o Python com o evento 'rota' (visa/interface.py).
 *
 * Por que canvas: com ~500 aeronaves atualizando a cada 2 s, um único desenho em
 * canvas é muito mais leve que 500 elementos SVG/HTML no mapa.
 */
(function () {
  const L = window.L;

  L.AeronavesLayer = L.Layer.extend({
    options: {
      cor: '#e2e8f0',
      corIndicativo: '#67e8f9',
      corNivel: '#fbbf24',
      corAlerta: '#f59e0b',
      tamanho: 14,          // px
      rotulos: true,
      zoomRotulos: 7,       // abaixo deste zoom os rótulos viram uma mancha ilegível
      raioClique: 12,       // px
    },

    // Construtor: guarda as opções vindas do Python (estilo) e começa sem aeronaves.
    initialize(options) {
      L.setOptions(this, options);
      this._dados = [];
    },

    // O Leaflet chama ao pôr a camada no mapa: cria o canvas e passa a redesenhar a cada
    // movimento/zoom do mapa e a tratar os cliques.
    onAdd(map) {
      this._map = map;
      if (!map.getPane('aeronaves')) {
        map.createPane('aeronaves').style.zIndex = 640;   // acima das camadas, abaixo dos popups
      }
      this._canvas = L.DomUtil.create('canvas', 'leaflet-zoom-hide');
      this._canvas.style.pointerEvents = 'none';
      map.getPane('aeronaves').appendChild(this._canvas);
      map.on('moveend zoomend resize', this._redesenhar, this);
      map.on('click', this._aoClicar, this);
      this._redesenhar();
    },

    // O Leaflet chama ao tirar a camada do mapa: desfaz o que onAdd fez.
    onRemove(map) {
      map.off('moveend zoomend resize', this._redesenhar, this);
      map.off('click', this._aoClicar, this);
      L.DomUtil.remove(this._canvas);
    },

    // Chamado pelo Python (CamadaAeronaves.enviar): troca a lista de aeronaves e redesenha.
    setDados(lista) {
      this._dados = lista || [];
      this._redesenhar();
    },

    // Chamado pelo Python (CamadaAeronaves.mudar_rotulos): muda opções (ex.: {rotulos: false}) e redesenha.
    setOpcoes(opcoes) {
      L.setOptions(this, opcoes);
      this._redesenhar();
    },

    // Apaga o canvas e desenha todas as aeronaves visíveis: um chevron (seta em "V") no rumo
    // e, se couber, o indicativo e o nível ao lado.
    _redesenhar() {
      if (!this._map) return;
      const map = this._map, tamanho = map.getSize(), escala = window.devicePixelRatio || 1;
      const canvas = this._canvas;
      // O canvas cobre exatamente a área visível do mapa
      L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]));
      canvas.width = tamanho.x * escala;
      canvas.height = tamanho.y * escala;
      canvas.style.width = tamanho.x + 'px';
      canvas.style.height = tamanho.y + 'px';

      const ctx = canvas.getContext('2d');   // o "pincel" do canvas
      ctx.setTransform(escala, 0, 0, escala, 0, 0);
      ctx.clearRect(0, 0, tamanho.x, tamanho.y);
      const meioTamanho = this.options.tamanho / 2;
      ctx.font = '11px monospace';
      ctx.lineWidth = 1.5;
      ctx.lineJoin = 'round';

      const comRotulo = this.options.rotulos && map.getZoom() >= this.options.zoomRotulos;
      // Em alerta: indicativo mesmo com os rótulos desligados, mas não no zoom do país inteiro
      const rotuloAlerta = map.getZoom() >= this.options.zoomRotulos - 1;
      this._telas = [];  // posições na tela, para achar a aeronave clicada
      for (const aeronave of this._dados) {
        const ponto = map.latLngToContainerPoint([aeronave.lat, aeronave.lon]);
        // Fora da tela (com 50 px de margem): não desenha
        if (ponto.x < -50 || ponto.y < -50 || ponto.x > tamanho.x + 50 || ponto.y > tamanho.y + 50) continue;
        this._telas.push([ponto, aeronave]);

        // Chevron apontando para o rumo
        ctx.save();
        ctx.translate(ponto.x, ponto.y);
        ctx.rotate((aeronave.rumo * Math.PI) / 180);
        ctx.strokeStyle = aeronave.alerta ? this.options.corAlerta : this.options.cor;
        ctx.lineWidth = aeronave.alerta ? 2.5 : 1.5;
        ctx.beginPath();
        ctx.moveTo(-meioTamanho * 0.7, meioTamanho * 0.7);
        ctx.lineTo(0, -meioTamanho * 0.8);
        ctx.lineTo(meioTamanho * 0.7, meioTamanho * 0.7);
        ctx.stroke();
        ctx.restore();

        // Indicativo e, abaixo dele, o nível de voo (ex.: 350)
        if (comRotulo || (aeronave.alerta && rotuloAlerta)) {
          ctx.fillStyle = aeronave.alerta ? this.options.corAlerta : this.options.corIndicativo;
          ctx.fillText(aeronave.indicativo, ponto.x + meioTamanho + 3, ponto.y - 2);
          if (aeronave.fl !== null) {
            ctx.fillStyle = this.options.corNivel;
            ctx.fillText(String(aeronave.fl).padStart(3, '0'), ponto.x + meioTamanho + 3, ponto.y + 10);
          }
        }
      }
    },

    // Clique no mapa: acha a aeronave desenhada mais perto do clique (até raioClique px)
    // e abre o popup com os dados dela.
    _aoClicar(evento) {
      const clique = evento.containerPoint;
      let maisPerto = null, menorDistancia = this.options.raioClique;
      for (const [ponto, aeronave] of this._telas || []) {
        const distancia = ponto.distanceTo(clique);
        if (distancia < menorDistancia) { menorDistancia = distancia; maisPerto = aeronave; }
      }
      if (!maisPerto) return;
      L.popup().setLatLng([maisPerto.lat, maisPerto.lon]).setContent(this._ficha(maisPerto)).openOn(this._map);
    },

    // Monta o popup com textContent (nunca innerHTML): dado externo não vira HTML
    _ficha(aeronave) {
      const div = document.createElement('div');
      const linhas = [
        ['', aeronave.indicativo],
        ['Rota', `${aeronave.origem || '?'} → ${aeronave.destino || '?'}`],
        ['Nível', aeronave.fl === null ? '—' : 'FL' + String(aeronave.fl).padStart(3, '0')],
        ['Velocidade', aeronave.vel === null ? '—' : aeronave.vel + ' kt'],
        ['Rumo', aeronave.rumo + '°'],
        ['Fonte', aeronave.fonte || '—'],
      ];
      if (aeronave.alerta) linhas.push(['Alerta', aeronave.alerta]);
      for (const [rotulo, valor] of linhas) {
        const linha = document.createElement('div');
        if (rotulo) {
          const negrito = document.createElement('b');
          negrito.textContent = rotulo + ': ';
          linha.appendChild(negrito);
        } else {
          linha.style.fontWeight = '700';   // a primeira linha (indicativo) vai toda em negrito
        }
        linha.appendChild(document.createTextNode(valor));
        div.appendChild(linha);
      }
      // Pede a rota ao Python (evento 'rota' tratado em interface.py); clicar de novo oculta
      const botao = document.createElement('button');
      botao.className = 'aeronave-botao-rota';
      botao.textContent = 'Ver / ocultar rota';
      botao.onclick = () => emitEvent('rota', aeronave.indicativo);
      div.appendChild(botao);
      return div;
    },
  });

  // Fábrica no estilo do Leaflet: é o nome 'aeronavesLayer' que o Python usa em generic_layer.
  L.aeronavesLayer = (options) => new L.AeronavesLayer(options);
})();
