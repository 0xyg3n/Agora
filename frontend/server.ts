import crypto from 'crypto';
import { spawn } from 'child_process';
import { createServer } from 'http';
import express from 'express';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import { AccessToken, AgentDispatchClient, RoomServiceClient } from 'livekit-server-sdk';
import path from 'path';
import { fileURLToPath } from 'url';
import { WebSocketServer, WebSocket } from 'ws';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORT = parseInt(process.env.TOKEN_PORT || '3210');
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_HTTP_URL = process.env.LIVEKIT_HTTP_URL || 'http://127.0.0.1:7880';
const ADMIN_SECRET = process.env.ADMIN_API_SECRET || '';
const AGENT_NAMES = (process.env.AGENT_NAMES || 'Laira,Loki').split(',').map((s) => s.trim());
const AUTO_DISPATCH_ON_JOIN = process.env.AUTO_DISPATCH_ON_JOIN === 'true';
const EVENT_FORWARD_URL = (process.env.EVENT_FORWARD_URL || '').trim();
const MAX_OBSERVABILITY_EVENTS = Math.max(
  50,
  parseInt(process.env.MAX_OBSERVABILITY_EVENTS || '300', 10) || 300,
);

if (!API_KEY || !API_SECRET) {
  console.error('FATAL: LIVEKIT_API_KEY and LIVEKIT_API_SECRET environment variables are required');
  process.exit(1);
}

const roomService = new RoomServiceClient(LIVEKIT_HTTP_URL, API_KEY, API_SECRET);
const agentClient = new AgentDispatchClient(LIVEKIT_HTTP_URL, API_KEY, API_SECRET);

interface AgentSnapshot {
  name: string;
  identity: string;
  connected: boolean;
  agentState: string;
  agentActivity: string;
  statusText: string;
  errorText: string;
  lastActivityAt: number | null;
  updatedAt: number;
}

interface ObservabilityEvent {
  id: string;
  source_app: string;
  session_id: string;
  hook_event_type: string;
  payload: Record<string, unknown>;
  timestamp: number;
  model_name?: string;
}

interface ThermalEvent {
  id: string;
  agentName: string;
  room: string;
  kind: 'user_input' | 'thinking' | 'tool_call' | 'tool_result' | 'response' | 'error' | 'model';
  timestamp: number;
  summary: string;
  detail?: string;
  modelName?: string;
}

const roomAgentSnapshots = new Map<string, Map<string, AgentSnapshot>>();
const observabilityEvents: ObservabilityEvent[] = [];
const observabilityClients = new Map<WebSocket, string | null>();

function cleanSnapshotText(value: unknown, maxLength: number): string {
  if (typeof value !== 'string') return '';
  return value.replace(/\s+/g, ' ').trim().slice(0, maxLength);
}

function parseSnapshotTime(value: unknown): number | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function getDefaultAgentSnapshot(name: string): AgentSnapshot {
  return {
    name,
    identity: '',
    connected: false,
    agentState: 'idle',
    agentActivity: 'disconnected',
    statusText: 'Offline',
    errorText: '',
    lastActivityAt: null,
    updatedAt: Date.now(),
  };
}

function getAgentContainerName(name: string): string {
  return `skynet-${name.toLowerCase()}`;
}

function cacheAgentSnapshot(room: string, snapshot: AgentSnapshot): void {
  let roomSnapshots = roomAgentSnapshots.get(room);
  if (!roomSnapshots) {
    roomSnapshots = new Map<string, AgentSnapshot>();
    roomAgentSnapshots.set(room, roomSnapshots);
  }
  roomSnapshots.set(snapshot.name, snapshot);
}

function buildAgentSnapshot(participant: {
  name?: string;
  identity: string;
  attributes?: Record<string, string>;
}): AgentSnapshot {
  const attributes = participant.attributes || {};
  const name = participant.name || participant.identity;
  return {
    name,
    identity: participant.identity,
    connected: true,
    agentState: cleanSnapshotText(attributes.agent_state, 32) || 'idle',
    agentActivity: cleanSnapshotText(attributes.agent_activity, 32) || 'idle',
    statusText: cleanSnapshotText(attributes.agent_status_text, 96),
    errorText: cleanSnapshotText(attributes.agent_error_text, 120),
    lastActivityAt: parseSnapshotTime(attributes.agent_last_activity_at),
    updatedAt: Date.now(),
  };
}

