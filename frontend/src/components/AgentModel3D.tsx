import {
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
  type ReactNode,
} from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { useGLTF, Environment } from '@react-three/drei';
import * as THREE from 'three';
import {
  useIsSpeaking,
  useParticipantAttributes,
} from '@livekit/components-react';
import type { Participant } from 'livekit-client';
import {
  deriveAgentDisplayState,
  formatAgentStateLabel,
  formatRelativeTime,
  type AgentTelemetry,
} from '../lib/agentTelemetry';
import { should3DRender } from '../lib/deviceCapabilities';
import './AgentModel3D.css';

/* ────────────────────────────────────────
   Types
   ──────────────────────────────────────── */

interface AgentState {
  isSpeaking: boolean;
  agentState: string;
  agentActivity: string;
  displayName: string;
  statusText: string;
  errorText: string;
  lastActivityAt: number | null;
}

interface AgentModel3DProps {
  participants: Participant[];
  offlineAgents: string[];
  agentSnapshots: Record<string, AgentTelemetry>;
  now: number;
  onKick: (identity: string, name: string) => void;
  onRestart: (identity: string, name: string) => void;
  onCallAgent: (name: string) => void;
  dispatchingAgents: Set<string>;
  /** Fallback renderer for 2D mode */
  fallback2D: ReactNode;
}

/* ────────────────────────────────────────
   Agent color mapping
   ──────────────────────────────────────── */

function getAgentColor(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes('laira') || lower.includes('claude')) return '#a855f7';
  if (lower.includes('loki') || lower.includes('gpt') || lower.includes('codex')) return '#22d3ee';
  // Fallback: hash to purple or cyan
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return hash % 2 === 0 ? '#a855f7' : '#22d3ee';
}

function getAgentClass(name: string): string {
  const lower = name.toLowerCase();
  if (lower.includes('laira') || lower.includes('claude')) return 'laira';
  return 'loki';
}

/* ────────────────────────────────────────
   AgentStateExtractor — runs outside Canvas
   to call LiveKit hooks, feeds shared state
   ──────────────────────────────────────── */

function AgentStateExtractor({
  participant,
  onUpdate,
}: {
  participant: Participant;
  onUpdate: (identity: string, state: AgentState) => void;
}) {
  const isSpeaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  const agentState = attributes?.agent_state || 'idle';
  const agentActivity = attributes?.agent_activity || 'idle';
  const displayName = participant.name || attributes?.agent_name || participant.identity;
  const statusText = attributes?.agent_status_text || '';
  const errorText = attributes?.agent_error_text || '';
  const lastActivityRaw = attributes?.agent_last_activity_at || '';
  const lastActivityAt = lastActivityRaw ? Number(lastActivityRaw) : null;

  useEffect(() => {
    onUpdate(participant.identity, {
      isSpeaking,
      agentState,
      agentActivity,
      displayName,
      statusText,
      errorText,
      lastActivityAt: Number.isFinite(lastActivityAt) ? lastActivityAt : null,
    });
  }, [
    participant.identity,
    isSpeaking,
    agentState,
    agentActivity,
    displayName,
    statusText,
    errorText,
    lastActivityAt,
    onUpdate,
  ]);

  return null;
}

function StageCameraRig({ focusY }: { focusY: number }) {
  useFrame(({ camera }) => {
    camera.lookAt(0, focusY, 0);
  });

  return null;
}

/* ────────────────────────────────────────
   RobotModel — 3D model inside Canvas
   ──────────────────────────────────────── */

