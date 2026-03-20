import test from 'node:test';
import assert from 'node:assert/strict';

import {
  deriveAgentDisplayState,
  formatAgentStateLabel,
  hydrateAgentTelemetrySnapshot,
} from '../src/lib/agentTelemetry.ts';

test('deriveAgentDisplayState respects disconnected and speaking precedence', () => {
  assert.equal(
    deriveAgentDisplayState({
      connected: false,
      isSpeaking: true,
      agentState: 'thinking',
      agentActivity: 'calling_openclaw',
    }),
    'disconnected'
  );

  assert.equal(
    deriveAgentDisplayState({
      connected: true,
      isSpeaking: true,
      agentState: 'thinking',
      agentActivity: 'calling_openclaw',
    }),
    'speaking'
  );

  assert.equal(
    deriveAgentDisplayState({
      connected: true,
      isSpeaking: false,
      agentState: 'thinking',
      agentActivity: 'calling_openclaw',
    }),
    'calling_openclaw'
  );
});

test('hydrateAgentTelemetrySnapshot keeps server shape stable and preserves local speaking state', () => {
  const existing = {
    name: 'Loki',
    identity: 'agent-1',
    connected: true,
    isSpeaking: true,
    agentState: 'thinking',
    agentActivity: 'thinking',
    statusText: 'Reply ready',
    errorText: '',
    lastActivityAt: 123,
  };

  const hydrated = hydrateAgentTelemetrySnapshot(
    {
      name: 'Loki',
      connected: false,
      agentActivity: 'disconnected',
      statusText: 'Offline',
    },
    existing
  );

  assert.ok(hydrated);
  assert.equal(hydrated?.name, 'Loki');
  assert.equal(hydrated?.identity, 'agent-1');
  assert.equal(hydrated?.connected, false);
  assert.equal(hydrated?.isSpeaking, true);
  assert.equal(hydrated?.lastActivityAt, 123);
});

test('formatAgentStateLabel exposes stable operator-friendly labels', () => {
  assert.equal(formatAgentStateLabel('calling_openclaw'), 'calling OpenClaw');
  assert.equal(formatAgentStateLabel('vision_processing'), 'vision');
  assert.equal(formatAgentStateLabel('idle'), 'idle');
});