function getRoomAgentSnapshots(room: string): AgentSnapshot[] {
  const roomSnapshots = roomAgentSnapshots.get(room);
  return AGENT_NAMES.map((name) => roomSnapshots?.get(name) || getDefaultAgentSnapshot(name));
}

function normalizeRoomFromSessionId(sessionId: string): string | null {
  const trimmed = sessionId.trim();
  if (trimmed.startsWith('livekit-') && trimmed.length > 8) {
    return trimmed.slice(8);
  }
  return null;
}

function getEventRoom(event: ObservabilityEvent): string | null {
  const payloadRoom = typeof event.payload.room === 'string' ? event.payload.room : null;
  return payloadRoom || normalizeRoomFromSessionId(event.session_id);
}

function sanitizeEventPayload(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return {};
  }
  const entries = Object.entries(payload as Record<string, unknown>).slice(0, 40);
  return Object.fromEntries(
    entries.map(([key, value]) => {
      if (typeof value === 'string') {
        return [key, value.replace(/\s+/g, ' ').trim().slice(0, 400)];
      }
      if (typeof value === 'number' || typeof value === 'boolean' || value === null) {
        return [key, value];
      }
      if (Array.isArray(value)) {
        return [key, value.slice(0, 20)];
      }
      if (typeof value === 'object') {
        return [key, JSON.parse(JSON.stringify(value))];
      }
      return [key, String(value)];
    }),
  );
}

function buildObservabilityEvent(raw: Partial<ObservabilityEvent>): ObservabilityEvent | null {
  if (
    typeof raw.source_app !== 'string'
    || typeof raw.session_id !== 'string'
    || typeof raw.hook_event_type !== 'string'
  ) {
    return null;
  }

  return {
    id: typeof raw.id === 'string' ? raw.id : crypto.randomUUID(),
    source_app: cleanSnapshotText(raw.source_app, 80),
    session_id: cleanSnapshotText(raw.session_id, 160),
    hook_event_type: cleanSnapshotText(raw.hook_event_type, 80),
    payload: sanitizeEventPayload(raw.payload),
    timestamp: typeof raw.timestamp === 'number' && Number.isFinite(raw.timestamp)
      ? raw.timestamp
      : Date.now(),
    model_name: typeof raw.model_name === 'string'
      ? cleanSnapshotText(raw.model_name, 80)
      : undefined,
  };
}

function getRecentObservabilityEvents(room: string | null, limit = 80): ObservabilityEvent[] {
  const filtered = room
    ? observabilityEvents.filter((event) => getEventRoom(event) === room)
    : observabilityEvents;
  return filtered.slice(-Math.max(1, Math.min(limit, MAX_OBSERVABILITY_EVENTS)));
}

