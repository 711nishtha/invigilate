/**
 * Browser-side proctoring: capability detection, media acquisition, and the
 * face-presence loop.
 *
 * Everything here produces *observations* that are sent to the server. Nothing
 * here decides anything. The server owns the policy, the severity, the counter
 * and the decision to start or end an exam — this module's only job is to look
 * at the browser honestly and report what it sees.
 *
 * ## Capability detection is the important part
 *
 * A proctored exam has requirements. A browser either meets them or it does
 * not, and the wrong answer to "it does not" is to run a reduced version of the
 * exam anyway. So `detectCapabilities` reports what genuinely exists, the
 * server compares that with the policy, and a browser that falls short is
 * blocked with a reason the student can act on.
 *
 * That matters most for face detection. `FaceDetector` (the Shape Detection
 * API) exists in some Chromium builds and nowhere else — not in Firefox, not in
 * Safari, and not in every Chrome. There is no polyfill here and no bundled
 * model: shipping several megabytes of ML weights into an exam page would cost
 * every student's battery and framerate, and pretending to detect faces without
 * one would be worse. If the API is absent we say so, and the server decides.
 *
 * ## What is deliberately not implemented
 *
 * - **Object / phone detection.** No browser API does this. Implementing it
 *   means shipping a model; the plumbing (event type, severity, evidence,
 *   review) exists so a detector can be added, but no detector is shipped and
 *   the capability reports false.
 * - **Continuous recording.** `MediaRecorder` is widely available and the
 *   capability is reported, but recording is not started. Stills tied to
 *   specific signals are a fraction of the privacy cost of filming a student's
 *   room for an hour.
 * - **Any form of "is this a real browser" detection.** It does not work, and
 *   the attempts that exist mostly detect unusual-but-honest setups.
 */

/** What this browser can actually do. Sent to the server, which gates on it. */
export interface Capabilities {
  camera: boolean
  microphone: boolean
  screen_share: boolean
  fullscreen: boolean
  face_detection: boolean
  object_detection: boolean
  media_recorder: boolean
  secure_context: boolean
  is_mobile: boolean
  browser_name: string
  browser_version: string
  platform: string
}

/** What the browser currently observes. Never a verdict. */
export interface Observations {
  camera_active: boolean
  microphone_active: boolean
  screen_share_active: boolean
  fullscreen_active: boolean
  face_detected: boolean
  multiple_faces: boolean
  consent_accepted: boolean
  monitoring_impaired: boolean
  impaired_reason?: string | null
}

interface FaceDetectorLike {
  detect(source: CanvasImageSource): Promise<unknown[]>
}

type FaceDetectorCtor = new (options?: { fastMode?: boolean; maxDetectedFaces?: number }) => FaceDetectorLike

function faceDetectorCtor(): FaceDetectorCtor | null {
  const candidate = (window as unknown as { FaceDetector?: FaceDetectorCtor }).FaceDetector
  return typeof candidate === 'function' ? candidate : null
}

function browserIdentity(): { name: string; version: string } {
  const ua = navigator.userAgent
  // Order matters: Edge and Opera both contain "Chrome".
  const patterns: Array<[string, RegExp]> = [
    ['Edge', /Edg\/([\d.]+)/],
    ['Opera', /OPR\/([\d.]+)/],
    ['Chrome', /Chrome\/([\d.]+)/],
    ['Firefox', /Firefox\/([\d.]+)/],
    ['Safari', /Version\/([\d.]+).*Safari/],
  ]
  for (const [name, pattern] of patterns) {
    const match = ua.match(pattern)
    if (match) return { name, version: match[1] }
  }
  return { name: 'Unknown', version: '' }
}

