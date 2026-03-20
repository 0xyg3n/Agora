import { useEffect, useMemo, useState } from 'react';

interface OpenClawEvent {
  id: string;
  source_app: string;
  session_id: string;
  hook_event_type: string;
  payload: Record<string, unknown>;
  timestamp: number;
  model_name?: string;
}

interface OpenClawCallTrace {
  id: string;
  agentName: string;
  sessionId: string;
  status: 'active' | 'success' | 'error' | 'gap';
  isStalled: boolean;
  hasHistoryGap: boolean;
  startedAt: number | null;
  finishedAt: number | null;
  eventTime: number;
  promptPreview: string;
  responsePreview: string;
  errorText: string;
  promptChars: number | null;
  responseChars: number | null;
  durationMs: number | null;
  modelName?: string;
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

interface OpenClawEventsPanelProps {
  roomName: string;
}

const MAX_EVENTS = 80;
const STALLED_CALL_MS = 25000;
const THERMAL_POLL_MS = 2000;

function formatClockTime(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatElapsed(durationMs: number): string {
  if (durationMs < 1000) return `${durationMs}ms`;
  if (durationMs < 10000) return `${(durationMs / 1000).toFixed(1)}s`;
  if (durationMs < 60000) return `${Math.round(durationMs / 1000)}s`;
  const minutes = Math.floor(durationMs / 60000);
  const seconds = Math.round((durationMs % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function toNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toText(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function eventTone(status: OpenClawCallTrace['status'], isStalled: boolean): 'start' | 'success' | 'error' {
  if (status === 'success') return 'success';
  if (status === 'error' || status === 'gap' || isStalled) return 'error';
  return 'start';
}

function traceLabel(trace: OpenClawCallTrace): string {
  if (trace.hasHistoryGap) return 'history gap';
  if (trace.isStalled) return 'stalled';
  if (trace.status === 'active') return 'waiting';
  if (trace.status === 'success') return 'complete';
  return 'error';
}

function thermalBadge(kind: ThermalEvent['kind']): string {
  switch (kind) {
    case 'thinking':
      return 'thinking';
    case 'tool_call':
      return 'tool';
    case 'tool_result':
      return 'result';
    case 'response':
      return 'reply';
    case 'error':
      return 'error';
    case 'model':
      return 'model';
    default:
      return 'input';
  }
}

function thermalIcon(kind: ThermalEvent['kind']): string {
  switch (kind) {
    case 'thinking':
      return '🧠';
    case 'tool_call':
      return '🔧';
    case 'tool_result':
      return '✅';
    case 'response':
      return '💬';
    case 'error':
      return '🔴';
    case 'model':
      return '⚙';
    default:
      return '👤';
  }
}

function traceKey(event: OpenClawEvent): string {
  const agentName = toText(event.payload.agent_name, 'Agent');
  return `${agentName}::${event.session_id}`;
}

function toTrace(
  startEvent: OpenClawEvent | null,
  finishEvent: OpenClawEvent | null,
  now: number,
): OpenClawCallTrace {
  const sourceEvent = finishEvent || startEvent;
  const startPayload = startEvent?.payload || {};
  const finishPayload = finishEvent?.payload || {};
  const startedAt = startEvent?.timestamp ?? null;
  const finishedAt = finishEvent?.timestamp ?? null;
  const durationFromFinish = toNumber(finishPayload.duration_ms);
  const liveDuration = startedAt ? Math.max(now - startedAt, 0) : null;
  const durationMs = durationFromFinish ?? (finishEvent ? null : liveDuration);
  const errorText = toText(finishPayload.error);
  const responsePreview = toText(finishPayload.response_preview);
  const promptPreview = toText(startPayload.prompt_preview) || toText(finishPayload.prompt_preview);
  const agentName = toText(sourceEvent?.payload?.agent_name, 'Agent');
  const finishType = finishEvent?.hook_event_type || '';
  const hasHistoryGap = !startEvent && !!finishEvent && finishType !== 'OpenClawCompatibilityError';

  let status: OpenClawCallTrace['status'] = 'active';
  if (finishEvent) {
    if (hasHistoryGap) {
      status = 'gap';
    } else if (finishType.includes('Error')) {
      status = 'error';
    } else {
      status = 'success';
    }
  }

  const isStalled = status === 'active' && (durationMs ?? 0) >= STALLED_CALL_MS;

  return {
    id: finishEvent?.id || startEvent?.id || `${agentName}-${startedAt || finishedAt || now}`,
    agentName,
    sessionId: sourceEvent?.session_id || 'unknown',
    status,
    isStalled,
    hasHistoryGap,
    startedAt,
    finishedAt,
    eventTime: finishedAt || startedAt || now,
    promptPreview,
    responsePreview,
    errorText,
    promptChars: toNumber(finishPayload.prompt_chars) ?? toNumber(startPayload.prompt_chars),
    responseChars: toNumber(finishPayload.response_chars),
    durationMs,
    modelName: finishEvent?.model_name || startEvent?.model_name,
  };
}

function deriveCallTraces(events: OpenClawEvent[], now: number): OpenClawCallTrace[] {
  const ascending = [...events].sort((a, b) => a.timestamp - b.timestamp);
  const pending = new Map<string, OpenClawEvent[]>();
  const traces: OpenClawCallTrace[] = [];

  for (const event of ascending) {
    const key = traceKey(event);
    if (event.hook_event_type === 'OpenClawCallStart') {
      const queue = pending.get(key) || [];
      queue.push(event);
      pending.set(key, queue);
      continue;
    }

    if (/OpenClawCallComplete|OpenClawCallError|OpenClawCompatibilityError/.test(event.hook_event_type)) {
      const queue = pending.get(key) || [];
      const startEvent = queue.shift() || null;
      traces.push(toTrace(startEvent, event, now));
      if (queue.length > 0) pending.set(key, queue);
      else pending.delete(key);
    }
  }

  for (const queue of pending.values()) {
    for (const startEvent of queue) {
      traces.push(toTrace(startEvent, null, now));
    }
  }

  return traces.sort((a, b) => b.eventTime - a.eventTime);
}

export default function OpenClawEventsPanel({ roomName }: OpenClawEventsPanelProps) {
  const [events, setEvents] = useState<OpenClawEvent[]>([]);
  const [thermalEvents, setThermalEvents] = useState<ThermalEvent[]>([]);
  const [connectionState, setConnectionState] = useState<'connecting' | 'live' | 'reconnecting'>('connecting');
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    const intervalId = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, []);

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let reconnectTimer: number | null = null;
    let ws: WebSocket | null = null;
    let reconnectAttempts = 0;

    const loadInitial = async () => {
      try {
        const resp = await fetch(
          `/api/observability/events/recent?room=${encodeURIComponent(roomName)}&limit=${MAX_EVENTS}`,
          { signal: controller.signal },
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json() as OpenClawEvent[];
        if (alive && Array.isArray(data)) {
          setEvents(data);
        }
      } catch (error) {
        if (alive) {
          console.error('Failed to load OpenClaw events:', error);
        }
      }
    };

    const scheduleReconnect = () => {
      if (!alive || reconnectTimer !== null) return;
      setConnectionState('reconnecting');
      const delay = Math.min(1000 * (2 ** reconnectAttempts), 5000);
      reconnectAttempts += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (!alive) return;
      setConnectionState((prev) => (prev === 'live' ? 'reconnecting' : prev));
      ws = new WebSocket(
        `${protocol}//${window.location.host}/api/observability/stream?room=${encodeURIComponent(roomName)}`,
      );

      ws.addEventListener('open', () => {
        if (!alive) return;
        reconnectAttempts = 0;
        setConnectionState('live');
      });

      ws.addEventListener('message', (message) => {
        if (!alive) return;

        try {
          const parsed = JSON.parse(message.data);
          if (parsed?.type === 'initial' && Array.isArray(parsed.data)) {
            setEvents(parsed.data.slice(-MAX_EVENTS));
            return;
          }

          if (parsed?.type === 'event' && parsed.data) {
            setEvents((prev) => {
              const next = [...prev, parsed.data as OpenClawEvent];
              const deduped = next.filter((event, index, list) =>
                list.findIndex((candidate) => candidate.id === event.id) === index);
              return deduped.slice(-MAX_EVENTS);
            });
          }
        } catch (error) {
          console.error('Failed to parse OpenClaw event message:', error);
        }
      });

      ws.addEventListener('error', () => {
        ws?.close();
      });

      ws.addEventListener('close', () => {
        if (!alive) return;
        scheduleReconnect();
      });
    };

    void loadInitial();
    connect();

    return () => {
      alive = false;
      controller.abort();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [roomName]);

  useEffect(() => {
    let alive = true;

    const loadThermal = async () => {
      try {
        const resp = await fetch(
          `/api/observability/thermal/recent?room=${encodeURIComponent(roomName)}&limit=${MAX_EVENTS}`,
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json() as ThermalEvent[];
        if (alive && Array.isArray(data)) {
          setThermalEvents(data);
        }
      } catch (error) {
        if (alive) {
          console.error('Failed to load OpenClaw thermal feed:', error);
        }
      }
    };

    void loadThermal();
    const intervalId = window.setInterval(() => {
      void loadThermal();
    }, THERMAL_POLL_MS);

    return () => {
      alive = false;
      window.clearInterval(intervalId);
    };
  }, [roomName]);

  const traces = useMemo(() => deriveCallTraces(events, now), [events, now]);
  const summary = useMemo(() => {
    const active = traces.filter((trace) => trace.status === 'active').length;
    const stalled = traces.filter((trace) => trace.isStalled).length;
    const errors = traces.filter((trace) => trace.status === 'error').length;
    const gaps = traces.filter((trace) => trace.hasHistoryGap).length;
    return {
      active,
      stalled,
      errors,
      gaps,
      completed: traces.filter((trace) => trace.status === 'success').length,
    };
  }, [traces]);

  return (
    <div className="vr-openclaw-panel">
      <div className="vr-openclaw-toolbar">
        <div className="vr-openclaw-toolbar-copy">
          <span className="vr-openclaw-kicker">OpenClaw Feed</span>
          <span className="vr-openclaw-meta">
            {traces.length} calls
            <span className={`vr-openclaw-live ${connectionState === 'live' ? 'live' : ''}`}>
              {connectionState}
            </span>
          </span>
        </div>

        <div className="vr-openclaw-summary">
          <span className="vr-openclaw-summary-pill neutral">{summary.completed} done</span>
          <span className={`vr-openclaw-summary-pill ${summary.active > 0 ? 'active' : 'neutral'}`}>
            {summary.active} running
          </span>
          <span className={`vr-openclaw-summary-pill ${summary.stalled > 0 ? 'warning' : 'neutral'}`}>
            {summary.stalled} stalled
          </span>
          <span className={`vr-openclaw-summary-pill ${summary.errors + summary.gaps > 0 ? 'error' : 'neutral'}`}>
            {summary.errors + summary.gaps} gaps
          </span>
        </div>
      </div>

      <div className="vr-openclaw-list">
        {traces.length === 0 && thermalEvents.length === 0 && (
          <div className="vr-openclaw-empty">
            No OpenClaw calls yet. Calls will appear here as paired request traces once an agent starts reasoning.
          </div>
        )}

        {traces.map((trace) => {
          const tone = eventTone(trace.status, trace.isStalled);
          const detailText = trace.errorText
            || trace.responsePreview
            || trace.promptPreview
            || 'Waiting for OpenClaw response';
          const waitingText = trace.durationMs !== null ? `Waiting ${formatElapsed(trace.durationMs)}` : 'Waiting';

          return (
            <div key={trace.id} className={`vr-openclaw-event tone-${tone}`}>
              <div className="vr-openclaw-event-top">
                <div className="vr-openclaw-event-title-row">
                  <span className="vr-openclaw-agent">{trace.agentName}</span>
                  <span className={`vr-openclaw-badge tone-${tone}`}>
                    {traceLabel(trace)}
                  </span>
                </div>
                <span className="vr-openclaw-time">
                  {formatClockTime(trace.eventTime)}
                </span>
              </div>

              <div className="vr-openclaw-preview-group">
                {trace.promptPreview && (
                  <div className="vr-openclaw-preview-block">
                    <span className="vr-openclaw-preview-label">Prompt</span>
                    <div className="vr-openclaw-detail" title={trace.promptPreview}>
                      {trace.promptPreview}
                    </div>
                  </div>
                )}

                <div className="vr-openclaw-preview-block">
                  <span className="vr-openclaw-preview-label">
                    {trace.errorText
                      ? 'Issue'
                      : trace.status === 'active'
                        ? 'Status'
                        : trace.hasHistoryGap
                          ? 'Gap'
                          : 'Reply'}
                  </span>
                  <div className={`vr-openclaw-detail ${trace.errorText || trace.hasHistoryGap || trace.isStalled ? 'error' : ''}`} title={detailText}>
                    {trace.hasHistoryGap
                      ? 'Completion arrived without a visible start event. The room likely reloaded or older history rolled off.'
                      : trace.status === 'active'
                        ? waitingText
                        : detailText}
                  </div>
                </div>
              </div>

              <div className="vr-openclaw-metrics">
                <span>session {trace.sessionId.replace(/^livekit-/, '')}</span>
                {trace.promptChars !== null && <span>prompt {trace.promptChars}c</span>}
                {trace.responseChars !== null && <span>reply {trace.responseChars}c</span>}
                {trace.durationMs !== null && <span>{formatElapsed(trace.durationMs)}</span>}
                {trace.modelName && <span>{trace.modelName}</span>}
              </div>
            </div>
          );
        })}

        <div className="vr-openclaw-thermal-section">
          <div className="vr-openclaw-thermal-header">
            <div className="vr-openclaw-thermal-title-row">
              <span className="vr-openclaw-preview-label">Thermal Feed</span>
              <span className="vr-openclaw-thermal-meta">{thermalEvents.length} events</span>
            </div>
            <div className="vr-openclaw-thermal-subtitle">
              Live session timeline from the bots&apos; OpenClaw room sessions
            </div>
          </div>

          {thermalEvents.length === 0 ? (
            <div className="vr-openclaw-thermal-empty">
              No session-level thinking or tool activity yet for this room.
            </div>
          ) : (
            <div className="vr-openclaw-thermal-list">
              {thermalEvents.map((event) => (
                <div key={event.id} className={`vr-openclaw-thermal-item kind-${event.kind}`}>
                  <div className="vr-openclaw-thermal-top">
                    <div className="vr-openclaw-thermal-title">
                      <span className="vr-openclaw-thermal-icon">{thermalIcon(event.kind)}</span>
                      <span className="vr-openclaw-agent">{event.agentName}</span>
                      <span className={`vr-openclaw-badge tone-${event.kind === 'error' ? 'error' : event.kind === 'response' || event.kind === 'tool_result' ? 'success' : 'start'}`}>
                        {thermalBadge(event.kind)}
                      </span>
                    </div>
                    <span className="vr-openclaw-time">{formatClockTime(event.timestamp)}</span>
                  </div>

                  <div className={`vr-openclaw-detail ${event.kind === 'error' ? 'error' : ''}`} title={event.summary}>
                    {event.summary}
                  </div>

                  {(event.detail || event.modelName) && (
                    <div className="vr-openclaw-metrics">
                      {event.detail && <span title={event.detail}>{event.detail}</span>}
                      {event.modelName && <span>{event.modelName}</span>}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