function RobotModel({
  position,
  baseScale,
  agentColor,
  agentState,
}: {
  position: [number, number, number];
  baseScale: number;
  agentColor: string;
  agentState: AgentState;
}) {
  const gltf = useGLTF('/models/model.glb');
  const modelRef = useRef<THREE.Group>(null);
  const motionRef = useRef({
    y: position[1],
    rotX: 0,
    rotY: 0,
    rotZ: 0,
    scale: baseScale,
  });

  const clonedScene = useMemo(() => {
    const cloned = gltf.scene.clone();
    cloned.traverse((child) => {
      if (child instanceof THREE.Mesh) {
        if (Array.isArray(child.material)) {
          child.material = child.material.map((m: THREE.Material) => m.clone());
        } else if (child.material) {
          child.material = child.material.clone();
        }
      }
    });
    const bounds = new THREE.Box3().setFromObject(cloned);
    const center = bounds.getCenter(new THREE.Vector3());
    cloned.position.set(-center.x, -bounds.min.y, -center.z);
    return cloned;
  }, [gltf.scene]);

  // Apply agent-specific materials
  useEffect(() => {
    const color = new THREE.Color(agentColor);
    clonedScene.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return;
      const mat = child.material as THREE.MeshStandardMaterial;
      if (!mat?.emissive) return;
      const meshName = child.name.toLowerCase();

      const isEye = meshName.includes('eye') || meshName.includes('psphere3');

      if (isEye) {
        mat.metalness = 0.2;
        mat.roughness = 0.05;
        mat.emissive.set(color);
        mat.emissiveIntensity = 0.8;
        if ('clearcoat' in mat) {
          (mat as THREE.MeshPhysicalMaterial).clearcoat = 0.95;
          (mat as THREE.MeshPhysicalMaterial).clearcoatRoughness = 0.05;
        }
      }
    });
  }, [clonedScene, agentColor]);

  // Animate based on state
  useFrame(({ clock }) => {
    if (!modelRef.current) return;
    const t = clock.getElapsedTime();
    const { isSpeaking } = agentState;
    const state = deriveAgentDisplayState({
      connected: true,
      isSpeaking: agentState.isSpeaking,
      agentState: agentState.agentState,
      agentActivity: agentState.agentActivity,
    });

    let targetY = position[1];
    let targetRotX = 0;
    let targetRotY = 0;
    let targetRotZ = 0;
    let targetScale = baseScale;

    if (isSpeaking) {
      targetY += Math.sin(t * 8) * 0.05;
      targetRotX = -0.03 + Math.sin(t * 4) * 0.015;
      targetRotY = Math.sin(t * 3.2) * 0.12;
      targetRotZ = Math.sin(t * 5.5) * 0.03;
      targetScale = baseScale * (1.03 + Math.sin(t * 6) * 0.015);
    } else if (state === 'thinking' || state === 'calling_openclaw' || state === 'vision_processing') {
      targetY += Math.sin(t * 5.2) * 0.04;
      targetRotX = 0.025;
      targetRotY = Math.sin(t * 1.8) * 0.2;
      targetRotZ = Math.sin(t * 3.8) * 0.025;
      targetScale = baseScale * (1.015 + Math.sin(t * 3.5) * 0.008);
    } else if (state === 'listening') {
      targetY += Math.sin(t * 2.2) * 0.018;
      targetRotX = 0.06;
      targetRotY = Math.sin(t * 1.4) * 0.08;
      targetScale = baseScale * 1.01;
    } else if (state === 'error') {
      targetY += Math.sin(t * 2.5) * 0.02;
      targetRotX = 0.015;
      targetRotY = Math.sin(t * 8) * 0.045;
      targetRotZ = Math.sin(t * 10) * 0.045;
      targetScale = baseScale * 0.992;
    } else {
      targetY += Math.sin(t * 0.9) * 0.015;
      targetRotY = Math.sin(t * 0.5) * 0.05;
      targetScale = baseScale * (1 + Math.sin(t * 1.2) * 0.005);
    }

    motionRef.current.y = THREE.MathUtils.lerp(motionRef.current.y, targetY, 0.12);
    motionRef.current.rotX = THREE.MathUtils.lerp(motionRef.current.rotX, targetRotX, 0.12);
    motionRef.current.rotY = THREE.MathUtils.lerp(motionRef.current.rotY, targetRotY, 0.12);
    motionRef.current.rotZ = THREE.MathUtils.lerp(motionRef.current.rotZ, targetRotZ, 0.14);
    motionRef.current.scale = THREE.MathUtils.lerp(motionRef.current.scale, targetScale, 0.12);

    modelRef.current.position.set(position[0], motionRef.current.y, position[2]);
    modelRef.current.rotation.x = motionRef.current.rotX;
    modelRef.current.rotation.y = motionRef.current.rotY;
    modelRef.current.rotation.z = motionRef.current.rotZ;
    modelRef.current.scale.setScalar(motionRef.current.scale);

    // Dynamic material updates
    const baseColor = new THREE.Color(agentColor);
    const listeningColor = new THREE.Color('#39ffb6');
    const thinkingColor = new THREE.Color('#ff6b00');
    const callingColor = new THREE.Color('#22d3ee');
    const visionColor = new THREE.Color('#a855f7');
    const errorColor = new THREE.Color('#ff4466');
    const processingColor = state === 'vision_processing'
      ? visionColor
      : state === 'calling_openclaw'
        ? callingColor
        : thinkingColor;

    clonedScene.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return;
      const mat = child.material as THREE.MeshStandardMaterial;
      if (!mat?.emissive) return;
      const meshName = child.name.toLowerCase();
      const isEye = meshName.includes('eye') || meshName.includes('psphere3');

      if (isEye) {
        if (isSpeaking) {
          mat.emissive.copy(baseColor);
          mat.emissiveIntensity = 0.9 + Math.sin(t * 8) * 0.35;
        } else if (state === 'thinking' || state === 'calling_openclaw' || state === 'vision_processing') {
          const lerpAmt = 0.5 + Math.sin(t * 3) * 0.3;
          mat.emissive.copy(baseColor).lerp(processingColor, lerpAmt);
          mat.emissiveIntensity = 0.75 + Math.sin(t * 4) * 0.24;
        } else if (state === 'listening') {
          mat.emissive.copy(baseColor).lerp(listeningColor, 0.65);
          mat.emissiveIntensity = 0.42 + Math.sin(t * 3.2) * 0.1;
        } else if (state === 'error') {
          mat.emissive.copy(errorColor);
          mat.emissiveIntensity = 0.7 + Math.sin(t * 7) * 0.16;
        } else {
          mat.emissive.set(baseColor);
          mat.emissiveIntensity = 0.16 + Math.sin(t * 1.2) * 0.03;
        }
      } else {
        // Body tint during processing
        if (state === 'thinking' || state === 'calling_openclaw' || state === 'vision_processing') {
          mat.emissive.copy(new THREE.Color('#1a1a2e')).lerp(processingColor, 0.12);
          mat.emissiveIntensity = 0.13 + Math.sin(t * 2) * 0.05;
        } else if (state === 'listening') {
          mat.emissive.copy(new THREE.Color('#10202b')).lerp(listeningColor, 0.08);
          mat.emissiveIntensity = 0.08 + Math.sin(t * 1.5) * 0.02;
        } else if (isSpeaking) {
          mat.emissive.copy(baseColor);
          mat.emissiveIntensity = 0.08 + Math.sin(t * 5) * 0.03;
        } else if (state === 'error') {
          mat.emissive.copy(errorColor);
          mat.emissiveIntensity = 0.08 + Math.sin(t * 4) * 0.04;
        } else {
          mat.emissiveIntensity = 0;
        }
      }
    });
  });

  return (
    <group ref={modelRef} position={position}>
      <primitive object={clonedScene} />
    </group>
  );
}

