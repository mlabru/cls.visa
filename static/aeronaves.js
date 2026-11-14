/*
 * Camada de aeronaves desenhada em <canvas> — o único JavaScript próprio do sistema.
 *
 * Não tem lógica de negócio: o Python decide QUAIS aeronaves mostrar e manda a
 * lista pronta com  setDados([{lat, lon, rumo, indicativo, fl, vel, origem, destino, fonte, alerta}, ...]).
 * Esta camada só desenha e, ao clicar perto de uma aeronave, mostra os dados dela.
 * `alerta` (texto ou null) também vem pronto do Python: aeronave em área de raios/eco forte
 * é desenhada em destaque e, a partir do zoom 6, sempre com o indicativo.
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

    initialize(options) {
      L.setOptions(this, options);
      this._dados = [];
    },

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

    onRemove(map) {
      map.off('moveend zoomend resize', this._redesenhar, this);
      map.off('click', this._aoClicar, this);
      L.DomUtil.remove(this._canvas);
    },

    // Chamado pelo Python
    setDados(lista) {
      this._dados = lista || [];
      this._redesenhar();
    },

    setOpcoes(opcoes) {
      L.setOptions(this, opcoes);
      this._redesenhar();
    },

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

      const ctx = canvas.getContext('2d');
      ctx.setTransform(escala, 0, 0, escala, 0, 0);
      ctx.clearRect(0, 0, tamanho.x, tamanho.y);
      const r = this.options.tamanho / 2;
      ctx.font = '11px monospace';
      ctx.lineWidth = 1.5;
      ctx.lineJoin = 'round';

      const comRotulo = this.options.rotulos && map.getZoom() >= this.options.zoomRotulos;
      // Em alerta: indicativo mesmo com os rótulos desligados, mas não no zoom do país inteiro
      const rotuloAlerta = map.getZoom() >= this.options.zoomRotulos - 1;
      this._telas = [];  // posições na tela, para achar a aeronave clicada
      for (const a of this._dados) {
        const p = map.latLngToContainerPoint([a.lat, a.lon]);
        if (p.x < -50 || p.y < -50 || p.x > tamanho.x + 50 || p.y > tamanho.y + 50) continue;
        this._telas.push([p, a]);

        // Chevron apontando para o rumo
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate((a.rumo * Math.PI) / 180);
        ctx.strokeStyle = a.alerta ? this.options.corAlerta : this.options.cor;
        ctx.lineWidth = a.alerta ? 2.5 : 1.5;
        ctx.beginPath();
        ctx.moveTo(-r * 0.7, r * 0.7);
        ctx.lineTo(0, -r * 0.8);
        ctx.lineTo(r * 0.7, r * 0.7);
        ctx.stroke();
        ctx.restore();

        if (comRotulo || (a.alerta && rotuloAlerta)) {
          ctx.fillStyle = a.alerta ? this.options.corAlerta : this.options.corIndicativo;
          ctx.fillText(a.indicativo, p.x + r + 3, p.y - 2);
          if (a.fl !== null) {
            ctx.fillStyle = this.options.corNivel;
            ctx.fillText(String(a.fl).padStart(3, '0'), p.x + r + 3, p.y + 10);
          }
        }
      }
    },

    _aoClicar(e) {
      const clique = e.containerPoint;
      let melhor = null, menor = this.options.raioClique;
      for (const [p, a] of this._telas || []) {
        const d = p.distanceTo(clique);
        if (d < menor) { menor = d; melhor = a; }
      }
      if (!melhor) return;
      L.popup().setLatLng([melhor.lat, melhor.lon]).setContent(this._ficha(melhor)).openOn(this._map);
    },

    // Monta o popup com textContent (nunca innerHTML): dado externo não vira HTML
    _ficha(a) {
      const div = document.createElement('div');
      const linhas = [
        ['', a.indicativo],
        ['Rota', `${a.origem || '?'} → ${a.destino || '?'}`],
        ['Nível', a.fl === null ? '—' : 'FL' + String(a.fl).padStart(3, '0')],
        ['Velocidade', a.vel === null ? '—' : a.vel + ' kt'],
        ['Rumo', a.rumo + '°'],
        ['Fonte', a.fonte || '—'],
      ];
      if (a.alerta) linhas.push(['Alerta', a.alerta]);
      for (const [rotulo, valor] of linhas) {
        const linha = document.createElement('div');
        if (rotulo) {
          const b = document.createElement('b');
          b.textContent = rotulo + ': ';
          linha.appendChild(b);
        } else {
          linha.style.fontWeight = '700';
        }
        linha.appendChild(document.createTextNode(valor));
        div.appendChild(linha);
      }
      // Pede a rota ao Python (evento 'rota' tratado em main.py); clicar de novo oculta
      const botao = document.createElement('button');
      botao.className = 'aeronave-botao-rota';
      botao.textContent = 'Ver / ocultar rota';
      botao.onclick = () => emitEvent('rota', a.indicativo);
      div.appendChild(botao);
      return div;
    },
  });

  L.aeronavesLayer = (options) => new L.AeronavesLayer(options);
})();
