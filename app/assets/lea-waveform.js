/* <lea-waveform> -- the nested-line sound wave that runs under LEA's portrait.
   One carrier sine, amplitude-modulated by a row of gaussian humps, drawn as a stack of
   scaled copies: every line crosses zero together and fans out at the crests, which is what
   gives the reference its nested-leaf look. Colour is a single magenta-to-cyan sweep.
   Point `avatar` at a <lea-avatar> selector and it follows her voice; otherwise drive `level`.
   (`for` is avoided on purpose: React-style runtimes rewrite it.) */
(() => {
  if (customElements.get('lea-waveform')) return;

  const LINES = 30;
  const SAMPLES = 190;
  const CYCLES = 4.25;
  // Where the crests sit and how tall each one is: small at the edges, one big swell centred.
  const HUMPS = [
    [.09, .17], [.21, .36], [.33, .30], [.46, 1], [.59, .37], [.71, .47], [.84, .21],
  ];
  const STOPS = [[0, '#ff2d9b'], [.22, '#c233cf'], [.47, '#4140e6'], [.72, '#1f93dd'], [1, '#22e3d2']];

  const envelope = x => {
    let sum = .045;                                       // the line never fully flattens
    for (const [centre, height] of HUMPS) {
      const d = (x - centre) / .068;
      sum += height * Math.exp(-d * d);
    }
    return sum;
  };

  class LeaWaveform extends HTMLElement {
    connectedCallback() {
      if (this.canvas) return;
      const shadow = this.shadowRoot || this.attachShadow({mode: 'open'});
      shadow.innerHTML = `<style>
        :host{display:block;height:92px;background:#000}
        canvas{display:block;width:100%;height:100%}
      </style>`;
      this.canvas = document.createElement('canvas');
      shadow.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d');
      this.level = 0;
      this.drive = 0;
      this.phase = 0;
      this.last = 0;

      this.resize = () => {
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        this.canvas.width = Math.max(1, Math.round(this.clientWidth * ratio));
        this.canvas.height = Math.max(1, Math.round(this.clientHeight * ratio));
        this.gradient = null;
      };
      this.observer = new ResizeObserver(this.resize);
      this.observer.observe(this);
      this.resize();
      this.frame = requestAnimationFrame(now => this.draw(now));
    }

    disconnectedCallback() {
      if (this.frame) cancelAnimationFrame(this.frame);
      this.observer?.disconnect();
      this.canvas = null;
    }

    /** How hard the wave should be moving right now: her voice if she is wired up, else `level`. */
    demand() {
      if (!this.source) {
        const selector = this.getAttribute('avatar');
        this.source = document.querySelector(selector || 'lea-avatar');
      }
      if (!this.source) return this.level;
      if (this.source.getAttribute('expression') !== 'speaking') return 0;
      // Speaking always makes a real wave; her voice only decides how far it swells.
      return .62 + .38 * (this.source.speechLevel || 0);
    }

    draw(now) {
      if (!this.canvas) return;
      const step = this.last ? Math.min((now - this.last) / 1000, .1) : 0;
      this.last = now;

      const target = Math.min(1, Math.max(0, this.demand()));
      this.drive += (target - this.drive) * .12;
      // Resting she only breathes; speaking she scrolls, faster the louder she gets.
      this.phase += step * (.55 + 3.4 * this.drive);

      const {width, height} = this.canvas;
      const ctx = this.ctx;
      ctx.clearRect(0, 0, width, height);
      if (!this.gradient) {
        this.gradient = ctx.createLinearGradient(0, 0, width, 0);
        for (const [at, color] of STOPS) this.gradient.addColorStop(at, color);
      }

      const mid = height / 2;
      const reach = height * .46 * (.14 + .86 * this.drive);
      const wave = new Float32Array(SAMPLES);
      for (let i = 0; i < SAMPLES; i++) {
        const x = i / (SAMPLES - 1);
        wave[i] = envelope(x) * Math.sin((x * CYCLES + this.phase) * Math.PI * 2 - 1.07);
      }

      ctx.strokeStyle = this.gradient;
      ctx.lineWidth = Math.max(.65, width / 900);
      ctx.globalAlpha = .5;
      for (let line = 0; line < LINES; line++) {
        const scale = .06 + .94 * (line / (LINES - 1));
        ctx.beginPath();
        for (let i = 0; i < SAMPLES; i++) {
          const x = (i / (SAMPLES - 1)) * width;
          const y = mid - wave[i] * scale * reach;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      this.frame = requestAnimationFrame(next => this.draw(next));
    }
  }

  customElements.define('lea-waveform', LeaWaveform);
})();