/* ────────────────────────────────────────
   Main AgentModel3D Component
   ──────────────────────────────────────── */

export default function AgentModel3D({
  participants,
  offlineAgents,
  agentSnapshots,
  now,
  onKick,
  onRestart,
  onCallAgent,
  dispatchingAgents,
  fallback2D,
}: AgentModel3DProps) {
  const [agentStates, setAgentStates] = useState<Record<string, AgentState>>({});

  const handleStateUpdate = useCallback(
    (identity: string, state: AgentState) => {
      setAgentStates((prev) => {
        const existing = prev[identity];
        if (
          existing &&
          existing.isSpeaking === state.isSpeaking &&
          existing.agentState === state.agentState &&
          existing.agentActivity === state.agentActivity &&
          existing.displayName === state.displayName &&
          existing.statusText === state.statusText &&
          existing.errorText === state.errorText &&
          existing.lastActivityAt === state.lastActivityAt
        ) {
          return prev;
        }
        return { ...prev, [identity]: state };
      });
    },
    []
  );

  // Check if 3D is supported
  const use3D = useMemo(() => should3DRender(), []);

  if (!use3D) {
    return <>{fallback2D}</>;
  }

  const count = participants.length;
  const stageLayout = useMemo(() => {
    if (count <= 1) {
      return {
        positions: [[0, -2.36, 0]] as [number, number, number][],
        baseScale: 2.9,
        cameraY: 0.3,
        cameraZ: 15.2,
        cameraFov: 46.0,
        focusY: -1.54,
        labelLefts: [50],
      };
    }

    if (count === 2) {
      return {
        positions: [[-4.82, -2.24, 0], [4.82, -2.24, 0]] as [number, number, number][],
        baseScale: 2.52,
        cameraY: 0.3,
        cameraZ: 15.5,
        cameraFov: 46.4,
        focusY: -1.88,
        labelLefts: [28, 72],
      };
    }

    if (count === 3) {
      return {
        positions: [[-5.45, -2.24, 0], [0, -2.05, 0], [5.45, -2.24, 0]] as [number, number, number][],
        baseScale: 2.24,
        cameraY: 0.36,
        cameraZ: 16.1,
        cameraFov: 47.6,
        focusY: -1.86,
        labelLefts: [20, 50, 80],
      };
    }

    const horizontalRange = Math.min(14.6, 3.15 * (count - 1));
    const step = count > 1 ? horizontalRange / (count - 1) : 0;
    const positions = participants.map((_, i) => (
      [(-horizontalRange / 2) + (i * step), -1.9, 0] as [number, number, number]
    ));

    return {
      positions,
      baseScale: Math.max(1.85, 1.96 - ((count - 4) * 0.04)),
      cameraY: 0.4,
      cameraZ: Math.min(19.0, 16.0 + ((count - 4) * 0.32)),
      cameraFov: Math.min(53.4, 48.1 + ((count - 4) * 0.36)),
      focusY: -1.82,
      labelLefts: null,
    };
  }, [count, participants]);

  const positions = stageLayout.positions;
  const labelHalfWidth = Math.tan((stageLayout.cameraFov / 2) * Math.PI / 180) * stageLayout.cameraZ;

  return (
    <>
      {/* Hidden LiveKit hook extractors (outside Canvas) */}
      {participants.map((p) => (
        <AgentStateExtractor
          key={p.identity}
          participant={p}
          onUpdate={handleStateUpdate}
        />
      ))}

      <div className="agent-3d-stage">
        <div className="agent-3d-stage-chrome">
          <span className="agent-3d-stage-kicker">Live Stage</span>
          <span className="agent-3d-stage-summary">
            {participants.length} active
            {offlineAgents.length > 0 ? ` · ${offlineAgents.length} offline` : ''}
          </span>
        </div>
        <Canvas
          className="agent-3d-canvas"
          camera={{ position: [0, stageLayout.cameraY, stageLayout.cameraZ], fov: stageLayout.cameraFov }}
          gl={{
            antialias: true,
            alpha: true,
            powerPreference: 'high-performance',
          }}
          dpr={Math.min(window.devicePixelRatio, 2)}
        >
          <StageCameraRig focusY={stageLayout.focusY} />
          <fog attach="fog" args={['#050913', 16, 28]} />

          {/* Stage environment */}
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -3.08, -0.8]}>
            <circleGeometry args={[18, 96]} />
            <meshStandardMaterial
              color="#050913"
              emissive="#0b1726"
              emissiveIntensity={0.24}
              transparent
              opacity={0.96}
            />
          </mesh>
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -3.03, -0.8]}>
            <ringGeometry args={[6.8, 12.8, 96]} />
            <meshBasicMaterial color="#12375a" transparent opacity={0.14} />
          </mesh>
          <mesh position={[0, 2.1, -7.5]}>
            <planeGeometry args={[24, 13]} />
            <meshBasicMaterial color="#09101c" transparent opacity={0.42} />
          </mesh>

          {/* Lighting */}
          <ambientLight intensity={0.64} />
          <hemisphereLight
            args={['#d7ecff', '#060914', 0.55]}
            position={[0, 8, 0]}
          />
          <spotLight
            position={[0, 8, 7]}
            angle={0.42}
            penumbra={0.75}
            intensity={1.05}
            color="#ffffff"
          />
          {participants.map((p, i) => {
            const name = p.name || p.identity;
            const color = getAgentColor(name);
            const state = agentStates[p.identity];
            const snapshot = agentSnapshots[name];
            const displayState = deriveAgentDisplayState({
              connected: true,
              isSpeaking: state?.isSpeaking || snapshot?.isSpeaking || false,
              agentState: state?.agentState || snapshot?.agentState || 'idle',
              agentActivity: state?.agentActivity || snapshot?.agentActivity || 'idle',
            });
            const accentColor = displayState === 'error'
              ? '#ff4466'
              : displayState === 'listening'
                ? '#39ffb6'
                : displayState === 'thinking'
                  ? '#ffb432'
                  : displayState === 'vision_processing'
                    ? '#c084fc'
                    : color;
            const keyIntensity = displayState === 'speaking'
              ? 0.72
              : displayState === 'thinking' || displayState === 'calling_openclaw' || displayState === 'vision_processing'
                ? 0.5
                : displayState === 'listening'
                  ? 0.42
                  : displayState === 'error'
                    ? 0.36
                    : 0.26;
            const pos = positions[i];
            return (
              <group key={`lights-${p.identity}`}>
                <mesh rotation={[-Math.PI / 2, 0, 0]} position={[pos[0], -2.88, 0]}>
                  <ringGeometry args={[1.15, 1.9, 64]} />
                  <meshBasicMaterial
                    color={accentColor}
                    transparent
                    opacity={
                      displayState === 'speaking'
                        ? 0.42
                        : displayState === 'thinking' || displayState === 'calling_openclaw' || displayState === 'vision_processing'
                          ? 0.32
                          : displayState === 'listening'
                            ? 0.24
                            : 0.16
                    }
                  />
                </mesh>
                <pointLight
                  position={[pos[0] + 2, 4, 6]}
                  intensity={0.48}
                  color="#fff5e6"
                />
                <pointLight
                  position={[pos[0] - 2, -1, 4]}
                  intensity={keyIntensity}
                  color={accentColor}
                />
                <pointLight
                  position={[pos[0], 1.2, 2.8]}
                  intensity={keyIntensity * 0.35}
                  distance={8}
                  color={accentColor}
                />
                <spotLight
                  position={[pos[0], 5.4, 4.6]}
                  angle={0.33}
                  penumbra={0.85}
                  intensity={displayState === 'speaking' ? 0.8 : 0.52}
                  color={accentColor}
                />
              </group>
            );
          })}

          {/* Robot models */}
          {participants.map((p, i) => {
            const name = p.name || p.identity;
            const color = getAgentColor(name);
            const snapshot = agentSnapshots[name];
            const state = agentStates[p.identity] || {
              isSpeaking: snapshot?.isSpeaking || false,
              agentState: snapshot?.agentState || 'idle',
              agentActivity: snapshot?.agentActivity || 'idle',
              displayName: snapshot?.name || name,
              statusText: snapshot?.statusText || '',
              errorText: snapshot?.errorText || '',
              lastActivityAt: snapshot?.lastActivityAt || null,
            };
            return (
              <RobotModel
                key={p.identity}
                position={positions[i]}
                baseScale={stageLayout.baseScale}
                agentColor={color}
                agentState={state}
              />
            );
          })}

          <Environment preset="night" />
        </Canvas>

        {/* HTML overlay — agent labels positioned to match 3D models */}
        {participants.map((p, i) => {
          const name = p.name || p.identity;
          const state = agentStates[p.identity];
          const snapshot = agentSnapshots[name];
          const agentCls = getAgentClass(name);
          const displayState = deriveAgentDisplayState({
            connected: true,
            isSpeaking: state?.isSpeaking || snapshot?.isSpeaking || false,
            agentState: state?.agentState || snapshot?.agentState || 'idle',
            agentActivity: state?.agentActivity || snapshot?.agentActivity || 'idle',
          });
          const detailText = state?.errorText || state?.statusText || snapshot?.errorText || snapshot?.statusText || 'Awaiting next input';
          const metaText = `active ${formatRelativeTime(state?.lastActivityAt || snapshot?.lastActivityAt || null, now)}`;
          const leftPct = stageLayout.labelLefts?.[i] ?? ((positions[i][0] / labelHalfWidth) * 50 + 50);
          return (
            <div
              key={p.identity}
              className="agent-3d-label"
              style={{ left: `${leftPct}%` }}
            >
              <span className={`agent-3d-name ${agentCls}`}>{name}</span>
              <span className={`agent-3d-state ${displayState}`}>
                {formatAgentStateLabel(displayState)}
              </span>
              <span
                className={`agent-3d-detail ${state?.errorText || snapshot?.errorText ? 'error' : ''}`}
                title={detailText}
              >
                {detailText}
              </span>
              <span className="agent-3d-meta">{metaText}</span>
              <div className="agent-3d-actions">
                <button
                  title={`Kick ${name}`}
                  onClick={() => onKick(p.identity, name)}
                >
                  X
                </button>
                <button
                  className="restart"
                  title={`Restart ${name}`}
                  onClick={() => onRestart(p.identity, name)}
                >
                  &#x21bb;
                </button>
              </div>
            </div>
          );
        })}

        {/* Offline agent cards */}
        {offlineAgents.length > 0 && (
          <div className="agent-3d-offline">
            {offlineAgents.map((name) => (
              <div key={name} className="agent-3d-offline-card">
                <div className="agent-3d-offline-copy">
                  <span className="agent-3d-offline-name">{name}</span>
                  <span className="agent-3d-offline-state">
                    last seen {formatRelativeTime(agentSnapshots[name]?.lastActivityAt || null, now)}
                  </span>
                  <span
                    className={`agent-3d-offline-detail ${agentSnapshots[name]?.errorText ? 'error' : ''}`}
                    title={agentSnapshots[name]?.errorText || agentSnapshots[name]?.statusText || 'Offline'}
                  >
                    {agentSnapshots[name]?.errorText || agentSnapshots[name]?.statusText || 'Offline'}
                  </span>
                </div>
                <button
                  onClick={() => onCallAgent(name)}
                  disabled={dispatchingAgents.has(name)}
                >
                  {dispatchingAgents.has(name) ? '...' : 'Call'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

// Preload the model
useGLTF.preload('/models/model.glb');
