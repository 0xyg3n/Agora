import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  useParticipants,
  useChat,
  useLocalParticipant,
  useDisconnectButton,
  useIsSpeaking,
  useParticipantAttributes,
  useTranscriptions,
  useTracks,
  TrackToggle,
  VideoTrack,
} from '@livekit/components-react';
import type { Participant } from 'livekit-client';
import { ParticipantKind, Track } from 'livekit-client';
import { SpaceBackground } from './SpaceBackground';
import {
  deriveAgentDisplayState,
  formatAgentStateLabel,
  formatRelativeTime,
  hydrateAgentTelemetrySnapshot,
  type AgentTelemetry,
} from '../lib/agentTelemetry';
import './VoiceRoom.css';

const AgentModel3D = lazy(() => import('./AgentModel3D'));
const OpenClawEventsPanel = lazy(() => import('./OpenClawEventsPanel'));
const TerminalPanel = lazy(() => import('./TerminalPanel'));

interface VoiceRoomProps {
  onLeave: () => void;
  roomName: string;
}

/* ────────────────────────────────────────
   Toast notification types
   ──────────────────────────────────────── */
interface Toast {
  id: number;
  message: string;
  type: 'success' | 'error' | 'info';
}

interface AgentStatusResponse {
  agents?: string[];
  snapshots?: Partial<AgentTelemetry>[];
}

let toastIdCounter = 0;

const SIDEBAR_WIDTH_STORAGE_KEY = 'agora.voiceRoom.sidebarWidth';
const DEFAULT_SIDEBAR_WIDTH = 432;
const MIN_SIDEBAR_WIDTH = 340;
const MAX_SIDEBAR_WIDTH = 680;

function clampSidebarWidth(width: number): number {
  return Math.max(MIN_SIDEBAR_WIDTH, Math.min(MAX_SIDEBAR_WIDTH, Math.round(width)));
}

/* ────────────────────────────────────────
   Format time for chat timestamps
   ──────────────────────────────────────── */
function formatTime(timestamp: number | undefined): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function summarizeText(value: string, limit: number): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (!normalized) return '';
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

function getChatSenderTone(senderName: string): 'laira' | 'loki' | 'giannis' | '' {
  const normalized = senderName.trim().toLowerCase();
  if (normalized.includes('laira')) return 'laira';
  if (normalized.includes('loki')) return 'loki';
  if (normalized.includes('giannis')) return 'giannis';
  return '';
}

function AgentTelemetryTracker({
  participant,
  onUpdate,
}: {
  participant: Participant;
  onUpdate: (snapshot: AgentTelemetry) => void;
}) {
  const isSpeaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  const displayName = participant.name || attributes?.agent_name || participant.identity;
  const lastActivityRaw = attributes?.agent_last_activity_at || '';
  const lastActivityAt = lastActivityRaw ? Number(lastActivityRaw) : null;

  useEffect(() => {
    onUpdate({
      name: displayName,
      identity: participant.identity,
      connected: true,
      isSpeaking,
      agentState: attributes?.agent_state || 'idle',
      agentActivity: attributes?.agent_activity || 'idle',
      statusText: attributes?.agent_status_text || '',
      errorText: attributes?.agent_error_text || '',
      lastActivityAt: Number.isFinite(lastActivityAt) ? lastActivityAt : null,
    });
  }, [
    attributes,
    displayName,
    isSpeaking,
    lastActivityAt,
    onUpdate,
    participant.identity,
  ]);

  return null;
}

