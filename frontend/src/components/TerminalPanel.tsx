import { useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';

const DEFAULT_WORKDIR = '/srv/project/livekit-collab';

interface TerminalPanelProps {
  roomName: string;
}

export default function TerminalPanel({ roomName }: TerminalPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);

  const [isConnected, setIsConnected] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current || terminalRef.current) return;

    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"SFMono-Regular", "JetBrains Mono", "Fira Code", monospace',
      convertEol: true,
      theme: {
        background: '#060a14',
        foreground: '#d7ecff',
        cursor: '#22d3ee',
        cursorAccent: '#060a14',
        selectionBackground: 'rgba(34, 211, 238, 0.25)',
        black: '#0b1324',
        red: '#ff4466',
        green: '#39ffb6',
        yellow: '#ffb432',
        blue: '#60a5fa',
        magenta: '#c084fc',
        cyan: '#22d3ee',
        white: '#f8fbff',
        brightBlack: '#64748b',
        brightRed: '#ff7590',
        brightGreen: '#7dffcf',
        brightYellow: '#ffd27a',
        brightBlue: '#93c5fd',
        brightMagenta: '#d8b4fe',
        brightCyan: '#67e8f9',
        brightWhite: '#ffffff',
      },
    });

    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.loadAddon(new WebLinksAddon());
    terminal.open(containerRef.current);
    fitAddon.fit();

    terminal.writeln('\x1b[1;36mSkynet Terminal\x1b[0m');
    terminal.writeln(`\x1b[90mroom: ${roomName}\x1b[0m`);
    terminal.writeln('');

    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/api/terminal/ws`);
    wsRef.current = ws;

    const createSession = () => {
      ws.send(JSON.stringify({
        type: 'create_session',
        payload: {
          name: `Skynet Terminal (${roomName})`,
          workingDir: DEFAULT_WORKDIR,
          cols: terminal.cols,
          rows: terminal.rows,
        },
      }));
    };

    ws.addEventListener('open', () => {
      setIsConnected(true);
      terminal.writeln('\x1b[32mConnected to terminal service\x1b[0m');
      createSession();
    });

    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      const payload = message?.payload || {};

      switch (message?.type) {
        case 'session_created':
          sessionIdRef.current = payload.sessionId;
          setSessionId(payload.sessionId);
          terminal.writeln(`\x1b[90msession ${payload.sessionId.slice(0, 8)} ready\x1b[0m`);
          terminal.writeln('');
          fitAddon.fit();
          break;
        case 'data':
          terminal.write(payload.data || '');
          break;
        case 'exit':
          terminal.writeln('');
          terminal.writeln('\x1b[33mTerminal session exited\x1b[0m');
          sessionIdRef.current = null;
          setSessionId(null);
          break;
        case 'error':
          terminal.writeln(`\x1b[31m${payload.error || 'Terminal error'}\x1b[0m`);
          break;
        default:
          break;
      }
    });

    ws.addEventListener('close', () => {
      setIsConnected(false);
      terminal.writeln('\x1b[33mTerminal disconnected\x1b[0m');
    });

    ws.addEventListener('error', () => {
      terminal.writeln('\x1b[31mFailed to connect to terminal service\x1b[0m');
    });

    const onResize = () => {
      fitAddon.fit();
      if (ws.readyState === WebSocket.OPEN && sessionIdRef.current) {
        ws.send(JSON.stringify({
          type: 'resize',
          payload: {
            sessionId: sessionIdRef.current,
            cols: terminal.cols,
            rows: terminal.rows,
          },
        }));
      }
    };

    const resizeObserver = new ResizeObserver(() => {
      onResize();
    });
    resizeObserver.observe(containerRef.current);
    resizeObserverRef.current = resizeObserver;

    const dataDisposable = terminal.onData((data) => {
      if (ws.readyState === WebSocket.OPEN && sessionIdRef.current) {
        ws.send(JSON.stringify({
          type: 'input',
          payload: {
            sessionId: sessionIdRef.current,
            input: { text: data },
          },
        }));
      }
    });

    window.addEventListener('resize', onResize);

    return () => {
      dataDisposable.dispose();
      window.removeEventListener('resize', onResize);
      resizeObserverRef.current?.disconnect();
      resizeObserverRef.current = null;
      if (ws.readyState === WebSocket.OPEN && sessionIdRef.current) {
        ws.send(JSON.stringify({
          type: 'kill_session',
          payload: { sessionId: sessionIdRef.current },
        }));
      }
      ws.close();
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
      sessionIdRef.current = null;
    };
  }, [roomName]);

  const handleClear = () => {
    terminalRef.current?.clear();
    terminalRef.current?.writeln('\x1b[1;36mSkynet Terminal\x1b[0m');
    terminalRef.current?.writeln(`\x1b[90mroom: ${roomName}\x1b[0m`);
    terminalRef.current?.writeln('');
  };

  const handleNewSession = () => {
    if (!wsRef.current || !terminalRef.current) return;
    if (wsRef.current.readyState !== WebSocket.OPEN) return;

    if (sessionIdRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'kill_session',
        payload: { sessionId: sessionIdRef.current },
      }));
    }

    wsRef.current.send(JSON.stringify({
      type: 'create_session',
      payload: {
        name: `Skynet Terminal (${roomName})`,
        workingDir: DEFAULT_WORKDIR,
        cols: terminalRef.current.cols,
        rows: terminalRef.current.rows,
      },
    }));
  };

  return (
    <div className="vr-terminal-panel">
      <div className="vr-terminal-toolbar">
        <div className="vr-terminal-status">
          <span className={`vr-terminal-dot ${isConnected ? 'connected' : 'disconnected'}`} />
          <span>{isConnected ? 'terminal connected' : 'terminal offline'}</span>
          {sessionId && <span className="vr-terminal-session">{sessionId.slice(0, 8)}</span>}
        </div>
        <div className="vr-terminal-actions">
          <button className="vr-sidebar-action-btn" onClick={handleClear}>Clear</button>
          <button className="vr-sidebar-action-btn primary" onClick={handleNewSession}>New Session</button>
        </div>
      </div>
      <div ref={containerRef} className="vr-terminal-shell" />
    </div>
  );
}