function isMobile(): boolean {
  // Coarse by design. It gates a policy that a teacher chose, and a false
  // negative is caught by the requirements that follow; a browser that wants
  // to claim it is a desktop still has to produce a camera and a face.
  const ua = navigator.userAgent
  if (/Android|iPhone|iPad|iPod|Windows Phone|Mobile/i.test(ua)) return true
  const touchPoints = navigator.maxTouchPoints ?? 0
  // iPadOS reports a desktop UA, so the touch count is the tell.
  return /Macintosh/.test(ua) && touchPoints > 1
}

/**
 * Inspect the browser. Reports what exists, not what would be convenient.
 *
 * `getUserMedia` presence is not permission: a camera capability of true means
 * the API is callable, not that the student has granted anything. Permission is
 * established later, by actually acquiring a track.
 */
export function detectCapabilities(): Capabilities {
  const media = navigator.mediaDevices as MediaDevices | undefined
  const identity = browserIdentity()

  return {
    camera: typeof media?.getUserMedia === 'function',
    microphone: typeof media?.getUserMedia === 'function',
    screen_share: typeof media?.getDisplayMedia === 'function',
    fullscreen:
      typeof document.documentElement.requestFullscreen === 'function' &&
      (document.fullscreenEnabled ?? false),
    // Chromium-only, often behind a flag. No polyfill, no bundled model.
    face_detection: faceDetectorCtor() !== null,
    // No browser API provides this, and no model is shipped. See the header.
    object_detection: false,
    media_recorder: typeof window.MediaRecorder === 'function',
    // getUserMedia and getDisplayMedia require a secure context. Without one
    // every check below is meaningless, so it is reported first-class.
    secure_context: window.isSecureContext === true,
    is_mobile: isMobile(),
    browser_name: identity.name,
    browser_version: identity.version,
    platform: navigator.platform || 'unknown',
  }
}

/** A human-readable reason a browser cannot run an exam, for the student. */
export const CAPABILITY_LABELS: Record<string, string> = {
  camera: 'This browser cannot access a camera.',
  microphone: 'This browser cannot access a microphone.',
  screen_share: 'This browser cannot share a screen.',
  fullscreen: 'This browser cannot enter fullscreen.',
  face_detection:
    'This browser does not support on-device face detection. Google Chrome or Microsoft Edge on a desktop computer does.',
  object_detection: 'This browser does not support on-device object detection.',
  media_recorder: 'This browser cannot record media.',
  secure_context: 'The exam must be opened over a secure (https) connection.',
  desktop_browser: 'This exam must be taken on a desktop or laptop computer.',
}

// --- Media acquisition -------------------------------------------------------

export type MediaFailure =
  | 'PERMISSION_DENIED'
  | 'NO_DEVICE'
  | 'DEVICE_BUSY'
  | 'UNSUPPORTED'
  | 'UNKNOWN'

export interface MediaResult {
  stream: MediaStream | null
  failure: MediaFailure | null
}

/**
 * Map a getUserMedia rejection to something a student can act on.
 *
 * "Permission denied" and "no camera attached" need completely different
 * instructions, and collapsing them into "camera error" is how a student ends
 * up unable to sit an exam for a reason nobody can diagnose.
 */
function classify(error: unknown): MediaFailure {
  const name = (error as DOMException | undefined)?.name
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'PERMISSION_DENIED'
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'NO_DEVICE'
    case 'NotReadableError':
    case 'AbortError':
      return 'DEVICE_BUSY'
    case 'TypeError':
      return 'UNSUPPORTED'
    default:
      return 'UNKNOWN'
  }
}

export const MEDIA_FAILURE_LABELS: Record<MediaFailure, string> = {
  PERMISSION_DENIED:
    'Permission was refused. Allow access in your browser’s address bar, then try again.',
  NO_DEVICE: 'No device was found. Check that it is plugged in and enabled.',
  DEVICE_BUSY:
    'The device is in use by another application. Close video calls and other apps, then try again.',
  UNSUPPORTED: 'This browser does not support what this exam requires.',
  UNKNOWN: 'The device could not be started.',
}

