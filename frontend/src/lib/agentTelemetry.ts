export interface AgentTelemetry {
  name: string;
  identity: string;
  connected: boolean;
  isSpeaking: boolean;
  agentState: string;
  agentActivity: string;
  statusText: string;
  errorText: string;
  lastActivityAt: number | null;
}

export function formatRelativeTime(timestamp: number | null, now: number): string {
  if (!timestamp) return 'never';
  const diffMs = Math.max(0, now - timestamp);
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 5) return 'just now';
  if (diffSec < 60) return `${diffSec}s ago`;

  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;

  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;

  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export function formatAgentStateLabel(state: string): string {
  switch (state) {
    case 'calling_openclaw':
      return 'calling OpenClaw';
    case 'vision_processing':
      return 'vision';
    case 'disconnected':
      return 'disconnected';
    default:
      return state.replace(/_/g, ' ');
  }
}

export function deriveAgentDisplayState(
  agent: Pick<AgentTelemetry, 'connected' | 'isSpeaking' | 'agentState' | 'agentActivity'>
): string {
  if (!agent.connected) return 'disconnected';
  if (agent.isSpeaking) return 'speaking';
  if (agent.agentActivity && agent.agentActivity !== 'idle') return agent.agentActivity;
  if (agent.agentState && agent.agentState !== 'idle') return agent.agentState;
  return 'idle';
}

export function hydrateAgentTelemetrySnapshot(
  rawSnapshot: Partial<AgentTelemetry> | undefined,
  existing?: AgentTelemetry
): AgentTelemetry | null {
  if (!rawSnapshot || typeof rawSnapshot.name !== 'string') return null;

  return {
    name: rawSnapshot.name,
    identity: typeof rawSnapshot.identity === 'string'
      ? rawSnapshot.identity
      : existing?.identity || '',
    connected: rawSnapshot.connected !== false,
    isSpeaking: existing?.isSpeaking || false,
    agentState: typeof rawSnapshot.agentState === 'string'
      ? rawSnapshot.agentState
      : existing?.agentState || 'idle',
    agentActivity: typeof rawSnapshot.agentActivity === 'string'
      ? rawSnapshot.agentActivity
      : existing?.agentActivity || 'idle',
    statusText: typeof rawSnapshot.statusText === 'string'
      ? rawSnapshot.statusText
      : existing?.statusText || '',
    errorText: typeof rawSnapshot.errorText === 'string'
      ? rawSnapshot.errorText
      : existing?.errorText || '',
    lastActivityAt: typeof rawSnapshot.lastActivityAt === 'number' && Number.isFinite(rawSnapshot.lastActivityAt)
      ? rawSnapshot.lastActivityAt
      : existing?.lastActivityAt || null,
  };
}
