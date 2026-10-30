/* LEA's portrait: the reference artwork rebuilt in Three.js as a lit voxel relief.
   Every pixel of the 96x96 map in lea-portrait.js becomes a box whose depth comes from a
   blurred silhouette, so the head domes, the optic implant floats and the neon traces hover
   in front of it. Public attributes: expression and mouth (0-1). */
(() => {
  const T = window.THREE;
  const ART = window.LEA_PORTRAIT;
  if (!T || !ART || customElements.get('lea-avatar')) return;

  const W = ART.width, H = ART.height, CELLS = W * H;
  const MID_X = (W - 1) / 2, MID_Y = (H - 1) / 2;
  const CROWN = ART.crown || 0;                             // rows the generator rebuilt on top
  const CLASS = Object.fromEntries(ART.classNames.map((name, i) => [name, i]));
  const CODE = new Map(Array.from(ART.alphabet, (ch, i) => [ch, i]));
  const COLORS = ART.palette.map(hex => new T.Color('#' + hex));
  const LUMA = COLORS.map(c => .2126 * c.r + .7152 * c.g + .0722 * c.b);
  const CLASS_OF = ART.paletteClass;
  // The artwork's background is near-black; snap it to true black so the 96x96 plate melts
  // into the card behind it instead of reading as a panel laid on top.
  COLORS.forEach((color, entry) => { if (CLASS_OF[entry] === CLASS.void) color.setRGB(0, 0, 0); });
  // Neon entries skip the lighting pass so the circuitry keeps the artwork's full chroma.
  const UNLIT = new Set([CLASS.void, CLASS.circuit, CLASS.implant, CLASS.back]);
  const isNeon = entry => UNLIT.has(CLASS_OF[entry]) || (CLASS_OF[entry] === CLASS.warm && LUMA[entry] > .3);

  /* While she is thinking the lens runs red. Rotating each palette entry's hue rather than
     flooding it with one red keeps the artwork's own shading inside the disc: the hot core
     stays hot, the rim stays a rim. */
  const ALERT = COLORS.map(color => {
    const hsl = {h: 0, s: 0, l: 0};
    color.getHSL(hsl);
    return new T.Color().setHSL(.995, Math.min(1, hsl.s * 1.15 + .34), hsl.l * .8);
  });
  const HALO_CALM = new T.Color(0x6ef4ff), HALO_ALERT = new T.Color(0xff3b42);

  const FOV = 38;
  const SPREAD = Math.tan(FOV * Math.PI / 360);             // half the vertical frustum
  const MARGIN = 1.05;                                      // a little air around her

  function decode(text, out) {
    let at = 0;
    for (let i = 0; i < text.length; i += 2) {
      const value = CODE.get(text[i]), run = CODE.get(text[i + 1]) + 1;
      out.fill(value, at, at + run);
      at += run;
    }
    return out;
  }

  const PIXELS = decode(ART.pixels, new Uint8Array(CELLS));

  /* Depth relief -----------------------------------------------------------
     Blurring the silhouette a few times turns it into a smooth height field: thick
     areas (the skull, the cheek) rise, thin ones (stray locks, traces) stay flat. */
  const blur = (src, radius) => {
    const span = radius * 2 + 1, mid = new Float32Array(CELLS), out = new Float32Array(CELLS);
    const clamp = (v, limit) => v < 0 ? 0 : v > limit - 1 ? limit - 1 : v;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) sum += src[y * W + clamp(x + k, W)];
      mid[y * W + x] = sum / span;
    }
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      let sum = 0;
      for (let k = -radius; k <= radius; k++) sum += mid[clamp(y + k, H) * W + x];
      out[y * W + x] = sum / span;
    }
    return out;
  };
  let relief = new Float32Array(CELLS);
  for (let i = 0; i < CELLS; i++) {
    const cid = CLASS_OF[PIXELS[i]];
    relief[i] = (cid === CLASS.void || cid === CLASS.back) ? 0 : 1;
  }
  for (let pass = 0; pass < 3; pass++) relief = blur(relief, 6);

  const BACK_PLANE = -11;
  function frontOf(x, y, cid) {
    if (cid === CLASS.back) return -19;                       // out-of-focus city lights
    if (cid === CLASS.void) return -13;
    if (cid === CLASS.implant) {                              // the lens bulges out of its socket
      const d = Math.hypot(x - ART.implant.cx, y - ART.implant.cy) / ART.implant.r;
      return 16 + 6 * Math.sqrt(Math.max(0, 1 - d * d));
    }
    const dome = Math.pow(relief[y * W + x], .55);
    if (cid === CLASS.circuit) return 13 + 2.5 * dome;        // traces hover off the skin
    const turned = (58 - x) * .15;                            // she is three-quarter on to the lens
    return BACK_PLANE + 2 + 27 * dome + turned + (cid === CLASS.hair ? 1.8 : 0);
  }
  // Solid classes extrude back to BACK_PLANE; these are slabs that hang free of the head.
  const floats = cid => cid === CLASS.circuit || cid === CLASS.implant || cid === CLASS.back || cid === CLASS.void;

  const inBox = (x, y, box) => x >= box[0] && x < box[0] + box[2] && y >= box[1] && y < box[1] + box[3];
  const inFeature = (x, y) => inBox(x, y, ART.eye.box) || inBox(x, y, ART.mouth.box);

  /* How much of the jaw swing each cell takes. The gradient is what makes this work: the lip
     line holds still and the chin travels furthest, so neighbouring rows never pull more than
     a fraction of a cell apart and the relief stays watertight while the mouth opens. */
  const ramp = (value, from, to) => Math.min(1, Math.max(0, (value - from) / (to - from)));
  function jawPull(x, y, cid) {
    if (cid === CLASS.circuit || cid === CLASS.implant || cid === CLASS.back) return 0;
    if (cid === CLASS.void && !inBox(x, y, ART.mouth.box)) return 0;
    const row = y - CROWN;
    return ramp(row, 58, 70) * ramp(row, 82, 74) * ramp(x, 40, 45) * ramp(x, 74, 68);
  }

  function gradientTexture(size, paint) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    paint(canvas.getContext('2d'));
    const texture = new T.CanvasTexture(canvas);
    texture.colorSpace = T.SRGBColorSpace;                    // or the renderer re-encodes it bright
    return texture;
  }

  /* A soft radial sprite stands in for a bloom pass, which the UMD build has no composer for. */
  const glowTexture = () => gradientTexture(64, ctx => {
    const grad = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, 'rgba(255,255,255,.5)');
    grad.addColorStop(.4, 'rgba(255,255,255,.1)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 64, 64);
  });

  /* Lip sync ---------------------------------------------------------------
     Mouth frame 0 is the resting artwork; lea-portrait.js supplies the visemes after it. */
  const MOUTH = {rest: 0};
  ART.mouth.shapes.forEach((name, i) => { MOUTH[name] = i + 1; });

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
      if (this.scene) return;
      const shadow = this.shadowRoot || this.attachShadow({mode: 'open'});
      shadow.innerHTML = `<style>
        :host{display:block;min-height:220px;position:relative;background:#000;overflow:hidden}
        canvas{display:block;width:100%;height:100%}
        .vignette{position:absolute;inset:0;pointer-events:none;
          background:radial-gradient(ellipse at 48% 46%,rgba(0,0,0,0) 46%,rgba(0,0,0,.34) 86%,rgba(0,0,0,.7) 100%)}
        .fallback{display:grid;place-items:center;min-height:220px;color:#789;font:11px monospace;text-align:center;padding:16px}
        .fallback[hidden]{display:none}
      </style><div class="fallback" hidden>Avatar 3D indisponível neste navegador.</div><div class="vignette"></div>`;
      try {
        this.start(shadow);
      } catch (err) {
        shadow.querySelector('.fallback').hidden = false;
        console.warn('LEA avatar:', err);
        this.dispose();
      }
    }

    attributeChangedCallback() { /* values are read on the next animation frame */ }
    disconnectedCallback() { this.dispose(); }

    start(shadow) {
      const renderer = this.renderer = new T.WebGLRenderer({antialias: true, powerPreference: 'low-power'});
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
      renderer.outputColorSpace = T.SRGBColorSpace;
      renderer.setClearColor(0x000000, 1);                    // absolute black, flush with the card
      shadow.prepend(renderer.domElement);                    // the vignette overlay stays on top

      const scene = this.scene = new T.Scene();
      this.camera = new T.PerspectiveCamera(FOV, 1, 1, 600);
      const stage = this.stage = new T.Group();
      scene.add(stage);

      scene.add(new T.AmbientLight(0xffffff, 2.78));
      const key = (color, intensity, x, y, z) => {
        const light = new T.DirectionalLight(color, intensity);
        light.position.set(x, y, z);
        scene.add(light);
      };
      key(0x5ff4ff, .79, -60, 18, 70);   // implant side
      key(0xff4f9d, .55, 70, 30, 55);    // hair side
      key(0xffffff, .36, 0, 80, 40);

      this.buildCells(stage);
      this.buildGlow(stage);
      this.buildFeatures();

      this.reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
      this.pointer = {x: 0, y: 0};
      this.onPointer = event => {
        const rect = this.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        this.pointer.y = ((event.clientY - rect.top) / rect.height) * 2 - 1;
      };
      this.onLeave = () => { this.pointer.x = this.pointer.y = 0; };
      this.addEventListener('pointermove', this.onPointer);
      this.addEventListener('pointerleave', this.onLeave);

      this.resize = () => {
        if (!this.renderer) return;
        const w = Math.max(this.clientWidth, 1), h = Math.max(this.clientHeight, 1);
        renderer.setSize(w, h, false);
        this.camera.aspect = w / h;
        // `fitY` já é exato; o horizontal se resolve aqui, com o aspecto em mãos.
        let wide = 0;
        for (let i = 0; i < this.reachZ.length; i++) {
          const need = this.reachZ[i] + this.reachX[i] / this.camera.aspect;
          if (need > wide) wide = need;
        }
        this.camera.position.z = Math.max(this.fitY, wide);
        this.camera.updateProjectionMatrix();
      };
      this.resizeObserver = new ResizeObserver(this.resize);
      this.resizeObserver.observe(this);
      this.resize();

      this.started = performance.now();
      this.frame = requestAnimationFrame(now => this.draw(now));
    }

    /* One instanced box per pixel, split into a lit mesh (skin, hair, lips) and an
       unlit one (implant, traces, bokeh) that keeps the neon at full strength. */
    buildCells(stage) {
      // Every cell is placed, unlit background included: the void slab keeps the shoulder and
      // the hair shadows reading as solid mass instead of fragments floating over the backdrop.
      // Cells inside the eye and mouth boxes always stay lit, dark ones included: the lids and
      // the jaw repaint them, so they need a slot in the mesh setFeature writes to.
      const litCells = [], neonCells = [];
      for (let index = 0; index < CELLS; index++) {
        const unlit = isNeon(PIXELS[index]) && !inFeature(index % W, (index / W) | 0);
        (unlit ? neonCells : litCells).push(index);
      }

      const box = new T.BoxGeometry(1, 1, 1);
      box.setAttribute('color', new T.BufferAttribute(new Float32Array(box.attributes.position.count * 3).fill(1), 3));
      const makeMesh = (count, material) => {
        const mesh = new T.InstancedMesh(box, material, count);
        mesh.frustumCulled = false;
        return mesh;
      };
      this.litMesh = makeMesh(litCells.length, new T.MeshLambertMaterial({vertexColors: true}));
      this.neonMesh = makeMesh(neonCells.length, new T.MeshBasicMaterial({vertexColors: true}));

      this.slotOf = new Int32Array(CELLS).fill(-1);
      this.neonEntry = new Uint8Array(neonCells.length);
      this.neonClass = new Uint8Array(neonCells.length);
      this.neonFlow = new Float32Array(neonCells.length);
      this.neonAngle = new Float32Array(neonCells.length);
      this.glowSeeds = [];

      const matrix = new T.Matrix4(), position = new T.Vector3(), scale = new T.Vector3();
      const quaternion = new T.Quaternion();
      const jaw = [{mesh: this.litMesh, cells: []}, {mesh: this.neonMesh, cells: []}];
      // Framing budget. A cell at depth `front` and height |y| stays on screen only while the
      // camera sits at least `front + |y| / SPREAD` away, so tracking that maximum while placing
      // gives an exact fit: the relief can never push the crown of her head off the card.
      this.nearest = 0;
      this.fitY = 0;
      const reachX = [], reachZ = [];     // o par (profundidade, |x|) de cada célula visível
      const place = (mesh, slot, index, hinge) => {
        const x = index % W, y = (index / W) | 0, entry = PIXELS[index], cid = CLASS_OF[entry];
        const front = frontOf(x, y, cid);
        const back = floats(cid) ? front - 2.2 : BACK_PLANE;
        position.set(x - MID_X, MID_Y - y, (front + back) / 2);
        scale.set(1, 1, Math.max(front - back, .6));
        mesh.setMatrixAt(slot, matrix.compose(position, quaternion, scale));
        mesh.setColorAt(slot, COLORS[entry]);
        if (front > this.nearest) this.nearest = front;
        if (cid !== CLASS.void) {                         // the black slab may run off the edges
          this.fitY = Math.max(this.fitY, front + (Math.abs(position.y) + .5) * MARGIN / SPREAD);
          reachZ.push(front);
          reachX.push((Math.abs(position.x) + .5) * MARGIN / SPREAD);
        }
        const pull = jawPull(x, y, cid);
        if (pull > .001) hinge.cells.push({slot, pull, restY: position.y});
        return {x, y, entry, cid, front};
      };

      litCells.forEach((index, slot) => {
        place(this.litMesh, slot, index, jaw[0]);
        this.slotOf[index] = slot;
      });
      neonCells.forEach((index, slot) => {
        const cell = place(this.neonMesh, slot, index, jaw[1]);
        this.neonEntry[slot] = cell.entry;
        this.neonClass[slot] = cell.cid;
        this.neonFlow[slot] = Math.hypot(cell.x - ART.implant.cx, cell.y - ART.implant.cy);
        this.neonAngle[slot] = Math.atan2(cell.y - ART.implant.cy, cell.x - ART.implant.cx);
        if (LUMA[cell.entry] > .22) this.glowSeeds.push(cell);
      });

      // Flatten the hinge into typed arrays: the Y translation of an instance matrix lives at
      // element 13, so a swing is one pass of float writes rather than rebuilt matrices.
      this.jaw = jaw.filter(part => part.cells.length).map(({mesh, cells}) => ({
        mesh,
        offset: Int32Array.from(cells, cell => cell.slot * 16 + 13),
        restY: Float32Array.from(cells, cell => cell.restY),
        pull: Float32Array.from(cells, cell => cell.pull),
      }));
      // O horizontal depende do aspecto, que só se conhece no resize, então guarda-se o par
      // por célula em vez de um único número: senão o ajuste teria de supor que a célula mais
      // larga é também a mais próxima, e reservaria margem que ninguém usa.
      this.reachZ = Float32Array.from(reachZ);
      this.reachX = Float32Array.from(reachX);
      this.jawOpen = 0;
      this.jawApplied = -1;
      this.alert = 0;
      this.absorb = 0;

      stage.add(this.litMesh, this.neonMesh);
    }

    /** 0-1, how far her mouth is open right now: the hook for anything syncing to her voice. */
    get speechLevel() { return this.jawOpen || 0; }

    /** Carry the lens and trace colours through to their bloom and halo. */
    paintGlow(alert, absorb) {
      if (Math.abs(alert - this.lensAlert) < .01 && Math.abs(absorb - this.lensAbsorb) < .01) return;
      this.lensAlert = alert;
      this.lensAbsorb = absorb;
      const tint = new T.Color();
      for (let i = 0; i < this.lensGlow.slot.length; i++) {
        const entry = this.lensGlow.entry[i];
        tint.copy(COLORS[entry]);
        if (this.lensGlow.cid[i] === CLASS.implant) {
          tint.lerp(ALERT[entry], alert);
        } else {
          tint.setRGB(tint.r * (1 - absorb * .45), tint.g, tint.b).multiplyScalar(1 + absorb * .55);
        }
        this.glowMesh.setColorAt(this.lensGlow.slot[i], tint.multiplyScalar(this.lensGlow.scale[i]));
      }
      this.glowMesh.instanceColor.needsUpdate = true;
      this.halo.material.color.copy(HALO_CALM).lerp(HALO_ALERT, alert);
    }

    /** Swing the jaw `drop` cells down, graded by each cell's share of the hinge. */
    applyJaw(drop) {
      if (Math.abs(drop - this.jawApplied) < .004) return;
      this.jawApplied = drop;
      for (const part of this.jaw) {
        const matrices = part.mesh.instanceMatrix.array;
        for (let i = 0; i < part.offset.length; i++) {
          matrices[part.offset[i]] = part.restY[i] - drop * part.pull[i];
        }
        part.mesh.instanceMatrix.needsUpdate = true;
      }
    }

    buildGlow(stage) {
      const sprite = new T.PlaneGeometry(1, 1);
      const material = new T.MeshBasicMaterial({
        map: glowTexture(), transparent: true, blending: T.AdditiveBlending,
        depthWrite: false, vertexColors: true,
      });
      sprite.setAttribute('color', new T.BufferAttribute(new Float32Array(sprite.attributes.position.count * 3).fill(1), 3));
      const mesh = this.glowMesh = new T.InstancedMesh(sprite, material, this.glowSeeds.length);
      mesh.frustumCulled = false;
      mesh.renderOrder = 2;

      const matrix = new T.Matrix4(), position = new T.Vector3(), scale = new T.Vector3();
      const quaternion = new T.Quaternion(), tint = new T.Color();
      const lensGlow = [];
      this.glowSeeds.forEach((cell, slot) => {
        const strength = Math.min(1, LUMA[cell.entry] * 2.1);
        const spread = 2.2 + 2.6 * strength;
        position.set(cell.x - MID_X, MID_Y - cell.y, cell.front + 1.2);
        scale.set(spread, spread, 1);
        mesh.setMatrixAt(slot, matrix.compose(position, quaternion, scale));
        mesh.setColorAt(slot, tint.copy(COLORS[cell.entry]).multiplyScalar(.07 + .13 * strength));
        // The bloom has to follow its source, or a red eye keeps a cyan halo and the traces
        // brighten without their glow. Only these two classes ever change colour.
        if (cell.cid === CLASS.implant || cell.cid === CLASS.circuit) {
          lensGlow.push({slot, entry: cell.entry, strength, cid: cell.cid});
        }
      });
      this.lensGlow = {
        slot: Int32Array.from(lensGlow, g => g.slot),
        entry: Uint8Array.from(lensGlow, g => g.entry),
        scale: Float32Array.from(lensGlow, g => .07 + .13 * g.strength),
        cid: Uint8Array.from(lensGlow, g => g.cid),
      };
      this.lensAlert = -1;
      this.lensAbsorb = -1;
      stage.add(mesh);

      this.halo = new T.Mesh(sprite, new T.MeshBasicMaterial({
        map: material.map, color: HALO_CALM.clone(), transparent: true,
        blending: T.AdditiveBlending, depthWrite: false, opacity: .16,
      }));
      this.halo.position.set(ART.implant.cx - MID_X, MID_Y - ART.implant.cy, 24);
      this.halo.scale.setScalar(ART.implant.r * 2.6);
      this.halo.renderOrder = 3;
      stage.add(this.halo);
    }

    // Feature frames: index 0 is the untouched artwork, then progressively lidded / opened.
    buildFeatures() {
      const crop = box => {
        const [x0, y0, w, h] = box, out = new Uint8Array(w * h);
        for (let yy = 0; yy < h; yy++) for (let xx = 0; xx < w; xx++) out[yy * w + xx] = PIXELS[(y0 + yy) * W + x0 + xx];
        return out;
      };
      const unpack = (box, text) => decode(text, new Uint8Array(box[2] * box[3]));
      this.lids = {box: ART.eye.box, applied: -1, frames: [crop(ART.eye.box),
        unpack(ART.eye.box, ART.eye.half), unpack(ART.eye.box, ART.eye.shut)]};
      this.lips = {box: ART.mouth.box, applied: -1, frames: [crop(ART.mouth.box),
        ...ART.mouth.frames.map(text => unpack(ART.mouth.box, text))]};
    }

    setFeature(feature, state) {
      if (feature.applied === state) return;
      feature.applied = state;
      const [x0, y0, w, h] = feature.box, frame = feature.frames[state];
      for (let yy = 0; yy < h; yy++) for (let xx = 0; xx < w; xx++) {
        const slot = this.slotOf[(y0 + yy) * W + x0 + xx];
        if (slot >= 0) this.litMesh.setColorAt(slot, COLORS[frame[yy * w + xx]]);
      }
      this.litMesh.instanceColor.needsUpdate = true;
    }

    /* Lip sync ------------------------------------------------------------
       Two drivers feed the same mouth: `speak` times visemes off the synthesiser's own word
       boundaries, `attachAudio` reads loudness off any playing audio. Used together, the
       voice decides how far the jaw drops and the text decides the shape. */

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
      const context = this.audioContext || (this.audioContext = new Ctx());
      let node;
      if (typeof MediaStream !== 'undefined' && source instanceof MediaStream) {
        node = context.createMediaStreamSource(source);
      } else if (source instanceof HTMLMediaElement) {
        // An element can only be tapped once, so reuse the node on replays.
        node = source.__leaSource || (source.__leaSource = context.createMediaElementSource(source));
        node.connect(context.destination);                  // keep it audible
      } else {
        node = source;
      }
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = .6;
      node.connect(analyser);
      this.analyser = analyser;
      this.samples = new Uint8Array(analyser.fftSize);
      if (context.state === 'suspended') context.resume();
      return analyser;
    }

    detachAudio() {
      this.analyser?.disconnect();
      this.analyser = null;
      this.samples = null;
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

    /* Pulses chase outward from the implant along the traces; the lens breathes and sweeps.
       `alert` (0-1) crossfades the lens to red while she thinks; `absorb` (0-1) hardens the
       traces to cyan and turns their pulses around, inbound to the lens. */
    paintNeon(t, mood, alert, absorb) {
      const tint = new T.Color();
      const span = 34, head = (t * 26 * mood.flow) % span;
      const breath = .86 + .14 * Math.sin(t * 2.1);
      // Each inbound wave lands on the lens when the cycle wraps, and the lens drinks it.
      const arrival = mood.absorb ? Math.pow(1 - Math.min(head, span - head) / (span * .5), 8) : 0;
      for (let slot = 0; slot < this.neonEntry.length; slot++) {
        const entry = this.neonEntry[slot], base = COLORS[entry], cid = this.neonClass[slot];
        if (cid === CLASS.void) continue;                   // the backdrop slab never animates
        let gain = mood.glow, lift = 0;
        let r = base.r, g = base.g, b = base.b;
        if (cid === CLASS.circuit) {
          // Outward by default; while absorbing, the wave runs back down the trace into the lens.
          const wave = mood.absorb ? this.neonFlow[slot] + head : this.neonFlow[slot] - head;
          const offset = ((wave % span) + span) % span;
          const pulse = offset < 5 ? (1 - offset / 5) ** 2 : 0;
          gain *= breath * (1 + absorb * .55);
          r *= 1 - absorb * .45;                            // draining red leaves a harder cyan
          lift = pulse * (.55 + absorb * .3);
        } else if (cid === CLASS.implant) {
          const sweep = Math.cos(this.neonAngle[slot] - t * 1.9 * mood.flow);
          gain *= .84 + .16 * Math.sin(t * 2.4 - this.neonFlow[slot] * .45);
          lift = .3 * Math.pow(Math.max(0, sweep), 10) + arrival * .3;
          if (alert > 0) {
            const red = ALERT[entry];
            r += (red.r - r) * alert;
            g += (red.g - g) * alert;
            b += (red.b - b) * alert;
          }
        } else if (cid === CLASS.back) {
          gain *= .9 + .1 * Math.sin(t * .7 + this.neonFlow[slot]);
        }
        tint.setRGB(
          Math.min(1, r * gain + lift),
          Math.min(1, g * gain + lift),
          Math.min(1, b * gain + lift));
        this.neonMesh.setColorAt(slot, tint);
      }
      this.neonMesh.instanceColor.needsUpdate = true;
    }

    draw(now) {
      if (!this.renderer) return;
      // The first rAF timestamp can predate `started`, and negative time walks the cycle
      // tables backwards, so clamp before anything indexes off it.
      const t = Math.max(0, (now - this.started) / 1000);
      const reduced = this.reduced.matches;
      const mood = MOODS[this.getAttribute('expression')] || MOODS.neutral;

      const cycle = 4.4, left = cycle - (t % cycle);
      const blink = (!reduced && left < .26) ? (left > .2 || left < .06 ? 1 : 2) : 0;
      this.setFeature(this.lids, Math.max(mood.lid, blink));

      // The pixels snap between visemes the way the artwork would, but the jaw underneath
      // eases, so speech reads as one continuous movement instead of a flipbook.
      const mouth = this.mouthFrame(t, mood, reduced);
      this.setFeature(this.lips, mouth.shape);
      this.jawOpen += (mouth.open - this.jawOpen) * (mouth.open > this.jawOpen ? .42 : .26);
      this.applyJaw(reduced ? mouth.open * 1.5 : this.jawOpen * 1.5);

      // The lens bleeds to red while she thinks and drifts back to cyan once she is done;
      // the traces harden to cyan while she listens. Both ease, so the change is never abrupt.
      const settle = (value, target) => {
        const next = value + (target - value) * .055;
        return next < .004 ? 0 : next;
      };
      this.alert = settle(this.alert, mood.alert ? 1 : 0);
      this.absorb = settle(this.absorb, mood.absorb ? 1 : 0);
      this.paintNeon(t, mood, this.alert, this.absorb);
      this.paintGlow(this.alert, this.absorb);
      this.halo.material.opacity = (.11 + .05 * Math.sin(t * (2.4 + this.alert * 2.2))) * mood.glow;
      this.halo.scale.setScalar(ART.implant.r * (2.5 + .18 * Math.sin(t * 1.7)));

      const sway = reduced ? 0 : mood.sway;
      // Open vowels tip the head a touch, the way a real speaker punches a syllable.
      const stress = this.jawOpen * .035;
      const yaw = Math.sin(t * .42) * .055 * sway + this.pointer.x * -.16;
      const pitch = Math.sin(t * .31) * .025 * sway + this.pointer.y * .1 + stress;
      this.stage.rotation.y += (yaw - this.stage.rotation.y) * .07;
      this.stage.rotation.x += (pitch - this.stage.rotation.x) * .07;
      this.stage.position.y = reduced ? 0 : Math.sin(t * .65) * .5;

      this.renderer.render(this.scene, this.camera);
      this.frame = requestAnimationFrame(next => this.draw(next));
    }

    dispose() {
      if (this.frame) cancelAnimationFrame(this.frame);
      this.resizeObserver?.disconnect();
      this.removeEventListener('pointermove', this.onPointer);
      this.removeEventListener('pointerleave', this.onLeave);
      if (this.speech) window.speechSynthesis?.cancel();
      this.speech = null;
      this.detachAudio();
      this.audioContext?.close();
      this.audioContext = null;
      if (this.scene) {
        const geometries = new Set(), materials = new Set(), textures = new Set();
        this.scene.traverse(object => {
          if (object.geometry) geometries.add(object.geometry);
          for (const material of [].concat(object.material || [])) {
            materials.add(material);
            if (material.map) textures.add(material.map);
          }
        });
        geometries.forEach(g => g.dispose());
        materials.forEach(m => m.dispose());
        textures.forEach(tx => tx.dispose());
      }
      this.renderer?.dispose();
      this.renderer?.domElement.remove();
      this.renderer = null;
      this.scene = null;
    }
  }

  customElements.define('lea-avatar', LeaAvatar);
})();