export async function requestCamera(): Promise<MediaResult> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      // Modest constraints: a proctoring still does not need 1080p, and asking
      // for more makes acquisition fail on cheap webcams for no benefit.
      video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' },
      audio: false,
    })
    return { stream, failure: null }
  } catch (error) {
    return { stream: null, failure: classify(error) }
  }
}

export async function requestMicrophone(): Promise<MediaResult> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    return { stream, failure: null }
  } catch (error) {
    return { stream: null, failure: classify(error) }
  }
}

export async function requestScreenShare(): Promise<MediaResult> {
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: false,
    })
    return { stream, failure: null }
  } catch (error) {
    return { stream: null, failure: classify(error) }
  }
}

/** True when the stream has at least one track that is actually live. */
export function isLive(stream: MediaStream | null): boolean {
  if (!stream) return false
  return stream.getTracks().some((track) => track.readyState === 'live' && track.enabled)
}

export function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop())
}

// --- Face presence -----------------------------------------------------------

export interface FaceReading {
  /** null when detection is unavailable — which is not the same as "no face". */
  faceCount: number | null
  supported: boolean
}

/**
 * A throttled face-presence detector.
 *
 * Runs at roughly 1 Hz rather than per frame. Faces do not move fast enough to
 * need more, and an exam page competing with a 30 Hz inference loop for the
 * main thread is a worse exam.
 *
 * `faceCount === null` means the browser could not tell us, and the caller must
 * treat that as "unknown" rather than "absent". Reporting an unknown as a
 * missing face would manufacture violations out of a browser limitation.
 */
export class FacePresenceDetector {
  private detector: FaceDetectorLike | null = null
  private canvas: HTMLCanvasElement | null = null
  readonly supported: boolean

  constructor() {
    const Ctor = faceDetectorCtor()
    this.supported = Ctor !== null
    if (Ctor) {
      try {
        this.detector = new Ctor({ fastMode: true, maxDetectedFaces: 3 })
      } catch {
        // Present but unusable — some builds expose the constructor and throw.
        this.detector = null
        this.supported = false
      }
    }
  }

  async read(video: HTMLVideoElement): Promise<FaceReading> {
    if (!this.detector || video.readyState < 2 || video.videoWidth === 0) {
      return { faceCount: null, supported: this.supported }
    }
    try {
      if (!this.canvas) this.canvas = document.createElement('canvas')
      this.canvas.width = video.videoWidth
      this.canvas.height = video.videoHeight
      const context = this.canvas.getContext('2d')
      if (!context) return { faceCount: null, supported: this.supported }
      context.drawImage(video, 0, 0, this.canvas.width, this.canvas.height)
      const faces = await this.detector.detect(this.canvas)
      return { faceCount: faces.length, supported: true }
    } catch {
      // A detector that throws mid-exam is an impairment to report, not a
      // student to accuse.
      return { faceCount: null, supported: this.supported }
    }
  }
}

/**
 * Capture a still from a live video element, for evidence.
 *
 * JPEG at modest quality: this is a frame for a human to glance at, and the
 * upload has a hard size limit on the server.
 */
export async function captureStill(
  video: HTMLVideoElement,
  quality = 0.6,
): Promise<Blob | null> {
  if (video.readyState < 2 || video.videoWidth === 0) return null
  const canvas = document.createElement('canvas')
  canvas.width = video.videoWidth
  canvas.height = video.videoHeight
  const context = canvas.getContext('2d')
  if (!context) return null
  context.drawImage(video, 0, 0, canvas.width, canvas.height)
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), 'image/jpeg', quality)
  })
}

// --- Fullscreen ---------------------------------------------------------------

export function isFullscreen(): boolean {
  return document.fullscreenElement !== null
}

export async function enterFullscreen(): Promise<boolean> {
  try {
    await document.documentElement.requestFullscreen()
    return true
  } catch {
    // Fullscreen needs a user gesture and can be refused outright. The caller
    // shows the student a button rather than retrying in a loop.
    return false
  }
}
