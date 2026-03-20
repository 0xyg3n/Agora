import { EventEmitter } from 'events';
import { randomUUID } from 'crypto';
import type { IPty } from 'node-pty';

let ptyModule: typeof import('node-pty') | null = null;

export interface TerminalSessionInfo {
  id: string;
  name: string;
  pid: number;
  cwd: string;
  cols: number;
  rows: number;
  createdAt: number;
  lastActivity: number;
}

interface TerminalSession extends TerminalSessionInfo {
  pty: IPty;
}

export interface TerminalSessionOptions {
  name?: string;
  workingDir?: string;
  cols?: number;
  rows?: number;
  shell?: string;
  shellArgs?: string[];
}

export class PtyManager extends EventEmitter {
  private sessions = new Map<string, TerminalSession>();
  private static initialized = false;

  public static async initialize(): Promise<void> {
    if (PtyManager.initialized) return;
    ptyModule = await import('node-pty');
    PtyManager.initialized = true;
  }

  public constructor() {
    super();
    if (!PtyManager.initialized || !ptyModule) {
      throw new Error('PtyManager not initialized');
    }
  }

  public async createSession(options: TerminalSessionOptions = {}): Promise<{
    sessionId: string;
    sessionInfo: TerminalSessionInfo;
  }> {
    if (!ptyModule) {
      throw new Error('node-pty is unavailable');
    }

    const sessionId = randomUUID();
    const {
      name = 'Skynet Terminal',
      workingDir = process.env.TERMINAL_WORKDIR || process.cwd(),
      cols = 100,
      rows = 28,
      shell = process.env.SHELL || '/bin/bash',
      shellArgs = ['-l'],
    } = options;

    const pty = ptyModule.spawn(shell, shellArgs, {
      name: 'xterm-256color',
      cols,
      rows,
      cwd: workingDir,
      env: {
        ...process.env,
        TERM: 'xterm-256color',
      },
    });

    const session: TerminalSession = {
      id: sessionId,
      name,
      pid: pty.pid,
      cwd: workingDir,
      cols,
      rows,
      createdAt: Date.now(),
      lastActivity: Date.now(),
      pty,
    };

    pty.onData((data) => {
      session.lastActivity = Date.now();
      this.emit(`data:${sessionId}`, data);
    });

    pty.onExit(({ exitCode, signal }) => {
      this.emit(`exit:${sessionId}`, { exitCode, signal });
      this.sessions.delete(sessionId);
    });

    this.sessions.set(sessionId, session);

    return {
      sessionId,
      sessionInfo: {
        id: session.id,
        name: session.name,
        pid: session.pid,
        cwd: session.cwd,
        cols: session.cols,
        rows: session.rows,
        createdAt: session.createdAt,
        lastActivity: session.lastActivity,
      },
    };
  }

  public hasSession(sessionId: string): boolean {
    return this.sessions.has(sessionId);
  }

  public getSessions(): TerminalSessionInfo[] {
    return Array.from(this.sessions.values()).map((session) => ({
      id: session.id,
      name: session.name,
      pid: session.pid,
      cwd: session.cwd,
      cols: session.cols,
      rows: session.rows,
      createdAt: session.createdAt,
      lastActivity: session.lastActivity,
    }));
  }

  public sendInput(sessionId: string, text: string): void {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new Error(`Unknown session: ${sessionId}`);
    }
    session.pty.write(text);
    session.lastActivity = Date.now();
  }

  public resizeSession(sessionId: string, cols: number, rows: number): void {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new Error(`Unknown session: ${sessionId}`);
    }
    session.pty.resize(cols, rows);
    session.cols = cols;
    session.rows = rows;
    session.lastActivity = Date.now();
  }

  public async killSession(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) return;
    session.pty.kill();
    this.sessions.delete(sessionId);
  }

  public async cleanup(): Promise<void> {
    await Promise.all(
      Array.from(this.sessions.keys()).map((sessionId) => this.killSession(sessionId))
    );
  }
}
