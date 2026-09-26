/**
 * The proctoring loop that runs during a proctored exam.
 *
 * Two independent timers:
 *
 * - a **face loop** at roughly 1 Hz, which watches for a sustained absence
 *   rather than a single missed frame;
 * - a **heartbeat** at the interval the *server* specified, which reports
 *   observed stream state.
 *
 * Both only ever report. Severity, the running total, and the decision to end
 * an exam belong to the server, and the server also re-checks for silence on
 * every other request, so stopping this loop does not hide anything.
 *
 * ## Why the grace window exists on this side too
 *
 * The server debounces repeated signals, but sending a FACE_MISSING every
 * second while a student reaches for a glass of water would be noise in an
 * audit trail that a human has to read. So the loop requires the absence to
 * persist for the grace period the server published before it reports
 * anything at all. The server's debounce is the guarantee; this is the
 * courtesy.
 *
 * ## Unknown is not absent
 *
 * `faceCount === null` means the browser could not tell us — the detector is
 * missing, the video is not ready, or `detect()` threw. That is reported as
 * `monitoring_impaired`, which costs no severity and marks the attempt
 * DEGRADED. Reporting it as a missing face would manufacture violations out of
 * a browser limitation and put them on a student's record.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type {
  HeartbeatResult,
  IntegrityEventType,
  PrecheckRequirements,
  ProctoringStatus,
} from '../lib/api'
import { api } from '../lib/api'
import {
  FacePresenceDetector,
  captureStill,
  isFullscreen,
  isLive,
} from '../lib/proctoring'

export interface MonitorStreams {
  camera: MediaStream | null
  microphone: MediaStream | null
  screen: MediaStream | null
}

export interface MonitorState {
  proctoringStatus: ProctoringStatus
  violationTotal: number
  maxViolations: number
  warnings: string[]
  autoSubmitted: boolean
  /** True when the browser cannot currently monitor. Shown to the student. */
  impaired: boolean
  impairedReason: string | null
  lastBeatAt: number | null
}

interface Options {
  attemptId: string
  attemptToken: string
  requirements: PrecheckRequirements | null
  streams: MonitorStreams
  videoRef: React.RefObject<HTMLVideoElement | null>
  enabled: boolean
  onAutoSubmitted: () => void
}

const FACE_POLL_MS = 1000

