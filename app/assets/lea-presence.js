/* LEA's presence: Codex drives what she does, and what she is doing drives her face.

   Two halves, both optional on their own:
   - the bridge posts what you type to /api/agent/runs with the codex backend and follows the
     session over the event websocket;
   - the state machine maps whatever is happening to one of her four expressions, so nobody
     has to pick one by hand.

   Configure the bridge with window.LEA_API = {base, token, projectId}. Without it the page
   stays a mock and only the local signals (typing, the speech test) move her face. */
(() => {
  if (window.LeaPresence) return;

  const CHAT_INPUT = 'input[placeholder*="fale com a lea" i]';
  // How long each state holds before she falls back to neutral.
  const LINGER = {listening: 2600, thinking: 0, speaking: 0};

  // Served by the daemon itself, so the API is the same origin and needs no CORS. Only the
  // token has to arrive: ?token=... stores it and is then stripped from the URL, so it does
  // not linger in history or in a screenshot.
  const bootstrap = () => {
    const params = new URLSearchParams(location.search);
    if (!params.get('token')) return;
    const saved = {token: params.get('token'), projectId: Number(params.get('project')) || 1};
    try { localStorage.setItem('lea.api', JSON.stringify(saved)); } catch { /* private mode */ }
    params.delete('token');
    params.delete('project');
    const rest = params.toString();
    history.replaceState(null, '', location.pathname + (rest ? '?' + rest : ''));
  };

  const config = () => {
    const given = window.LEA_API || {};
    let stored = {};
    try { stored = JSON.parse(localStorage.getItem('lea.api') || '{}'); } catch { /* private mode */ }
    const merged = {base: location.origin, ...stored, ...given};
    return merged.base && merged.token ? merged : null;
  };

  const presence = {
    state: 'neutral',
    auto: true,
    since: 0,
    avatar: null,
    socket: null,
    session: null,

    /** Point at the avatar it should drive. */
    attach(avatar) {
      this.avatar = avatar;
      this.apply('neutral');
      return this;
    },

    /** The runtime re-renders and swaps the element, so never trust a cached node. */
    node() {
      if (!this.avatar || !this.avatar.isConnected) this.avatar = document.querySelector('lea-avatar');
      return this.avatar;
    },

    /** Hand control back and forth between the automatic machine and a manual pick. */
    setAuto(on, expression) {
      this.auto = !!on;
      if (this.auto) this.apply(this.state);
      else if (expression) this.node()?.setAttribute('expression', expression);
    },

    apply(state) {
      this.state = state;
      this.since = Date.now();
      if (this.auto) this.node()?.setAttribute('expression', state);
    },

    /** The one entry point: say what just happened, not which face to make. */
    signal(what, detail = {}) {
      switch (what) {
        case 'typing':                                   // you are still composing
        case 'inbound':                                  // something arrived for her to take in
          if (this.state !== 'speaking') this.apply('listening');
          break;
        case 'working':                                  // a run started, a tool fired, approval pending
          this.apply('thinking');
          break;
        case 'reply':
          this.apply('speaking');
          const avatar = this.node();
          if (detail.text && avatar?.speak) {
            avatar.speak(detail.text).catch(() => {
              // No voice installed: hold the speaking face for about as long as reading it takes.
              setTimeout(() => this.state === 'speaking' && this.apply('neutral'),
                         Math.min(9000, 1200 + detail.text.length * 45));
            });
          }
          break;
        case 'idle':
          this.apply('neutral');
          break;
      }
    },

    /** Agent events share one vocabulary; this is the whole mapping. */
    onAgentEvent(event) {
      const type = event?.type;
      if (!type) return;
      if (type === 'session_start') { this.session = event.session_id; this.signal('working'); }
      else if (type === 'tool_call' || type === 'tool_result' || type === 'alert') this.signal('working');
      else if (type === 'message') this.signal('reply', {text: event.payload?.text || ''});
      else if (type === 'session_end') this.signal('idle');
    },

    /** Send a task to the real agent. Resolves to the session, or null when unconfigured. */
    async ask(task) {
      const api = config();
      if (!api || !task.trim()) return null;
      this.signal('working');
      const response = await fetch(`${api.base.replace(/\/$/, '')}/api/agent/runs`, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'x-lea-token': api.token},
        body: JSON.stringify({project_id: api.projectId ?? 1, task, backend: 'codex'}),
      });
      if (!response.ok) {
        this.signal('idle');
        throw new Error(`agente respondeu ${response.status}`);
      }
      return response.json();
    },

    /** Follow the global event bus so her face tracks the agent even for runs she did not start. */
    connect() {
      const api = config();
      if (!api || this.socket) return null;
      const url = new URL(`${api.base.replace(/\/$/, '')}/ws`);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      url.searchParams.set('token', api.token);
      const socket = this.socket = new WebSocket(url);
      socket.addEventListener('message', frame => {
        try { this.onAgentEvent(JSON.parse(frame.data)); } catch { /* not ours */ }
      });
      socket.addEventListener('close', () => {
        this.socket = null;
        setTimeout(() => this.connect(), 4000);          // the server restarts; just come back
      });
      socket.addEventListener('error', () => socket.close());
      return socket;
    },
  };

  // Local signals, delegated on the document so they survive the runtime re-rendering.
  let quiet = 0;
  document.addEventListener('input', event => {
    if (!event.target.matches?.(CHAT_INPUT)) return;
    presence.signal('typing');
    clearTimeout(quiet);
    quiet = setTimeout(() => presence.state === 'listening' && presence.signal('idle'), LINGER.listening);
  }, true);

  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || !event.target.matches?.(CHAT_INPUT)) return;
    const task = event.target.value.trim();
    if (!task) return;
    clearTimeout(quiet);
    presence.ask(task).catch(error => console.warn('LEA agent:', error.message));
    // With no backend configured the mock answers locally, so give her a beat of thought.
    if (!config()) {
      presence.signal('working');
      setTimeout(() => presence.state === 'thinking' && presence.signal('idle'), 1800);
    }
  }, true);

  window.LeaPresence = presence;
  const start = () => {
    bootstrap();
    const avatar = document.querySelector('lea-avatar');
    if (avatar) presence.attach(avatar);
    presence.connect();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
