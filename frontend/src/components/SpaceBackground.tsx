import { useEffect, useRef } from 'react';

const WORDS_CYAN = ['AGORA', 'COMMS', 'VOICE', 'AGENT', 'STREAM', 'NODE'];
const WORDS_PURPLE = ['HERMES', 'OPENCLAW', 'ANTHROPIC', 'OPENAI', 'AI', 'LLM'];
const WORDS_BLUE = ['LiveKit', 'WebRTC', 'STT', 'TTS', 'SIP', 'RTC'];
const WORDS_YELLOW = ['async', 'await', '=>', '{}', '()', '[];'];

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  text: string;
  color: string;
  alpha: number;
  size: number;
  targetAlpha: number;
  fadeSpeed: number;
}

function pickRandom<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

function createParticle(w: number, h: number): Particle {
  const category = Math.random();
  let text: string;
  let color: string;

  if (category < 0.3) {
    text = pickRandom(WORDS_CYAN);
    color = '#22d3ee';
  } else if (category < 0.55) {
    text = pickRandom(WORDS_PURPLE);
    color = '#a855f7';
  } else if (category < 0.8) {
    text = pickRandom(WORDS_BLUE);
    color = '#3b82f6';
  } else {
    text = pickRandom(WORDS_YELLOW);
    color = '#fbbf24';
  }

  return {
    x: Math.random() * w,
    y: Math.random() * h,
    vx: (Math.random() - 0.5) * 0.3,
    vy: (Math.random() - 0.5) * 0.2 - 0.1,
    text,
    color,
    alpha: 0,
    size: 9 + Math.random() * 5,
    targetAlpha: 0.06 + Math.random() * 0.1,
    fadeSpeed: 0.002 + Math.random() * 0.003,
  };
}

export function SpaceBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let w = window.innerWidth;
    let h = window.innerHeight;
    canvas.width = w;
    canvas.height = h;

    // Adaptive particle count
    const cores = navigator.hardwareConcurrency ?? 2;
    const count = cores <= 2 ? 30 : cores <= 4 ? 60 : 100;

    const particles: Particle[] = [];
    for (let i = 0; i < count; i++) {
      particles.push(createParticle(w, h));
    }

    let lastTime = 0;
    let fpsAccum = 0;
    let fpsFrames = 0;

    function animate(time: number) {
      const dt = time - lastTime;
      lastTime = time;

      // FPS tracking for degradation
      if (dt > 0) {
        fpsAccum += 1000 / dt;
        fpsFrames++;
        if (fpsFrames >= 60) {
          const avgFps = fpsAccum / fpsFrames;
          // If FPS is too low, remove particles
          if (avgFps < 25 && particles.length > 15) {
            particles.splice(particles.length - 10, 10);
          }
          fpsAccum = 0;
          fpsFrames = 0;
        }
      }

      // Skip if tab is hidden
      if (document.hidden) {
        rafRef.current = requestAnimationFrame(animate);
        return;
      }

      ctx!.clearRect(0, 0, w, h);

      for (const p of particles) {
        // Fade in
        if (p.alpha < p.targetAlpha) {
          p.alpha = Math.min(p.alpha + p.fadeSpeed, p.targetAlpha);
        }

        p.x += p.vx;
        p.y += p.vy;

        // Wrap around
        if (p.x < -50) p.x = w + 50;
        if (p.x > w + 50) p.x = -50;
        if (p.y < -20) p.y = h + 20;
        if (p.y > h + 20) p.y = -20;

        ctx!.globalAlpha = p.alpha;
        ctx!.font = `${p.size}px 'Orbitron', monospace`;
        ctx!.fillStyle = p.color;
        ctx!.fillText(p.text, p.x, p.y);
      }

      ctx!.globalAlpha = 1;
      rafRef.current = requestAnimationFrame(animate);
    }

    rafRef.current = requestAnimationFrame(animate);

    const onResize = () => {
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w;
      canvas.height = h;
    };
    window.addEventListener('resize', onResize);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('resize', onResize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 0,
        pointerEvents: 'none',
      }}
    />
  );
}