export function useProctoringMonitor({
  attemptId,
  attemptToken,
  requirements,
  streams,
  videoRef,
  enabled,
  onAutoSubmitted,
}: Options): MonitorState {
  const [state, setState] = useState<MonitorState>({
    proctoringStatus: 'NOT_REQUIRED',
    violationTotal: 0,
    maxViolations: 0,
    warnings: [],
    autoSubmitted: false,
    impaired: false,
    impairedReason: null,
    lastBeatAt: null,
  })

  const detectorRef = useRef<FacePresenceDetector | null>(null)
  // When the current absence started, in ms. Null while a face is present.
  const absentSinceRef = useRef<number | null>(null)
  const reportedAbsenceRef = useRef(false)
  const impairedRef = useRef(false)
  const impairedReasonRef = useRef<string | null>(null)
  // Kept in a ref so the heartbeat effect does not re-subscribe on every frame.
  const streamsRef = useRef(streams)
  streamsRef.current = streams

  if (detectorRef.current === null) detectorRef.current = new FacePresenceDetector()

  const applyResult = useCallback(
    (result: HeartbeatResult) => {
      setState((previous) => ({
        ...previous,
        proctoringStatus: result.proctoring_status,
        violationTotal: result.violation_total,
        maxViolations: result.max_integrity_violations,
        warnings: result.warnings,
        autoSubmitted: result.auto_submitted,
        lastBeatAt: Date.now(),
      }))
      if (result.auto_submitted) onAutoSubmitted()
    },
    [onAutoSubmitted],
  )

  /** Report one signal. Failures are swallowed: an exam is not a network test. */
  const reportEvent = useCallback(
    async (eventType: IntegrityEventType, metadata?: Record<string, unknown>) => {
      try {
        await api.reportIntegrityEvent(attemptId, attemptToken, {
          event_type: eventType,
          metadata,
        })
      } catch {
        /* The server also notices silence on its own; see check_heartbeat_staleness. */
      }
    },
    [attemptId, attemptToken],
  )

  // --- Face presence ---------------------------------------------------------
  useEffect(() => {
    if (!enabled || !requirements?.face_presence) return
    const graceMs = (requirements.face_missing_grace_seconds || 15) * 1000
    let cancelled = false

    const tick = async () => {
      const video = videoRef.current
      const detector = detectorRef.current
      if (cancelled || !video || !detector) return

      const reading = await detector.read(video)
      if (cancelled) return

      if (reading.faceCount === null) {
        // Unknown, not absent. Report impairment once and stop guessing.
        if (!impairedRef.current) {
          impairedRef.current = true
          impairedReasonRef.current = reading.supported
            ? 'The face detector stopped responding.'
            : 'This browser cannot detect faces.'
          setState((previous) => ({
            ...previous,
            impaired: true,
            impairedReason: impairedReasonRef.current,
          }))
        }
        return
      }

      if (impairedRef.current) {
        impairedRef.current = false
        impairedReasonRef.current = null
        setState((previous) => ({ ...previous, impaired: false, impairedReason: null }))
      }

      if (reading.faceCount > 1 && requirements.multiple_face_detection) {
        // A second person in frame is the strongest signal a webcam can give,
        // and it is still only a signal: rooms are shared, people walk past.
        await reportEvent('MULTIPLE_FACES_DETECTED', { faces: reading.faceCount })
        const still = await captureStill(video)
        if (still) await api.uploadEvidence(attemptId, attemptToken, 'EVENT_SNAPSHOT', still)
        return
      }

      if (reading.faceCount === 0) {
        const now = Date.now()
        if (absentSinceRef.current === null) {
          absentSinceRef.current = now
          return
        }
        // Only once the absence has lasted the published grace period.
        if (now - absentSinceRef.current >= graceMs && !reportedAbsenceRef.current) {
          reportedAbsenceRef.current = true
          await reportEvent('FACE_MISSING', {
            absent_seconds: Math.round((now - absentSinceRef.current) / 1000),
          })
          const still = await captureStill(video)
          if (still) await api.uploadEvidence(attemptId, attemptToken, 'EVENT_SNAPSHOT', still)
        }
        return
      }

      // A face is back.
      if (reportedAbsenceRef.current) {
        reportedAbsenceRef.current = false
        await reportEvent('FACE_PRESENT')
      }
      absentSinceRef.current = null
    }

    const timer = window.setInterval(() => {
      void tick()
    }, FACE_POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [enabled, requirements, videoRef, reportEvent, attemptId, attemptToken])

  // --- Heartbeat -------------------------------------------------------------
  useEffect(() => {
    if (!enabled || !requirements) return
    // The server sets the cadence; the client does not get to slow it down.
    const intervalMs = Math.max(10, requirements.heartbeat_interval_seconds || 30) * 1000

    const beat = async () => {
      const current = streamsRef.current
      try {
        const result = await api.proctoringHeartbeat(attemptId, attemptToken, {
          camera_active: isLive(current.camera),
          microphone_active: isLive(current.microphone),
          screen_share_active: isLive(current.screen),
          fullscreen_active: isFullscreen(),
          monitoring_impaired: impairedRef.current,
          impaired_reason: impairedReasonRef.current,
        })
        applyResult(result)
      } catch {
        // A dropped heartbeat is a network problem, not misconduct. The server
        // notices the silence on the next request either way.
      }
    }

    void beat()
    const timer = window.setInterval(() => {
      void beat()
    }, intervalMs)
    return () => window.clearInterval(timer)
  }, [enabled, requirements, attemptId, attemptToken, applyResult])

  // --- Track-level stream loss ----------------------------------------------
  // A user can revoke camera access or click "Stop sharing" mid-exam. The
  // `ended` event fires immediately, which is faster and more accurate than
  // waiting for the next heartbeat to notice a dead track.
  useEffect(() => {
    if (!enabled) return
    const cleanups: Array<() => void> = []

    const watch = (stream: MediaStream | null, eventType: IntegrityEventType) => {
      stream?.getTracks().forEach((track) => {
        const handler = () => void reportEvent(eventType, { track: track.kind })
        track.addEventListener('ended', handler)
        cleanups.push(() => track.removeEventListener('ended', handler))
      })
    }

    watch(streams.camera, 'CAMERA_STOPPED')
    watch(streams.microphone, 'MICROPHONE_STOPPED')
    watch(streams.screen, 'SCREEN_SHARE_STOPPED')

    return () => cleanups.forEach((off) => off())
  }, [enabled, streams, reportEvent])

  // --- Device changes --------------------------------------------------------
  useEffect(() => {
    if (!enabled || !requirements) return
    const media = navigator.mediaDevices
    if (!media?.addEventListener) return
    const handler = () => void reportEvent('DEVICE_CHANGED')
    media.addEventListener('devicechange', handler)
    return () => media.removeEventListener('devicechange', handler)
  }, [enabled, requirements, reportEvent])

  return state
}
