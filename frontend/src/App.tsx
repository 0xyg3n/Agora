import { useState, useCallback, useEffect } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
} from '@livekit/components-react';
import '@livekit/components-styles';
import { VoiceRoom } from './components/VoiceRoom';
import './App.css';

const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL || 'ws://localhost:7880';
const TOKEN_ENDPOINT = import.meta.env.VITE_TOKEN_ENDPOINT || '/api/token';

function App() {
  const [token, setToken] = useState<string>('');
  const [room, setRoom] = useState<string>('agora-comms');
  const [username, setUsername] = useState<string>('');
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [agents, setAgents] = useState<string[]>([]);

  // Fetch available agents on mount
  useEffect(() => {
    fetch('/api/agents')
      .then(r => r.json())
      .then(d => setAgents(d.agents || []))
      .catch(() => setAgents(['Laira', 'Loki']));
  }, []);

  const handleStartCall = useCallback(async () => {
    if (!username.trim()) return;
    setConnecting(true);
    try {
      const resp = await fetch(TOKEN_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          room,
          identity: username.trim().toLowerCase().replace(/\s+/g, '-'),
          name: username.trim(),
        }),
      });
      if (!resp.ok) throw new Error('Failed to get token');
      const data = await resp.json();
      setToken(data.token);
      setConnected(true);
    } catch (err) {
      console.error('Token error:', err);
      alert('Failed to connect. Check console.');
    } finally {
      setConnecting(false);
    }
  }, [room, username]);

  const handleDisconnect = useCallback(() => {
    setConnected(false);
    setToken('');
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleStartCall();
  }, [handleStartCall]);

  if (!connected) {
    return (
      <div className="call-screen">
        {/* Background glow */}
        <div className="call-bg-glow" />

        <div className="call-card">
          {/* Logo */}
          <div className="call-logo">
            <svg viewBox="0 0 48 48" fill="none" aria-hidden="true">
              <circle cx="24" cy="24" r="22" stroke="url(#cg)" strokeWidth="2" />
              <circle cx="24" cy="24" r="10" fill="url(#cg)" opacity="0.5" />
              <circle cx="24" cy="24" r="4" fill="#fff" />
              <defs>
                <linearGradient id="cg" x1="0" y1="0" x2="48" y2="48">
                  <stop stopColor="#6c63ff" />
                  <stop offset="1" stopColor="#00d4aa" />
                </linearGradient>
              </defs>
            </svg>
          </div>
          <h1 className="call-title">Agora</h1>
          <p className="call-subtitle">AI Voice Collaboration Platform</p>

          {/* Agent avatars preview */}
          <div className="call-agents-preview">
            {agents.map(name => (
              <div key={name} className="call-agent-chip">
                <div
                  className="call-agent-dot"
                  style={{ background: `hsl(${hashHue(name)}, 70%, 50%)` }}
                >
                  {name[0]}
                </div>
                <span>{name}</span>
                <span className="call-agent-ready">ready</span>
              </div>
            ))}
          </div>

          {/* Inputs */}
          <div className="call-fields">
            <div className="call-field">
              <label>Your name</label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Enter your name"
                autoFocus
              />
            </div>
            <div className="call-field">
              <label>Room</label>
              <input
                type="text"
                value={room}
                onChange={e => setRoom(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Room name"
              />
            </div>
          </div>

          {/* Start call button */}
          <button
            className="call-start-btn"
            onClick={handleStartCall}
            disabled={connecting || !username.trim()}
          >
            {connecting ? (
              <span className="call-btn-loading">
                <span className="call-btn-spinner" />
                <span className="call-btn-loading-text">Connecting...</span>
              </span>
            ) : (
              <>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                  <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92Z" fill="currentColor"/>
                </svg>
                Start Call
              </>
            )}
          </button>
        </div>
      </div>
    );
  }

  return (
    <LiveKitRoom
      token={token}
      serverUrl={LIVEKIT_URL}
      connect={true}
      onDisconnected={handleDisconnect}
      video={false}
      audio={false}
    >
      <VoiceRoom onLeave={handleDisconnect} roomName={room} />
      <RoomAudioRenderer />
    </LiveKitRoom>
  );
}

function hashHue(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash);
  return Math.abs(hash) % 360;
}

export default App;