export function VoiceRoom({ onLeave, roomName }: VoiceRoomProps) {
  const participants = useParticipants();
  const localParticipant = useLocalParticipant();
  const { send, chatMessages, isSending } = useChat();
  const transcriptions = useTranscriptions();
  const { buttonProps } = useDisconnectButton({});
  const [chatInput, setChatInput] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // All configured agent names (fetched from server)
  const [allAgentNames, setAllAgentNames] = useState<string[]>([]);
  const [isCallingAgents, setIsCallingAgents] = useState(false);
  const [dispatchingAgents, setDispatchingAgents] = useState<Set<string>>(new Set());
  const [agentTelemetry, setAgentTelemetry] = useState<Record<string, AgentTelemetry>>({});
  const [telemetryNow, setTelemetryNow] = useState<number>(() => Date.now());
  const [sidebarTab, setSidebarTab] = useState<'chat' | 'openclaw' | 'terminal'>('chat');
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_SIDEBAR_WIDTH;
    const raw = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
    const parsed = raw ? Number(raw) : DEFAULT_SIDEBAR_WIDTH;
    return Number.isFinite(parsed) ? clampSidebarWidth(parsed) : DEFAULT_SIDEBAR_WIDTH;
  });
  const [isWideLayout, setIsWideLayout] = useState<boolean>(() =>
    typeof window === 'undefined' ? true : window.innerWidth > 1100);
  const [isSidebarResizing, setIsSidebarResizing] = useState(false);

  // Toast notifications
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: Toast['type'] = 'info') => {
    const id = ++toastIdCounter;
    setToasts(prev => [...prev, { id, message, type }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4000);
  }, []);

  // Local camera track for webcam preview
  const localVideoTracks = useTracks([Track.Source.Camera], { onlySubscribed: false });
  const localCamTrack = localVideoTracks.find(
    t => t.participant.identity === localParticipant.localParticipant?.identity
  );

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatMessages, transcriptions]);

  useEffect(() => {
    const intervalId = window.setInterval(() => setTelemetryNow(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, []);

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(sidebarWidth));
  }, [sidebarWidth]);

  useEffect(() => {
    const handleResize = () => {
      setIsWideLayout(window.innerWidth > 1100);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    if (!isSidebarResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      if (window.innerWidth <= 1100) return;
      const bodyRect = bodyRef.current?.getBoundingClientRect();
      if (!bodyRect) return;

      const nextWidth = clampSidebarWidth(bodyRect.right - event.clientX);
      setSidebarWidth(nextWidth);
    };

    const handlePointerUp = () => {
      setIsSidebarResizing(false);
      document.body.classList.remove('vr-is-resizing');
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp, { once: true });

    return () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      document.body.classList.remove('vr-is-resizing');
    };
  }, [isSidebarResizing]);

  const refreshAgentStatus = useCallback(async () => {
    try {
      const resp = await fetch('/api/agent/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: roomName }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json() as AgentStatusResponse;
      if (Array.isArray(data.agents) && data.agents.length > 0) {
        setAllAgentNames(data.agents);
      }
      if (!Array.isArray(data.snapshots)) return;

      setAgentTelemetry(prev => {
        let changed = false;
        const next = { ...prev };

        for (const rawSnapshot of data.snapshots || []) {
          const existing = rawSnapshot?.name ? prev[rawSnapshot.name] : undefined;
          const snapshot = hydrateAgentTelemetrySnapshot(rawSnapshot, existing);
          if (!snapshot) continue;

          if (
            existing &&
            existing.identity === snapshot.identity &&
            existing.connected === snapshot.connected &&
            existing.isSpeaking === snapshot.isSpeaking &&
            existing.agentState === snapshot.agentState &&
            existing.agentActivity === snapshot.agentActivity &&
            existing.statusText === snapshot.statusText &&
            existing.errorText === snapshot.errorText &&
            existing.lastActivityAt === snapshot.lastActivityAt
          ) {
            continue;
          }

          next[snapshot.name] = snapshot;
          changed = true;
        }

        return changed ? next : prev;
      });
    } catch (err) {
      console.error('Failed to fetch agent status:', err);
      setAllAgentNames(prev => (prev.length > 0 ? prev : ['Laira', 'Loki']));
    }
  }, [roomName]);

  useEffect(() => {
    void refreshAgentStatus();

    const intervalId = window.setInterval(() => {
      if (!document.hidden) {
        void refreshAgentStatus();
      }
    }, 10000);

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        void refreshAgentStatus();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [refreshAgentStatus]);

  const handleSend = useCallback(async () => {
    const text = chatInput.trim();
    if (!text || isSending) return;
    setChatInput('');
    await send(text);
  }, [chatInput, isSending, send]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleKickAgent = useCallback(
    async (agentIdentity: string, displayName: string) => {
      try {
        const resp = await fetch('/api/agent/kick', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ room: roomName, agentIdentity }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        addToast(`Kicked ${displayName}`, 'success');
      } catch (err) {
        console.error('Failed to kick agent:', err);
        addToast(`Failed to kick ${displayName}`, 'error');
      }
    },
    [roomName, addToast]
  );

  const handleRestartAgent = useCallback(
    async (agentIdentity: string, agentName: string) => {
      try {
        const resp = await fetch('/api/agent/restart', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ room: roomName, agentIdentity, agentName }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        addToast(`Restarting ${agentName}...`, 'success');
      } catch (err) {
        console.error('Failed to restart agent:', err);
        addToast(`Failed to restart ${agentName}`, 'error');
      }
    },
    [roomName, addToast]
  );

  const handleCallAgent = useCallback(
    async (agentName: string) => {
      setDispatchingAgents(prev => new Set(prev).add(agentName));
      try {
        const resp = await fetch('/api/agent/dispatch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ room: roomName, agentName }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        addToast(`Dispatched ${agentName}`, 'success');
      } catch (err) {
        console.error('Failed to call agent:', err);
        addToast(`Failed to dispatch ${agentName}`, 'error');
      } finally {
        setDispatchingAgents(prev => {
          const next = new Set(prev);
          next.delete(agentName);
          return next;
        });
      }
    },
    [roomName, addToast]
  );

  const handleCallAllAgents = useCallback(async () => {
    setIsCallingAgents(true);
    try {
      const resp = await fetch('/api/agent/dispatch-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: roomName }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      addToast(`Dispatched all agents`, 'success');
    } catch (err) {
      console.error('Failed to call agents:', err);
      addToast('Failed to dispatch agents', 'error');
    } finally {
      setIsCallingAgents(false);
    }
  }, [roomName, addToast]);

  const handleAgentTelemetryUpdate = useCallback((snapshot: AgentTelemetry) => {
    setAgentTelemetry(prev => {
      const existing = prev[snapshot.name];
      if (
        existing &&
        existing.identity === snapshot.identity &&
        existing.connected === snapshot.connected &&
        existing.isSpeaking === snapshot.isSpeaking &&
        existing.agentState === snapshot.agentState &&
        existing.agentActivity === snapshot.agentActivity &&
        existing.statusText === snapshot.statusText &&
        existing.errorText === snapshot.errorText &&
        existing.lastActivityAt === snapshot.lastActivityAt
      ) {
        return prev;
      }
      return { ...prev, [snapshot.name]: snapshot };
    });
  }, []);

  // Separate agents from humans
  const agents = participants.filter(p => p.kind === ParticipantKind.AGENT);
  const humans = participants.filter(p => p.kind !== ParticipantKind.AGENT);

  // Build a list of configured agents that aren't in the room
  const onlineAgentNames = new Set(agents.map(a => a.name || a.identity));
  const offlineAgentNames = allAgentNames.filter(name => !onlineAgentNames.has(name));
  const agentOnlineCount = agents.length;
  const agentOfflineCount = offlineAgentNames.length;
  const participantSummary = humans.length === 1 ? '1 human live' : `${humans.length} humans live`;
  const localIdentity = localParticipant.localParticipant?.identity || '';
  const participantByIdentity = useMemo(() => {
    const map = new Map<string, Participant>();
    if (localParticipant.localParticipant) {
      map.set(localParticipant.localParticipant.identity, localParticipant.localParticipant);
    }
    for (const participant of participants) {
      map.set(participant.identity, participant);
    }
    return map;
  }, [localParticipant.localParticipant, participants]);
  const visibleMessages = useMemo(
    () => {
      const normalizedChat = chatMessages
        .filter((message) => message.attributes?.transcription !== 'true')
        .map((message, index) => ({
          key: `chat-${message.timestamp}-${index}`,
          senderName: message.from?.name || message.from?.identity || 'Unknown',
          message: message.message,
          timestamp: message.timestamp,
          isAgent: message.from?.kind === ParticipantKind.AGENT,
          isMe: message.from?.identity === localIdentity,
          isTranscription: false,
        }));

      const normalizedTranscriptions = transcriptions
        .map((stream, index) => {
          const participant = participantByIdentity.get(stream.participantInfo.identity);
          if (participant?.kind === ParticipantKind.AGENT) return null;

          return {
            key: `transcript-${stream.streamInfo.id}-${index}`,
            senderName: participant?.name || stream.participantInfo.identity || 'Unknown',
            message: stream.text,
            timestamp: stream.streamInfo.timestamp,
            isAgent: false,
            isMe: stream.participantInfo.identity === localIdentity,
            isTranscription: true,
          };
        })
        .filter((message): message is NonNullable<typeof message> => Boolean(message));

      return [...normalizedChat, ...normalizedTranscriptions]
        .sort((a, b) => a.timestamp - b.timestamp);
    },
    [chatMessages, localIdentity, participantByIdentity, transcriptions],
  );
  const chatSummary = visibleMessages.length === 1 ? '1 message' : `${visibleMessages.length} messages`;
  const offlineAgentButtonLabel = isCallingAgents
    ? 'Dispatching...'
    : offlineAgentNames.length <= 1
      ? `Call ${offlineAgentNames[0]}`
      : `Call ${offlineAgentNames.length} Agents`;
  const roomStatusTone = agentOnlineCount > 0 ? 'live' : 'standby';
  const liveAgentOrder = agents.map(a => a.name || a.identity);
  const telemetryAgentNames = liveAgentOrder.concat(
    allAgentNames.filter(name => !liveAgentOrder.includes(name))
  );
  const agentTelemetryCards = telemetryAgentNames.map(name => {
    const live = agentTelemetry[name];
    const participant = agents.find(p => (p.name || p.identity) === name);

    if (participant) {
      return {
        ...live,
        name,
        identity: participant.identity,
        connected: true,
        isSpeaking: live?.isSpeaking || false,
        agentState: live?.agentState || 'idle',
        agentActivity: live?.agentActivity || 'idle',
        statusText: live?.statusText || 'Connected',
        errorText: live?.errorText || '',
        lastActivityAt: live?.lastActivityAt || null,
      };
    }

    if (live) {
      return {
        ...live,
        connected: false,
        isSpeaking: false,
      };
    }

    return {
      name,
      identity: '',
      connected: false,
      isSpeaking: false,
      agentState: 'idle',
      agentActivity: 'disconnected',
      statusText: 'Offline',
      errorText: '',
      lastActivityAt: null,
    };
  });
  const liveAgentTelemetryCards = agentTelemetryCards.filter((agent) => agent.connected);
  const activeAgentCard = liveAgentTelemetryCards.find(
    (agent) => deriveAgentDisplayState(agent) !== 'idle',
  ) || liveAgentTelemetryCards[0] || null;
  const activeAgentState = activeAgentCard
    ? formatAgentStateLabel(deriveAgentDisplayState(activeAgentCard))
    : 'idle';
  const activeAgentMeta = activeAgentCard
    ? `active ${formatRelativeTime(activeAgentCard.lastActivityAt, telemetryNow)}`
    : 'waiting for dispatch';
  const latestMessage = visibleMessages.length > 0 ? visibleMessages[visibleMessages.length - 1] : null;
  const latestMessageSender = latestMessage?.senderName || 'Room thread';
  const latestMessagePreview = latestMessage
    ? summarizeText(latestMessage.message, 60)
    : 'No thread yet';

  const handleSidebarResizeStart = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (window.innerWidth <= 1100) return;
    event.preventDefault();
    setIsSidebarResizing(true);
    document.body.classList.add('vr-is-resizing');
  }, []);

  const handleSidebarWidthReset = useCallback(() => {
    setSidebarWidth(DEFAULT_SIDEBAR_WIDTH);
  }, []);

  return (
    <div className="voice-room">
      <SpaceBackground />

      {/* Toast notifications */}
      {toasts.length > 0 && (
        <div className="vr-toast-container">
          {toasts.map(toast => (
            <div key={toast.id} className={`vr-toast vr-toast-${toast.type}`}>
              {toast.message}
            </div>
          ))}
        </div>
      )}

      {/* Header */}
      <header className="vr-header">
        <div className="vr-header-main">
          <div className="vr-header-left">
            <svg className="vr-logo" width="24" height="24" viewBox="0 0 32 32" fill="none">
              <circle cx="16" cy="16" r="14" stroke="url(#gl)" strokeWidth="2" />
              <circle cx="16" cy="16" r="6" fill="url(#gl)" opacity="0.6" />
              <circle cx="16" cy="16" r="2" fill="#fff" />
              <defs>
                <linearGradient id="gl" x1="0" y1="0" x2="32" y2="32">
                  <stop stopColor="#6c63ff" />
                  <stop offset="1" stopColor="#00d4aa" />
                </linearGradient>
              </defs>
            </svg>
            <div className="vr-room-context">
              <span className="vr-room-kicker">Live Room</span>
              <div className="vr-room-row">
                <span className="vr-room-name">{roomName}</span>
                <span className={`vr-header-room-pill ${roomStatusTone}`}>
                  {roomStatusTone === 'live' ? 'room live' : 'standby'}
                </span>
              </div>
              <div className="vr-room-subline">
                <span>{participants.length} total live</span>
                <span>{agentOnlineCount} AI active</span>
                <span>{humans.length} humans connected</span>
              </div>
            </div>
          </div>
          <div className="vr-header-metrics">
            <div className="vr-header-metric-card">
              <span className="vr-header-metric-label">AI stage</span>
              <span className="vr-header-metric-value">{agentOnlineCount} live</span>
            </div>
            <div className="vr-header-metric-card">
              <span className="vr-header-metric-label">Humans</span>
              <span className="vr-header-metric-value">{humans.length} connected</span>
            </div>
            <div className="vr-header-metric-card">
              <span className="vr-header-metric-label">Room thread</span>
              <span className="vr-header-metric-value">{chatSummary}</span>
            </div>
          </div>
        </div>

        <div className="vr-header-actions">
          <div className="vr-header-control-shell">
            <span className="vr-header-control-label">Local controls</span>
            <LocalMediaControls
              isMicrophoneEnabled={localParticipant.isMicrophoneEnabled}
              isCameraEnabled={localParticipant.isCameraEnabled}
            />
          </div>

          {/* Call agents button -- shows when any agents are missing */}
          {offlineAgentNames.length > 0 && (
            <button
              className="vr-call-agents-btn"
              onClick={handleCallAllAgents}
              disabled={isCallingAgents}
              title={`Dispatch: ${offlineAgentNames.join(', ')}`}
            >
              {offlineAgentButtonLabel}
            </button>
          )}

          <button
            {...buttonProps}
            className="vr-leave-btn"
            onClick={(e) => {
              buttonProps.onClick?.(e);
              onLeave();
            }}
          >
            Leave
          </button>
        </div>
      </header>

      <div ref={bodyRef} className="vr-body">
        {/* Main stage */}
        <main className="vr-stage">
          {/* AI Agents section -- 3D stage with fallback */}
          {(agents.length > 0 || offlineAgentNames.length > 0 || allAgentNames.length > 0) && (
            <div className="vr-section vr-section-primary vr-section-agents">
              <div className="vr-section-header vr-section-header-premium">
                <div className="vr-section-heading-group">
                  <div className="vr-section-label cyber-heading">AI Agents</div>
                  <div className="vr-section-intro">
                    <div className="vr-section-title">Collaborative live stage</div>
                    <div className="vr-section-copy">
                      Real-time voice presence, agent state, and OpenClaw-backed room reasoning in one stage.
                    </div>
                  </div>
                </div>
                <div className="vr-section-head-pills">
                  <span className="vr-section-head-pill primary">{agentOnlineCount} live agents</span>
                  {allAgentNames.length > 0 && (
                    <span className="vr-section-head-pill">
                      {agentOfflineCount} offline
                    </span>
                  )}
                  <span className="vr-section-head-pill subtle">{chatSummary}</span>
                </div>
              </div>
              {agents.map(p => (
                <AgentTelemetryTracker
                  key={`telemetry-${p.identity}`}
                  participant={p}
                  onUpdate={handleAgentTelemetryUpdate}
                />
              ))}
              <div className="vr-stage-command-board">
                <div className="vr-stage-command-copy">
                  <span className="vr-stage-command-kicker">Stage focus</span>
                  <div className="vr-stage-command-title">Broadcast-grade room control</div>
                  <div className="vr-stage-command-text">
                    The stage keeps live presence, agent state, and OpenClaw-backed reasoning in one view.
                  </div>
                </div>
                <div className="vr-stage-command-grid">
                  <div className="vr-stage-command-card tone-cyan">
                    <span className="vr-stage-command-label">Live agents</span>
                    <span className="vr-stage-command-value">{agentOnlineCount}</span>
                    <span className="vr-stage-command-meta">
                      {agentOfflineCount} offline · {humans.length} humans
                    </span>
                  </div>
                  <div className="vr-stage-command-card tone-violet">
                    <span className="vr-stage-command-label">Stage pulse</span>
                    <span className="vr-stage-command-value">
                      {activeAgentCard ? activeAgentCard.name : 'Waiting'}
                    </span>
                    <span className="vr-stage-command-meta">
                      {activeAgentCard
                        ? `${activeAgentState} · ${activeAgentMeta}`
                        : 'Dispatch an agent to light up the stage'}
                    </span>
                  </div>
                  <div className="vr-stage-command-card tone-amber">
                    <span className="vr-stage-command-label">Thread</span>
                    <span className="vr-stage-command-value">{chatSummary}</span>
                    <span className="vr-stage-command-meta">
                      {latestMessage ? `${latestMessageSender}: ${latestMessagePreview}` : 'Voice, text, and agent replies'}
                    </span>
                  </div>
                </div>
              </div>
              <Suspense
                fallback={
                  <div className="agent-3d-loading">
                    <div className="agent-3d-loading-inner">
                      <div className="agent-3d-loading-spinner" />
                      <span className="agent-3d-loading-text">Loading 3D Models</span>
                    </div>
                  </div>
                }
              >
                <AgentModel3D
                  participants={agents}
                  offlineAgents={offlineAgentNames}
                  agentSnapshots={agentTelemetry}
                  now={telemetryNow}
                  onKick={handleKickAgent}
                  onRestart={handleRestartAgent}
                  onCallAgent={handleCallAgent}
                  dispatchingAgents={dispatchingAgents}
                  fallback2D={
                    <div className="vr-avatars">
                      {agents.map(p => (
                        <AgentAvatar
                          key={p.identity}
                          participant={p}
                          onKick={() => handleKickAgent(p.identity, p.name || p.identity)}
                          onRestart={() =>
                            handleRestartAgent(p.identity, p.name || p.identity)
                          }
                        />
                      ))}
                      {offlineAgentNames.map(name => (
                        <OfflineAgentCard
                          key={`offline-${name}`}
                          agentName={name}
                          onCall={() => handleCallAgent(name)}
                          isDispatching={dispatchingAgents.has(name)}
                        />
                      ))}
                    </div>
                  }
                />
              </Suspense>
              <div className="vr-agent-telemetry-head">
                <div className="vr-agent-telemetry-head-copy">
                  <span className="vr-agent-telemetry-kicker">Agent telemetry</span>
                  <span className="vr-agent-telemetry-caption">
                    Presence, last activity, failures, and direct controls for each room agent.
                  </span>
                </div>
              </div>
              <div className="vr-agent-telemetry-grid">
                {agentTelemetryCards.map(agent => (
                  <AgentTelemetryCard
                    key={`telemetry-card-${agent.name}`}
                    agent={agent}
                    now={telemetryNow}
                    isDispatching={dispatchingAgents.has(agent.name)}
                    onCall={() => handleCallAgent(agent.name)}
                    onKick={() => handleKickAgent(agent.identity, agent.name)}
                    onRestart={() => handleRestartAgent(agent.identity, agent.name)}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Humans section */}
          <div className="vr-section vr-section-secondary">
            <div className="vr-section-header vr-section-header-premium">
              <div className="vr-section-heading-group">
                <div className="vr-section-label">Participants</div>
                <div className="vr-section-intro">
                  <div className="vr-section-title">Human presence</div>
                  <div className="vr-section-copy">
                    Local media controls live in the command bar above so the room body can stay focused on the call itself.
                  </div>
                </div>
              </div>
              <div className="vr-section-meta">{participantSummary}</div>
            </div>
            <div className="vr-avatars">
              {humans.map(p => {
                const isLocal = p.identity === localParticipant.localParticipant?.identity;
                return (
                  <ParticipantAvatar
                    key={p.identity}
                    participant={p}
                    isLocal={isLocal}
                    localCamTrack={isLocal ? localCamTrack : undefined}
                    isCameraEnabled={isLocal ? localParticipant.isCameraEnabled : false}
                  />
                );
              })}
            </div>
          </div>
        </main>

        <div
          className={`vr-sidebar-resizer ${isSidebarResizing ? 'active' : ''}`}
          onPointerDown={handleSidebarResizeStart}
          onDoubleClick={handleSidebarWidthReset}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize sidebar"
          title="Drag to resize sidebar"
        />

        <aside className="vr-chat" style={isWideLayout ? { width: `${sidebarWidth}px` } : undefined}>
          <div className="vr-sidebar-topline">
            <div className="vr-sidebar-topline-copy">
              <span className="vr-sidebar-kicker">Room Ops</span>
              <span className="vr-sidebar-room">{roomName}</span>
            </div>
            <div className="vr-sidebar-topline-stats">
              <span>{chatSummary}</span>
              <span>{agentOnlineCount} AI</span>
              <span>{humans.length} humans</span>
            </div>
          </div>
          <div className="vr-sidebar-summary">
            <div className="vr-sidebar-summary-card tone-cyan">
              <span className="vr-sidebar-summary-label">Room pulse</span>
              <span className="vr-sidebar-summary-value">
                {activeAgentCard ? `${activeAgentCard.name} ${activeAgentState}` : 'Awaiting agents'}
              </span>
              <span className="vr-sidebar-summary-meta">{activeAgentMeta}</span>
            </div>
            <div className="vr-sidebar-summary-card tone-violet">
              <span className="vr-sidebar-summary-label">Thread</span>
              <span className="vr-sidebar-summary-value">{chatSummary}</span>
              <span className="vr-sidebar-summary-meta">
                {latestMessage ? `${latestMessageSender}: ${latestMessagePreview}` : 'No room thread yet'}
              </span>
            </div>
            <div className="vr-sidebar-summary-card tone-amber">
              <span className="vr-sidebar-summary-label">Ops</span>
              <span className="vr-sidebar-summary-value">{agentOnlineCount} live</span>
              <span className="vr-sidebar-summary-meta">
                {agentOfflineCount} offline · OpenClaw and terminal ready
              </span>
            </div>
          </div>
          <div className="vr-sidebar-tabs">
            <button
              className={`vr-sidebar-tab ${sidebarTab === 'chat' ? 'active' : ''}`}
              onClick={() => setSidebarTab('chat')}
            >
              Chat
              <span className="vr-sidebar-tab-meta">{visibleMessages.length}</span>
            </button>
            <button
              className={`vr-sidebar-tab ${sidebarTab === 'openclaw' ? 'active' : ''}`}
              onClick={() => setSidebarTab('openclaw')}
            >
              OpenClaw
            </button>
            <button
              className={`vr-sidebar-tab ${sidebarTab === 'terminal' ? 'active' : ''}`}
              onClick={() => setSidebarTab('terminal')}
            >
              Terminal
            </button>
          </div>

          {sidebarTab === 'chat' && (
            <>
              <div className="vr-chat-header">
                <span>Chat</span>
                <span className="vr-chat-header-meta">{chatSummary}</span>
              </div>
              <div className="vr-chat-messages">
                {visibleMessages.length === 0 && (
                  <div className="vr-chat-empty">
                    <span className="vr-chat-empty-title">Room thread is ready</span>
                    <span className="vr-chat-empty-copy">
                      Speak or type to seed the conversation. Human transcripts, agent replies, and typed chat appear together here in order. Mention Laira or Loki to trigger a turn.
                    </span>
                    <div className="vr-chat-empty-pills">
                      <span>voice transcripts</span>
                      <span>agent replies</span>
                      <span>typed chat</span>
                    </div>
                  </div>
                )}
                {visibleMessages.map((msg, i) => {
                  const senderTone = getChatSenderTone(msg.senderName);
                  const senderToneClass = senderTone ? `sender-${senderTone}` : '';
                  return (
                    <div
                      key={msg.key || i}
                      className={`vr-chat-msg ${msg.isAgent ? 'agent' : ''} ${msg.isMe ? 'me' : ''} ${msg.isTranscription ? 'transcription' : ''} ${senderToneClass}`}
                    >
                      <span className={`vr-chat-sender ${msg.isAgent ? 'agent' : ''} ${senderToneClass}`}>
                        {msg.isAgent && <span className={`vr-agent-badge ${senderToneClass}`}>AI</span>}
                        {msg.isTranscription && <span className="vr-chat-voice-badge">voice</span>}
                        {msg.senderName}
                        <span className="vr-chat-time">{formatTime(msg.timestamp)}</span>
                      </span>
                      <span className="vr-chat-text">{msg.message}</span>
                    </div>
                  );
                })}
                <div ref={chatEndRef} />
              </div>
              <div className="vr-chat-input-row">
                <input
                  className="vr-chat-input"
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Type a message or mention Laira / Loki..."
                  disabled={isSending}
                />
                <button
                  className="vr-chat-send"
                  onClick={handleSend}
                  disabled={isSending || !chatInput.trim()}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                    <path d="M22 2L11 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                    <path d="M22 2L15 22L11 13L2 9L22 2Z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
            </>
          )}

          {sidebarTab === 'openclaw' && (
            <Suspense fallback={<div className="vr-sidebar-loading">Loading OpenClaw feed...</div>}>
              <OpenClawEventsPanel roomName={roomName} />
            </Suspense>
          )}

          {sidebarTab === 'terminal' && (
            <Suspense fallback={<div className="vr-sidebar-loading">Loading terminal...</div>}>
              <TerminalPanel roomName={roomName} />
            </Suspense>
          )}
        </aside>
      </div>
    </div>
  );
}

/* ────────────────────────────────────────
   Agent avatar with kick/restart buttons
   ──────────────────────────────────────── */
function AgentAvatar({
  participant,
  onKick,
  onRestart,
}: {
  participant: Participant;
  onKick: () => void;
  onRestart: () => void;
}) {
  const isSpeaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  const agentState = attributes?.agent_state || 'idle';
  const displayName = participant.name || attributes?.agent_name || participant.identity;
  const initials = getInitials(displayName);
  const hue = hashToHue(displayName);

  return (
    <div
      className={[
        'vr-avatar',
        'agent',
        isSpeaking ? 'speaking' : '',
        `state-${agentState}`,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {/* Speaking ring animation */}
      {isSpeaking && <div className="vr-speaking-ring" />}

      {/* Avatar circle -- hue set via CSS custom property */}
      <div
        className="vr-avatar-circle"
        style={{
          '--avatar-hue': hue,
          background: `linear-gradient(135deg, hsl(${hue}, 70%, 45%), hsl(${hue + 40}, 60%, 35%))`,
        } as React.CSSProperties}
      >
        <span className="vr-avatar-initials">{initials}</span>

        {/* Kick button (X on top-right) */}
        <button
          className="vr-kick-btn"
          title={`Kick ${displayName}`}
          onClick={(e) => { e.stopPropagation(); onKick(); }}
          aria-label={`Kick ${displayName}`}
        >
          X
        </button>

        {/* Restart button (cycle on top-left) */}
        <button
          className="vr-restart-btn"
          title={`Restart ${displayName}`}
          onClick={(e) => { e.stopPropagation(); onRestart(); }}
          aria-label={`Restart ${displayName}`}
        >
          &#x21bb;
        </button>
      </div>

      {/* Name + state */}
      <div className="vr-avatar-info">
        <span className="vr-avatar-name">{displayName}</span>
        <span className={`vr-avatar-state ${agentState}`}>
          <span className={`vr-state-dot ${agentState}`} />
          {agentState}
        </span>
      </div>
    </div>
  );
}

function AgentTelemetryCard({
  agent,
  now,
  isDispatching,
  onCall,
  onKick,
  onRestart,
}: {
  agent: AgentTelemetry;
  now: number;
  isDispatching: boolean;
  onCall: () => void;
  onKick: () => void;
  onRestart: () => void;
}) {
  const displayState = deriveAgentDisplayState(agent);
  const detailText = agent.errorText || agent.statusText || (agent.connected ? 'Awaiting next input' : 'Offline');
  const lastActivityLabel = agent.connected
    ? `active ${formatRelativeTime(agent.lastActivityAt, now)}`
    : `last seen ${formatRelativeTime(agent.lastActivityAt, now)}`;

  return (
    <div className={`vr-telemetry-card state-${displayState}`}>
      <div className="vr-telemetry-top">
        <div className="vr-telemetry-name-row">
          <span className="vr-telemetry-name">{agent.name}</span>
          <span className={`vr-telemetry-connection ${agent.connected ? 'online' : 'offline'}`}>
            {agent.connected ? 'online' : 'offline'}
          </span>
        </div>
        <div className="vr-telemetry-status-row">
          <span className={`vr-telemetry-state state-${displayState}`}>
            <span className={`vr-state-dot ${displayState}`} />
            {formatAgentStateLabel(displayState)}
          </span>
          <span className="vr-telemetry-meta">{lastActivityLabel}</span>
        </div>
      </div>

      <div
        className={`vr-telemetry-detail-surface ${agent.errorText ? 'error' : ''}`}
        title={detailText}
      >
        <div className={`vr-telemetry-detail ${agent.errorText ? 'error' : ''}`}>
          {detailText}
        </div>
      </div>

      <div className="vr-telemetry-actions">
        {agent.connected ? (
          <>
            <button
              className="vr-telemetry-btn secondary"
              onClick={onRestart}
              disabled={!agent.identity}
            >
              Restart
            </button>
            <button
              className="vr-telemetry-btn danger"
              onClick={onKick}
              disabled={!agent.identity}
            >
              Kick
            </button>
          </>
        ) : (
          <button
            className="vr-telemetry-btn primary"
            onClick={onCall}
            disabled={isDispatching}
          >
            {isDispatching ? 'Calling...' : 'Call'}
          </button>
        )}
      </div>
    </div>
  );
}

/* ────────────────────────────────────────
   Offline agent card (kicked, not in room)
   ──────────────────────────────────────── */
function OfflineAgentCard({
  agentName,
  onCall,
  isDispatching,
}: {
  agentName: string;
  onCall: () => void;
  isDispatching: boolean;
}) {
  const initials = getInitials(agentName);
  const hue = hashToHue(agentName);

  return (
    <div className="vr-avatar agent state-offline vr-offline-agent">
      {/* Avatar circle (dimmed for offline) */}
      <div
        className="vr-avatar-circle"
        style={{
          background: `linear-gradient(135deg, hsl(${hue}, 30%, 30%), hsl(${hue + 40}, 25%, 25%))`,
        }}
      >
        <span className="vr-avatar-initials">{initials}</span>
      </div>

      {/* Name + offline indicator */}
      <div className="vr-avatar-info">
        <span className="vr-avatar-name">{agentName}</span>
        <span className="vr-avatar-state vr-state-offline-text">
          <span className="vr-state-dot vr-dot-offline" />
          offline
        </span>
      </div>

      {/* Call button */}
      <button
        className="vr-call-btn"
        onClick={onCall}
        disabled={isDispatching}
      >
        {isDispatching ? 'Calling...' : 'Call'}
      </button>
    </div>
  );
}

/* ────────────────────────────────────────
   Human participant avatar
   ──────────────────────────────────────── */
function ParticipantAvatar({
  participant,
  isLocal = false,
  localCamTrack,
  isCameraEnabled = false,
}: {
  participant: Participant;
  isLocal?: boolean;
  localCamTrack?: ReturnType<typeof useTracks>[number];
  isCameraEnabled?: boolean;
}) {
  const isSpeaking = useIsSpeaking(participant);
  useParticipantAttributes({ participant });
  const displayName = participant.name || participant.identity;
  const initials = getInitials(displayName);
  const hue = hashToHue(displayName);

  // Only show video when camera is enabled AND the track has a real publication
  const hasRealTrack = localCamTrack && 'publication' in localCamTrack && localCamTrack.publication;
  const showVideo = isLocal && isCameraEnabled && hasRealTrack;

  return (
    <div
      className={[
        'vr-avatar',
        'human',
        isSpeaking ? 'speaking' : '',
        isLocal ? 'local' : '',
        showVideo ? 'has-video' : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {/* Speaking ring animation */}
      {isSpeaking && <div className="vr-speaking-ring" />}

      {/* Video preview OR avatar circle */}
      {showVideo ? (
        <div className="vr-avatar-video-wrapper">
          <VideoTrack trackRef={localCamTrack as Parameters<typeof VideoTrack>[0]['trackRef']} className="vr-avatar-video" />
        </div>
      ) : (
        <div
          className="vr-avatar-circle"
          style={{
            '--avatar-hue': hue,
            background: `hsl(${hue}, 50%, 30%)`,
          } as React.CSSProperties}
        >
          <span className="vr-avatar-initials">{initials}</span>
        </div>
      )}

      {/* Name + state */}
      <div className="vr-avatar-info">
        <span className="vr-avatar-name">
          {displayName}
          {isLocal && <span className="vr-you-badge">you</span>}
        </span>
        {isSpeaking && (
          <span className="vr-avatar-state speaking">
            <span className="vr-state-dot speaking" />
            speaking
          </span>
        )}
      </div>

    </div>
  );
}

function LocalMediaControls({
  isMicrophoneEnabled,
  isCameraEnabled,
}: {
  isMicrophoneEnabled: boolean;
  isCameraEnabled: boolean;
}) {
  return (
    <div className="vr-header-media-controls">
      <TrackToggle source={Track.Source.Microphone} className="vr-header-media-btn" showIcon={false}>
        {isMicrophoneEnabled ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z" fill="currentColor" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            <line x1="12" y1="19" x2="12" y2="23" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3Z" fill="currentColor" opacity="0.3" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            <line x1="2" y1="2" x2="22" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round" opacity="0.6" />
          </svg>
        )}
      </TrackToggle>
      <TrackToggle source={Track.Source.Camera} className="vr-header-media-btn" showIcon={false}>
        {isCameraEnabled ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <rect x="2" y="5" width="15" height="14" rx="2" fill="currentColor" />
            <path d="M17 9l5-3v12l-5-3V9Z" fill="currentColor" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <rect x="2" y="5" width="15" height="14" rx="2" fill="currentColor" opacity="0.3" />
            <path d="M17 9l5-3v12l-5-3V9Z" fill="currentColor" opacity="0.3" />
            <line x1="2" y1="2" x2="22" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round" opacity="0.6" />
          </svg>
        )}
      </TrackToggle>
      <TrackToggle source={Track.Source.ScreenShare} className="vr-header-media-btn" showIcon={false}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <rect x="2" y="3" width="20" height="14" rx="2" stroke="currentColor" strokeWidth="2" />
          <path d="M8 21h8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          <path d="M12 17v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          <path d="M12 7v4m0 0l-2-2m2 2l2-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </TrackToggle>
    </div>
  );
}

/* ────────────────────────────────────────
   Helpers
   ──────────────────────────────────────── */
function getInitials(name: string): string {
  return name
    .split(/[\s-]+/)
    .map(w => w[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase();
}

function hashToHue(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}
