/* LEA: high-resolution reference on a deformable Three.js relief. */
(() => {
  const T = window.THREE;
  if (!T || customElements.get('lea-avatar')) return;
  const imageURL = new URL('lea-retrato.png', document.currentScript.src).href;
  const MOUTH = {rest: 0, ajar: 1, open: 2, wide: 3, round: 4, narrow: 5, gape: 6};
  // Which shape a letter asks for. Anything unlisted is a consonant: lips barely part.
  const LETTER_SHAPE = {};
  const assign = (letters, shape) => { for (const ch of letters) LETTER_SHAPE[ch] = MOUTH[shape]; };
  assign('aáàâãh', 'open');
  assign('eéêiíy', 'wide');
  assign('oóôõ', 'round');
  assign('uúüw', 'narrow');
  assign('mbp', 'rest');          // the lips close to make these at all
  assign('fv', 'ajar');
  const CONSONANT = MOUTH.ajar;

  // How far the jaw rides down for each shape, and how long each one holds. Vowels carry
  // a syllable, so they linger; consonants are passed through quickly.
  const APERTURE = [], BEATS = [];
  const shapeTiming = (name, aperture, beats) => { APERTURE[MOUTH[name]] = aperture; BEATS[MOUTH[name]] = beats; };
  shapeTiming('rest', 0, .55);
  shapeTiming('ajar', .3, .7);
  shapeTiming('open', .8, 1.3);
  shapeTiming('wide', .5, 1.15);
  shapeTiming('round', .62, 1.2);
  shapeTiming('narrow', .22, 1);
  shapeTiming('gape', 1, 1.6);

  /** A word becomes a run of shapes with the beats they hold for. */
  function visemesFor(word) {
    const shapes = [];
    for (const ch of word.toLowerCase()) {
      const shape = LETTER_SHAPE[ch] ?? (/[a-zà-ÿ]/.test(ch) ? CONSONANT : MOUTH.rest);
      if (shape !== shapes[shapes.length - 1]) shapes.push(shape);
    }
    if (!shapes.length) shapes.push(MOUTH.rest);
    const ends = [];
    let total = 0;
    for (const shape of shapes) ends.push(total += BEATS[shape]);
    return {shapes, ends, total};
  }

  // Loudness alone cannot tell us the shape, only how far the jaw is down.
  const apertureShape = level => level < .1 ? MOUTH.rest : level < .3 ? MOUTH.ajar
    : level < .7 ? MOUTH.open : MOUTH.gape;

  /* expression -> lid (0 open, 1 half, 2 shut), mouth frame or -1 for "driven by speech".
     `alert` runs the lens red while she thinks. `absorb` turns the traces a harder cyan and
     sends their pulses back down into the lens, so listening reads as her drinking in what
     you are saying or typing rather than broadcasting. */
  const MOODS = {
    neutral:   {lid: 0, mouth: MOUTH.rest, glow: 1,    flow: 1,   sway: 1,   alert: false, absorb: false},
    listening: {lid: 0, mouth: MOUTH.rest, glow: 1.25, flow: 1.8, sway: .8,  alert: false, absorb: true},
    thinking:  {lid: 1, mouth: MOUTH.rest, glow: 1,    flow: .55, sway: 1.6, alert: true,  absorb: false},
    speaking:  {lid: 0, mouth: -1,         glow: 1.15, flow: 1.4, sway: 1.1, alert: false, absorb: false},
  };
  // Fallback chatter for `expression="speaking"` with no real voice attached.
  const CHATTER = [MOUTH.ajar, MOUTH.open, MOUTH.wide, MOUTH.ajar, MOUTH.round, MOUTH.open, MOUTH.ajar, MOUTH.rest];

  class LeaAvatar extends HTMLElement {
    static get observedAttributes() { return ['expression', 'mouth']; }
    connectedCallback() {
      if (this.renderer) return;
      const shadow = this.shadowRoot || this.attachShadow({mode: 'open'});
      shadow.innerHTML = `<style>
        :host{display:block;min-height:220px;position:relative;background:#000;overflow:hidden}
        canvas{display:block;width:100%;height:100%}
        .fallback{display:grid;place-items:center;min-height:220px;color:#789;font:11px monospace;text-align:center;padding:16px}
        .fallback[hidden]{display:none}
      </style><div class="fallback" hidden>Avatar indisponível neste navegador.</div>`;
      try { this.start(shadow); } catch (error) {
        shadow.querySelector('.fallback').hidden = false;
        console.warn('LEA avatar:', error); this.dispose();
      }
    }
    attributeChangedCallback() {}
    disconnectedCallback() { this.dispose(); }
    start(shadow) {
      const renderer = this.renderer = new T.WebGLRenderer({antialias: true, powerPreference: 'high-performance'});
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.outputColorSpace = T.SRGBColorSpace;
      renderer.setClearColor(0x000000, 1);
      shadow.prepend(renderer.domElement);
      this.scene = new T.Scene();
      this.camera = new T.PerspectiveCamera(38, 1, .1, 100);
      this.stage = new T.Group(); this.scene.add(this.stage);
      this.jawOpen = this.alert = this.absorb = this.roundness = 0;
      this.pointer = {x: 0, y: 0};
      this.reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.onPointer = event => {
        const rect = this.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        this.pointer.x = (event.clientX - rect.left) / rect.width * 2 - 1;
        this.pointer.y = (event.clientY - rect.top) / rect.height * 2 - 1;
      };
      this.onLeave = () => { this.pointer.x = this.pointer.y = 0; };
      this.addEventListener('pointermove', this.onPointer);
      this.addEventListener('pointerleave', this.onLeave);
      this.resize = () => {
        if (!this.renderer) return;
        const w = Math.max(this.clientWidth, 1), h = Math.max(this.clientHeight, 1);
        renderer.setSize(w, h, false);
        this.camera.aspect = w / h;
        const aspect = 1191 / 1321;
        this.camera.position.z = Math.max(1, aspect / this.camera.aspect) * 1.09 / Math.tan(38 * Math.PI / 360) + .17;
        this.camera.updateProjectionMatrix();
      };
      this.resizeObserver = new ResizeObserver(this.resize); this.resizeObserver.observe(this); this.resize();
      const image = this.image = new Image();
      image.onload = () => {
        if (!this.renderer || !this.isConnected) return;
        try {
          this.buildRelief(image); this.started = performance.now();
          this.frame = requestAnimationFrame(now => this.draw(now));
        } catch (error) {
          console.warn('LEA avatar:', error); shadow.querySelector('.fallback').hidden = false; this.dispose();
        }
      };
      image.onerror = () => { shadow.querySelector('.fallback').hidden = false; };
      image.src = imageURL;
    }
    buildRelief(image) {
      const texture = new T.Texture(image);
      texture.colorSpace = T.SRGBColorSpace; texture.needsUpdate = true;
      texture.anisotropy = Math.min(4, this.renderer.capabilities.getMaxAnisotropy());
      const uniforms = this.uniforms = {
        portrait: {value: texture}, time: {value: 0}, mouth: {value: 0}, mouthShape: {value: 0},
        lid: {value: 0}, roundness: {value: 0}, alert: {value: 0}, absorb: {value: 0}, flow: {value: 1}, motion: {value: 1},
      };
      const material = new T.ShaderMaterial({
        uniforms, transparent: true, depthWrite: false,
        vertexShader: `
          uniform float mouth;
          varying vec2 vUv;
          void main() {
            vUv = uv;
            vec3 p = position;
            // A shallow dome with a raised optic lens and cheek; the original texture stays sharp.
            float dome = exp(-dot((uv-vec2(.52,.52))*vec2(2.5,2.0), (uv-vec2(.52,.52))*vec2(2.5,2.0)));
            float lens = exp(-dot((uv-vec2(.37,.55))*vec2(11.0,11.0), (uv-vec2(.37,.55))*vec2(11.0,11.0)));
            p.z += .16*dome + .045*lens;
            float jaw = exp(-dot((uv-vec2(.51,.30))*vec2(8.0,9.0), (uv-vec2(.51,.30))*vec2(8.0,9.0)));
            p.y -= mouth * .008 * jaw;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
          }
        `,
        fragmentShader: `
          uniform sampler2D portrait;
          uniform float time, mouth, roundness, lid, alert, absorb, flow, motion;
          varying vec2 vUv;
          void main() {
            vec2 uv = vUv;
            // Follow the slanted lip seam in the portrait. A small, even jaw shift keeps
            // the lower lip from being stretched into a point as the mouth opens; the
            // opening tapers at the corners and rounding stays subtle during speech.
            float seam = .366 - (uv.x - .503) * .13;
            float halfWidth = mix(.047, .044, roundness);
            float across = abs((uv.x-.503) / halfWidth);
            float taper = pow(max(0.0, 1.0-across*across), .8);
            float drop = mouth * .012 * taper;
            vec2 region = (uv - vec2(.503,.340)) / vec2(.085,.085);
            float jawArea = 1.0-smoothstep(.50,1.35,dot(region,region));
            float belowSeam = 1.0-smoothstep(seam-.001,seam+.001,uv.y);
            vec2 sampleUv = uv;
            sampleUv.y += mouth * .012 * jawArea * belowSeam;
            sampleUv.x += (uv.x-.503) * roundness * mouth * .012 * jawArea;
            // Sample the same fine pixel-art grid for both lips and the surrounding face.
            vec2 pixelGrid = vec2(150.0, 166.0);
            vec2 pixelUv = (floor(sampleUv * pixelGrid) + .5) / pixelGrid;
            vec4 color = texture2D(portrait, pixelUv);
            float topEdge = seam;
            float bottomEdge = seam - drop;
            float opening = (1.0-smoothstep(topEdge-.0005,topEdge+.0005,uv.y));
            opening *= smoothstep(bottomEdge-.0005,bottomEdge+.0005,uv.y);
            opening *= (1.0-smoothstep(.92,1.0,across)) * smoothstep(.06,.25,mouth);
            // The opening uses the same absolute black as the panel background.
            color.rgb = mix(color.rgb, vec3(0.0), opening);
            // A skin-toned eyelid closes over the visible eye without changing the rest of the art.
            vec2 eye = (uv - vec2(.629,.553)) / vec2(.065,.030);
            float eyeArea = 1.0 - smoothstep(.72,1.05,dot(eye,eye));
            float cover = smoothstep(1.05-lid*2.15, 1.22-lid*2.15, eye.y) * eyeArea * smoothstep(.02,.3,lid);
            vec3 skin = texture2D(portrait, vec2(uv.x, .588)).rgb;
            color.rgb = mix(color.rgb, skin*.85, cover);
            // Only saturated light sources on the mechanical side or in the lower circuitry
            // receive moving energy; the hair and skin retain their original colour.
            float high = max(max(color.r,color.g),color.b);
            float low = min(min(color.r,color.g),color.b);
            float cyan = smoothstep(.12,.5,color.g+color.b-2.0*color.r) * smoothstep(.14,.48,high);
            float neonPink = smoothstep(.16,.55,color.r+color.b-2.0*color.g) * smoothstep(.3,.68,high);
            float zone = max(1.0-smoothstep(.43,.51,uv.x), smoothstep(.83,.91,uv.x));
            zone = max(zone, 1.0-smoothstep(.23,.30,uv.y));
            float circuitry = max(cyan, neonPink*.7) * zone * smoothstep(.05,.3,high-low);
            float dist = length((uv-vec2(.37,.55))*vec2(1.1,1.0));
            float direction = mix(1.0,-1.0,absorb);
            float travel = fract(dist*4.8 - time*.42*flow*direction);
            float pulse = pow(max(0.0,1.0-abs(travel-.5)*5.0),3.0) * motion;
            color.rgb += color.rgb * circuitry * (.10 + pulse*(.65+.25*absorb));
            float lens = 1.0-smoothstep(.04,.085,length((uv-vec2(.37,.55))*vec2(1.0,1.0)));
            color.rgb = mix(color.rgb, vec3(high, high*.08, high*.10), lens*cyan*alert*.8);
            if (color.a < .005) discard;
            gl_FragColor = color;
            #include <colorspace_fragment>
          }
        `,
      });
      const geometry = new T.PlaneGeometry(2 * 1191 / 1321, 2, 160, 176);
      this.stage.add(new T.Mesh(geometry, material));
    }
    get speechLevel() { return this.jawOpen || 0; }
    speak(text, options = {}) {
      const synth = window.speechSynthesis;
      if (!synth) return Promise.reject(new Error('SpeechSynthesis indisponível neste navegador.'));
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = options.lang || 'pt-BR';
      if (options.voice) utterance.voice = options.voice;
      if (options.rate) utterance.rate = options.rate;
      if (options.pitch) utterance.pitch = options.pitch;
      if (options.volume != null) utterance.volume = options.volume;

      const previous = this.getAttribute('expression');
      this.setAttribute('expression', 'speaking');
      const rate = utterance.rate || 1;
      let lastBoundary = 0, unit = 74 / rate;
      utterance.addEventListener('boundary', event => {
        if (event.name === 'sentence') return;
        const now = performance.now();
        const word = text.slice(event.charIndex, event.charIndex + (event.charLength || 14)).split(/\s/)[0];
        const script = visemesFor(word);
        // The gap since the previous word is the only clock the synthesiser gives us. Trust it,
        // but ease into it so one long word does not jerk the whole cadence.
        if (lastBoundary && this.speech) {
          const measured = (now - lastBoundary) / this.speech.total;
          unit = Math.min(150, Math.max(42, unit + (measured - unit) * .5));
        }
        lastBoundary = now;
        this.speech = {...script, startedAt: now, unit};
      });

      const settle = () => {
        this.speech = null;
        if (this.getAttribute('expression') === 'speaking') {
          if (previous) this.setAttribute('expression', previous);
          else this.removeAttribute('expression');
        }
      };
      return new Promise((resolve, reject) => {
        utterance.addEventListener('end', () => { settle(); resolve(); });
        utterance.addEventListener('error', event => { settle(); reject(event.error || new Error('fala interrompida')); });
        synth.speak(utterance);
      });
    }

    /** Drive the mouth from live audio: an <audio>/<video> element, a MediaStream or an AudioNode. */
    attachAudio(source) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx || !source) return null;
      this.detachAudio();
      const isMedia = source instanceof HTMLMediaElement;
      const isStream = typeof MediaStream !== 'undefined' && source instanceof MediaStream;
      const context = (!isMedia && !isStream && source.context) || source.__leaSource?.context ||
        (this.audioContext || (this.audioContext = new Ctx()));
      let node;
      if (isStream) {
        node = context.createMediaStreamSource(source);
      } else if (isMedia) {
        node = source.__leaSource || (source.__leaSource = context.createMediaElementSource(source));
        if (!source.__leaAudible) { node.connect(context.destination); source.__leaAudible = true; }
      } else {
        node = source;
      }
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = .6;
      node.connect(analyser);
      this.audioNode = node;
      this.analyser = analyser;
      this.samples = new Uint8Array(analyser.fftSize);
      if (context.state === 'suspended') context.resume();
      return analyser;
    }

    detachAudio() {
      if (this.audioNode && this.analyser) this.audioNode.disconnect(this.analyser);
      this.analyser?.disconnect();
      this.audioNode = null;
      this.analyser = null;
      this.samples = null;
      this.level = null;
    }

    /** Smoothed 0-1 loudness of the attached audio, or null when nothing is attached. */
    voiceLevel() {
      if (!this.analyser) return null;
      this.analyser.getByteTimeDomainData(this.samples);
      let sum = 0;
      for (let i = 0; i < this.samples.length; i++) {
        const deviation = (this.samples[i] - 128) / 128;
        sum += deviation * deviation;
      }
      const rms = Math.sqrt(sum / this.samples.length);
      const level = Math.min(1, rms * 4.5);                 // speech rarely clips the full range
      this.level = this.level == null ? level : this.level + (level - this.level) * .45;
      return this.level;
    }

    /** Which mouth frame this instant calls for, and how wide, in priority order. */
    mouthFrame(t, mood, reduced) {
      const supplied = Number.parseFloat(this.getAttribute('mouth'));
      if (Number.isFinite(supplied)) {
        const level = Math.min(1, Math.max(0, supplied));
        return {shape: apertureShape(level), open: level};
      }

      const voice = this.voiceLevel();
      if (this.speech) {
        const beats = (performance.now() - this.speech.startedAt) / this.speech.unit;
        // Past the last beat the word is over: lips settle until the next boundary lands.
        let shape = MOUTH.rest;
        if (beats < this.speech.total) {
          let i = 0;
          while (beats > this.speech.ends[i]) i++;
          shape = this.speech.shapes[i];
        }
        // With audio attached the voice says whether the mouth moves; the text says its shape.
        if (voice != null) {
          return voice < .07 ? {shape: MOUTH.rest, open: 0} : {shape, open: Math.max(voice, APERTURE[shape] * .5)};
        }
        return {shape, open: APERTURE[shape]};
      }

      if (voice != null) return {shape: apertureShape(voice), open: voice};
      if (mood.mouth === -1) {
        const shape = reduced ? MOUTH.ajar : CHATTER[Math.floor(t * 8) % CHATTER.length];
        return {shape, open: APERTURE[shape]};
      }
      return {shape: mood.mouth, open: APERTURE[mood.mouth]};
    }

    draw(now) {
      if (!this.renderer || !this.uniforms) return;
      const t = Math.max(0, (now-this.started)/1000), reduced = this.reduced.matches;
      const mood = MOODS[this.getAttribute('expression')] || MOODS.neutral;
      const mouth = this.mouthFrame(t, mood, reduced);
      const elapsed = Math.min(.1, Math.max(0, (now-(this.lastFrame || now))/1000));
      this.lastFrame = now;
      const speed = mouth.open > this.jawOpen ? 13 : 9;
      this.jawOpen += (mouth.open-this.jawOpen) * (1-Math.exp(-speed*elapsed));
      const rounded = mouth.shape === MOUTH.round || mouth.shape === MOUTH.narrow ? 1 : 0;
      this.roundness += (rounded-this.roundness) * (1-Math.exp(-9*elapsed));
      const settle = (v,target) => v + (target-v)*.055;
      this.alert = settle(this.alert, mood.alert ? 1 : 0);
      this.absorb = settle(this.absorb, mood.absorb ? 1 : 0);
      const cycle = 4.4, left = cycle - (t % cycle);
      const blink = !reduced && left < .26 ? (left > .2 || left < .06 ? .55 : 1) : 0;
      const u = this.uniforms;
      u.time.value = t; u.mouth.value = this.jawOpen; u.mouthShape.value = mouth.shape; u.roundness.value = this.roundness;
      u.lid.value = Math.max(blink, mood.lid ? .38 : 0);
      u.alert.value = this.alert; u.absorb.value = this.absorb; u.flow.value = mood.flow;
      u.motion.value = reduced ? 0 : 1;
      const sway = reduced ? 0 : mood.sway;
      const yaw = reduced ? 0 : Math.sin(t*.42)*.035*sway-this.pointer.x*.065;
      const pitch = reduced ? 0 : Math.sin(t*.31)*.018*sway+this.pointer.y*.04;
      this.stage.rotation.y += (yaw-this.stage.rotation.y)*.07;
      this.stage.rotation.x += (pitch-this.stage.rotation.x)*.07;
      this.stage.position.y = reduced ? 0 : Math.sin(t*.65)*.012;
      this.renderer.render(this.scene,this.camera);
      this.frame = requestAnimationFrame(next => this.draw(next));
    }
    dispose() {
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = null; this.lastFrame = null; this.resizeObserver?.disconnect();
      this.removeEventListener('pointermove',this.onPointer); this.removeEventListener('pointerleave',this.onLeave);
      if (this.speech) window.speechSynthesis?.cancel();
      this.speech = null; this.detachAudio(); this.audioContext?.close(); this.audioContext = null;
      if (this.image) { this.image.onload = this.image.onerror = null; this.image = null; }
      if (this.scene) this.scene.traverse(object => {
        object.geometry?.dispose();
        if (object.material) {
          object.material.uniforms?.portrait?.value?.dispose(); object.material.dispose();
        }
      });
      this.renderer?.dispose(); this.renderer?.domElement.remove();
      this.renderer = this.scene = this.uniforms = null;
    }
  }
  customElements.define('lea-avatar', LeaAvatar);
})();
