const STORAGE_KEY = 'skynet-3d-preference';

export interface DeviceCapabilities {
  supports3D: boolean;
  hasWebGL: boolean;
  isMobile: boolean;
}

let cached: DeviceCapabilities | null = null;

function testWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas');
    const gl =
      canvas.getContext('webgl2') ||
      canvas.getContext('webgl') ||
      canvas.getContext('experimental-webgl');
    return !!gl;
  } catch {
    return false;
  }
}

function testMobile(): boolean {
  return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
    navigator.userAgent
  );
}

export function detectDeviceCapabilities(): DeviceCapabilities {
  if (cached) return cached;

  const hasWebGL = testWebGL();
  const isMobile = testMobile();
  const cores = navigator.hardwareConcurrency ?? 2;

  // Require WebGL, non-mobile, and at least 4 cores for 3D
  const supports3D = hasWebGL && !isMobile && cores >= 4;

  cached = { supports3D, hasWebGL, isMobile };
  return cached;
}

export function getUser3DPreference(): boolean | null {
  const val = localStorage.getItem(STORAGE_KEY);
  if (val === 'true') return true;
  if (val === 'false') return false;
  return null;
}

export function setUser3DPreference(enabled: boolean): void {
  localStorage.setItem(STORAGE_KEY, String(enabled));
}

/** Returns true if 3D should be used (user pref overrides auto-detect). */
export function should3DRender(): boolean {
  const pref = getUser3DPreference();
  if (pref !== null) return pref;
  return detectDeviceCapabilities().supports3D;
}