function parseIsoTimestamp(value: unknown): number | null {
  if (typeof value !== 'string' || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function trimThermalText(value: unknown, maxLength = 260): string {
  if (typeof value !== 'string') return '';
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 3).trimEnd()}...`;
}

function buildThermalId(
  agentName: string,
  entryId: string | undefined,
  blockIndex: number,
  kind: ThermalEvent['kind'],
): string {
  return `${agentName}:${entryId || 'entry'}:${blockIndex}:${kind}`;
}

function parseThermalLine(line: string, agentName: string, room: string): ThermalEvent[] {
  if (!line.trim()) return [];

  let parsed: Record<string, any>;
  try {
    parsed = JSON.parse(line) as Record<string, any>;
  } catch {
    return [];
  }

  const timestamp = parseIsoTimestamp(parsed.timestamp) ?? Date.now();
  const entryId = typeof parsed.id === 'string' ? parsed.id : crypto.randomUUID();
  const events: ThermalEvent[] = [];

  if (parsed.type === 'model_change') {
    const provider = trimThermalText(parsed.provider, 40);
    const modelId = trimThermalText(parsed.modelId, 60);
    events.push({
      id: buildThermalId(agentName, entryId, 0, 'model'),
      agentName,
      room,
      kind: 'model',
      timestamp,
      summary: [provider, modelId].filter(Boolean).join(' · ') || 'Model updated',
      modelName: modelId || undefined,
    });
    return events;
  }

  if (parsed.type === 'custom' && parsed.customType === 'openclaw:prompt-error') {
    const error = trimThermalText(parsed.data?.error || 'Prompt failed', 220);
    events.push({
      id: buildThermalId(agentName, entryId, 0, 'error'),
      agentName,
      room,
      kind: 'error',
      timestamp,
      summary: error || 'Prompt failed',
      detail: trimThermalText(JSON.stringify(parsed.data ?? {}), 320),
    });
    return events;
  }

  if (parsed.type !== 'message' || !parsed.message || typeof parsed.message !== 'object') {
    return events;
  }

  const message = parsed.message as Record<string, any>;
  const role = typeof message.role === 'string' ? message.role : '';
  const content = Array.isArray(message.content) ? message.content : [];

  if (role === 'user') {
    content.forEach((block, index) => {
      if (!block || typeof block !== 'object' || block.type !== 'text') return;
      const text = trimThermalText(block.text, 260);
      if (!text) return;
      events.push({
        id: buildThermalId(agentName, entryId, index, 'user_input'),
        agentName,
        room,
        kind: 'user_input',
        timestamp,
        summary: text,
      });
    });
    return events;
  }

  if (role === 'assistant') {
    if (message.stopReason === 'error' || message.errorMessage) {
      const error = trimThermalText(message.errorMessage || 'Assistant error', 220);
      events.push({
        id: buildThermalId(agentName, entryId, 0, 'error'),
        agentName,
        room,
        kind: 'error',
        timestamp,
        summary: error || 'Assistant error',
        detail: trimThermalText(message.model, 80),
        modelName: trimThermalText(message.model, 80) || undefined,
      });
      return events;
    }

    content.forEach((block, index) => {
      if (!block || typeof block !== 'object') return;
      if (block.type === 'thinking') {
        const thinking = trimThermalText(block.thinking || block.text, 320);
        if (!thinking) return;
        events.push({
          id: buildThermalId(agentName, entryId, index, 'thinking'),
          agentName,
          room,
          kind: 'thinking',
          timestamp,
          summary: thinking,
          modelName: trimThermalText(message.model, 80) || undefined,
        });
        return;
      }

      if (block.type === 'toolCall') {
        const name = trimThermalText(block.name, 80) || 'tool';
        const argsPreview = trimThermalText(JSON.stringify(block.arguments ?? {}), 240);
        events.push({
          id: buildThermalId(agentName, entryId, index, 'tool_call'),
          agentName,
          room,
          kind: 'tool_call',
          timestamp,
          summary: name,
          detail: argsPreview,
          modelName: trimThermalText(message.model, 80) || undefined,
        });
        return;
      }

      if (block.type === 'text') {
        const text = trimThermalText(block.text, 260);
        if (!text) return;
        events.push({
          id: buildThermalId(agentName, entryId, index, 'response'),
          agentName,
          room,
          kind: 'response',
          timestamp,
          summary: text,
          modelName: trimThermalText(message.model, 80) || undefined,
        });
      }
    });
    return events;
  }

  if (role === 'toolResult') {
    content.forEach((block, index) => {
      if (!block || typeof block !== 'object' || block.type !== 'text') return;
      const text = trimThermalText(block.text, 260);
      if (!text) return;
      const toolName = trimThermalText(message.toolName, 80);
      events.push({
        id: buildThermalId(agentName, entryId, index, 'tool_result'),
        agentName,
        room,
        kind: 'tool_result',
        timestamp,
        summary: toolName || 'tool result',
        detail: text,
      });
    });
  }

  return events;
}

async function execProcess(command: string, args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
  return await new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });

    child.on('error', reject);
    child.on('close', (code) => {
      resolve({
        code: typeof code === 'number' ? code : 1,
        stdout,
        stderr,
      });
    });
  });
}

async function readAgentThermalEvents(agentName: string, room: string): Promise<ThermalEvent[]> {
  const sessionFile = `/home/node/.openclaw/agents/main/sessions/livekit-${room}.jsonl`;
  const result = await execProcess('docker', [
    'exec',
    '--user',
    'node',
    getAgentContainerName(agentName),
    'bash',
    '-lc',
    `tail -n 160 "${sessionFile}" 2>/dev/null || true`,
  ]);

  if (result.code !== 0 || !result.stdout.trim()) {
    return [];
  }

  return result.stdout
    .split('\n')
    .flatMap((line) => parseThermalLine(line, agentName, room));
}

async function getRecentThermalEvents(room: string, limit = 80): Promise<ThermalEvent[]> {
  const perAgentEvents = await Promise.all(
    AGENT_NAMES.map((agentName) => readAgentThermalEvents(agentName, room)),
  );

  const deduped = new Map<string, ThermalEvent>();
  for (const event of perAgentEvents.flat()) {
    deduped.set(event.id, event);
  }

  return [...deduped.values()]
    .sort((a, b) => b.timestamp - a.timestamp)
    .slice(0, Math.max(1, Math.min(limit, 200)));
}

function sendJson(ws: WebSocket, payload: unknown): void {
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(payload));
}

async function forwardObservabilityEvent(event: ObservabilityEvent): Promise<void> {
  if (!EVENT_FORWARD_URL) return;
  try {
    await fetch(EVENT_FORWARD_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
    });
  } catch (error) {
    console.warn('[Observability] Forward failed:', error);
  }
}

function broadcastObservabilityEvent(event: ObservabilityEvent): void {
  for (const [ws, roomFilter] of observabilityClients.entries()) {
    if (roomFilter && getEventRoom(event) !== roomFilter) continue;
    sendJson(ws, { type: 'event', data: event });
  }
}

function storeObservabilityEvent(event: ObservabilityEvent): ObservabilityEvent {
  observabilityEvents.push(event);
  if (observabilityEvents.length > MAX_OBSERVABILITY_EVENTS) {
    observabilityEvents.splice(0, observabilityEvents.length - MAX_OBSERVABILITY_EVENTS);
  }
  broadcastObservabilityEvent(event);
  void forwardObservabilityEvent(event);
  return event;
}

const ROOM_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;
const IDENTITY_PATTERN = /^[a-zA-Z0-9_.@-]{1,64}$/;
const NAME_PATTERN = /^[a-zA-Z0-9 _.@-]{1,64}$/;

function isValidRoom(val: unknown): val is string {
  return typeof val === 'string' && ROOM_PATTERN.test(val);
}

function isValidIdentity(val: unknown): val is string {
  return typeof val === 'string' && IDENTITY_PATTERN.test(val);
}

function isValidName(val: unknown): val is string {
  return typeof val === 'string' && NAME_PATTERN.test(val);
}

function isValidAgentName(val: unknown): val is string {
  return typeof val === 'string' && AGENT_NAMES.includes(val);
}

const app = express();

app.use(helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", "'unsafe-inline'"],
      connectSrc: ["'self'", 'ws:', 'wss:', 'http:', 'https:'],
      styleSrc: ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com'],
      fontSrc: ["'self'", 'https://fonts.gstatic.com'],
      imgSrc: ["'self'", 'data:', 'blob:'],
      workerSrc: ["'self'", 'blob:'],
    },
  },
  crossOriginResourcePolicy: { policy: 'cross-origin' },
  crossOriginEmbedderPolicy: false,
}));

app.use(express.json({ limit: '32kb' }));

const tokenLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 10,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many token requests, try again later' },
});

const agentLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 60,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests, try again later' },
});

const observabilityLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 600,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many observability requests, try again later' },
});

function requireAdmin(req: express.Request, res: express.Response, next: express.NextFunction) {
  if (!ADMIN_SECRET) {
    next();
    return;
  }
  const provided = (req.headers['x-api-key'] || '') as string;
  if (
    !provided
    || provided.length !== ADMIN_SECRET.length
    || !crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(ADMIN_SECRET))
  ) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }
  next();
}

app.use(express.static(path.join(__dirname, 'dist')));

async function getPresentAgentNames(room: string): Promise<Set<string>> {
  try {
    const participants = await roomService.listParticipants(room);
    return new Set(
      participants
        .filter((participant) => participant.kind === 4)
        .map((participant) => participant.name || participant.identity),
    );
  } catch {
    return new Set<string>();
  }
}

async function dispatchMissingAgents(
  room: string,
  requestedAgents: string[] = AGENT_NAMES,
): Promise<{ dispatched: string[]; skipped: string[]; failed: string[] }> {
  const presentAgents = await getPresentAgentNames(room);
  const dispatched: string[] = [];
  const skipped: string[] = [];
  const failed: string[] = [];

  for (const agentName of requestedAgents) {
    if (presentAgents.has(agentName)) {
      skipped.push(agentName);
      continue;
    }
    try {
      await agentClient.createDispatch(room, agentName);
      dispatched.push(agentName);
    } catch (error) {
      console.error(`Dispatch failed for '${agentName}' in '${room}':`, error);
      failed.push(agentName);
    }
  }

  return { dispatched, skipped, failed };
}

app.get('/api/agents', (_req, res) => {
  res.json({ agents: AGENT_NAMES, autoDispatchOnJoin: AUTO_DISPATCH_ON_JOIN });
});

app.post('/api/token', tokenLimiter, async (req, res) => {
  const { room, identity, name } = req.body;

  if (!isValidRoom(room)) {
    res.status(400).json({ error: 'Invalid room name (alphanumeric, dashes, underscores, max 64 chars)' });
    return;
  }
  if (!isValidIdentity(identity)) {
    res.status(400).json({ error: 'Invalid identity' });
    return;
  }
  if (name !== undefined && !isValidName(name)) {
    res.status(400).json({ error: 'Invalid name' });
    return;
  }

  const token = new AccessToken(API_KEY, API_SECRET, {
    identity,
    name: name || identity,
    ttl: '2h',
  });

  token.addGrant({
    room,
    roomJoin: true,
    canPublish: true,
    canSubscribe: true,
    canPublishData: true,
  });

  const jwt = await token.toJwt();

  if (AUTO_DISPATCH_ON_JOIN) {
    await dispatchMissingAgents(room);
  }

  res.json({ token: jwt });
});

app.post('/api/agent/status', agentLimiter, async (req, res) => {
  const { room } = req.body;
  if (!isValidRoom(room)) {
    res.status(400).json({ error: 'Invalid room name' });
    return;
  }

  const present = new Set<string>();
  try {
    const participants = await roomService.listParticipants(room);
    for (const participant of participants) {
      if (participant.kind !== 4) continue;
      const snapshot = buildAgentSnapshot({
        name: participant.name,
        identity: participant.identity,
        attributes: participant.attributes,
      });
      present.add(snapshot.name);
      cacheAgentSnapshot(room, snapshot);
    }
  } catch {
    // room may not exist yet
  }

  const missing = AGENT_NAMES.filter((name) => !present.has(name));
  const snapshots = getRoomAgentSnapshots(room).map((snapshot) => (
    present.has(snapshot.name)
      ? snapshot
      : {
          ...snapshot,
          connected: false,
          agentActivity: 'disconnected',
        }
  ));

  res.json({
    room,
    agents: AGENT_NAMES,
    present: Array.from(present),
    missing,
    snapshots,
  });
});

app.post('/api/agent/dispatch-all', requireAdmin, agentLimiter, async (req, res) => {
  const { room } = req.body;
  if (!isValidRoom(room)) {
    res.status(400).json({ error: 'Invalid room name' });
    return;
  }
  const result = await dispatchMissingAgents(room);
  res.json({ ok: true, ...result });
});

app.post('/api/agent/restart', requireAdmin, agentLimiter, async (req, res) => {
  const { room, agentIdentity, agentName } = req.body;
  if (!isValidRoom(room) || !isValidIdentity(agentIdentity) || !isValidAgentName(agentName)) {
    res.status(400).json({ error: 'Invalid parameters' });
    return;
  }
  try {
    await roomService.removeParticipant(room, agentIdentity);
  } catch (error) {
    console.error(`Kick failed for '${agentIdentity}':`, error);
  }
  await new Promise((resolve) => setTimeout(resolve, 1000));
  try {
    await agentClient.createDispatch(room, agentName);
    res.json({ ok: true });
  } catch (error) {
    console.error(`Re-dispatch failed for '${agentName}':`, error);
    res.status(500).json({ error: 'Agent restart failed' });
  }
});

app.post('/api/agent/dispatch', requireAdmin, agentLimiter, async (req, res) => {
  const { room, agentName } = req.body;
  if (!isValidRoom(room) || !isValidAgentName(agentName)) {
    res.status(400).json({ error: 'Invalid parameters' });
    return;
  }

  const presentAgents = await getPresentAgentNames(room);
  if (presentAgents.has(agentName)) {
    res.json({ ok: true, dispatched: false, skipped: true });
    return;
  }

  try {
    await agentClient.createDispatch(room, agentName);
    res.json({ ok: true, dispatched: true, skipped: false });
  } catch (error) {
    console.error(`Dispatch failed for '${agentName}':`, error);
    res.status(500).json({ error: 'Agent dispatch failed' });
  }
});

app.post('/api/agent/kick', requireAdmin, agentLimiter, async (req, res) => {
  const { room, agentIdentity } = req.body;
  if (!isValidRoom(room) || !isValidIdentity(agentIdentity)) {
    res.status(400).json({ error: 'Invalid parameters' });
    return;
  }
  try {
    await roomService.removeParticipant(room, agentIdentity);
    res.json({ ok: true });
  } catch (error) {
    console.error(`Kick failed for '${agentIdentity}':`, error);
    res.status(500).json({ error: 'Agent kick failed' });
  }
});

app.post('/api/observability/events', observabilityLimiter, async (req, res) => {
  const event = buildObservabilityEvent(req.body);
  if (!event) {
    res.status(400).json({ error: 'Invalid observability event payload' });
    return;
  }

  const saved = storeObservabilityEvent(event);
  res.json(saved);
});

app.get('/api/observability/events/recent', observabilityLimiter, (req, res) => {
  const room = typeof req.query.room === 'string' && isValidRoom(req.query.room)
    ? req.query.room
    : null;
  const rawLimit = typeof req.query.limit === 'string' ? Number(req.query.limit) : 60;
  const limit = Number.isFinite(rawLimit) ? rawLimit : 60;
  res.json(getRecentObservabilityEvents(room, limit));
});

app.get('/api/observability/thermal/recent', observabilityLimiter, async (req, res) => {
  const room = typeof req.query.room === 'string' && isValidRoom(req.query.room)
    ? req.query.room
    : null;
  if (!room) {
    res.status(400).json({ error: 'Valid room is required' });
    return;
  }

  const rawLimit = typeof req.query.limit === 'string' ? Number(req.query.limit) : 80;
  const limit = Number.isFinite(rawLimit) ? rawLimit : 80;

  try {
    const events = await getRecentThermalEvents(room, limit);
    res.json(events);
  } catch (error) {
    console.error(`[Thermal] Failed to read thermal events for room '${room}':`, error);
    res.status(500).json({ error: 'Failed to read thermal events' });
  }
});

app.get('/{*splat}', (_req, res) => {
  res.sendFile(path.join(__dirname, 'dist', 'index.html'));
});

async function startServer(): Promise<void> {
  const server = createServer(app);

  const observabilityWss = new WebSocketServer({ noServer: true });

  observabilityWss.on('connection', (ws, req) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    const room = url.searchParams.get('room');
    const roomFilter = room && isValidRoom(room) ? room : null;
    observabilityClients.set(ws, roomFilter);
    sendJson(ws, {
      type: 'initial',
      data: getRecentObservabilityEvents(roomFilter, 80),
    });
    ws.on('close', () => {
      observabilityClients.delete(ws);
    });
  });

  server.on('upgrade', (req, socket, head) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (url.pathname === '/api/observability/stream') {
      observabilityWss.handleUpgrade(req, socket, head, (ws) => {
        observabilityWss.emit('connection', ws, req);
      });
      return;
    }
    socket.destroy();
  });

  server.listen(PORT, '127.0.0.1', () => {
    console.log(`Token server running on http://127.0.0.1:${PORT}`);
    if (!ADMIN_SECRET) {
      console.warn('WARNING: ADMIN_API_SECRET not set — agent management endpoints are unprotected');
    }
    if (!AUTO_DISPATCH_ON_JOIN) {
      console.log('Agent auto-dispatch on room join is disabled');
    }
  });

  const shutdown = async () => {
    for (const ws of observabilityClients.keys()) ws.close();
    server.close();
  };

  process.on('SIGTERM', () => void shutdown());
  process.on('SIGINT', () => void shutdown());
}

void startServer().catch((error) => {
  console.error('Failed to start token server:', error);
  process.exit(1);
});
