/**
 * Browser integrity signal reporting.
 *
 * What this does: notices a handful of browser-level events and tells the
 * server about them.
 *
 * What this does not do: prove anything. These are signals, not evidence. A
 * browser cannot see the operating system, a second device, or the room, and a
 * student with DevTools can stop this file running at all. The server treats
 * the reports as untrusted input, assigns its own severity, keeps its own
 * count, and makes its own decision — this hook cannot understate a violation
 * or talk the server out of an auto-submit.
 *
 * Deliberately absent: any DevTools "detection". Reliable detection does not
 * exist, and a fake one would give an invigilator false confidence.
 */

import { useCallback, useEffect, useRef } from 'react'

import { api, type IntegrityEventType } from '../lib/api'

interface Options {
  attemptId: string
  attemptToken: string
  /** Stop reporting once the attempt is finished. */
  active: boolean
  /** Called when the server says it has auto-submitted the attempt. */
  onAutoSubmit: () => void
  /** Called with the server's official count after each accepted report. */
  onViolation: (total: number, max: number) => void
}

// Signals are deduplicated within this window. A single alt-tab can fire blur
// and visibilitychange together, and charging a student twice for one action
// would be unfair rather than strict.
const DEDUPE_MS = 1000

export function useIntegrityMonitor({
  attemptId,
  attemptToken,
  active,
  onAutoSubmit,
  onViolation,
}: Options) {
  const lastSent = useRef<Map<string, number>>(new Map())
  const activeRef = useRef(active)
  activeRef.current = active

  const report = useCallback(
    async (eventType: IntegrityEventType, metadata?: Record<string, unknown>) => {
      if (!activeRef.current) return

      const now = Date.now()
      const previous = lastSent.current.get(eventType) ?? 0
      if (now - previous < DEDUPE_MS) return
      lastSent.current.set(eventType, now)

      try {
        const result = await api.reportIntegrityEvent(attemptId, attemptToken, {
          event_type: eventType,
          // Recorded for forensics only. The server stamps its own receipt
          // time and orders the timeline by that.
          client_reported_at: new Date().toISOString(),
          metadata,
        })
        onViolation(result.violation_total, result.max_integrity_violations)
        if (result.auto_submitted) onAutoSubmit()
      } catch {
        // A failed report must never break the exam. The student keeps
        // working; the server simply has one fewer signal.
      }
    },
    [attemptId, attemptToken, onAutoSubmit, onViolation],
  )

  useEffect(() => {
    if (!active) return

    const onVisibility = () =>
      void report(document.hidden ? 'TAB_HIDDEN' : 'TAB_VISIBLE')
    const onBlur = () => void report('WINDOW_BLUR')
    const onFocus = () => void report('WINDOW_FOCUS')
    const onFullscreen = () =>
      void report(document.fullscreenElement ? 'FULLSCREEN_ENTER' : 'FULLSCREEN_EXIT')
    const onCopy = (event: Event) => {
      event.preventDefault()
      void report('COPY_ATTEMPT')
    }
    const onCut = (event: Event) => {
      event.preventDefault()
      void report('CUT_ATTEMPT')
    }
    const onPaste = (event: Event) => {
      event.preventDefault()
      void report('PASTE_ATTEMPT')
    }
    const onContextMenu = (event: Event) => {
      event.preventDefault()
      void report('RIGHT_CLICK')
    }
    const onKeyDown = (event: KeyboardEvent) => {
      // Clipboard, print, view-source, save. Blocking these is a deterrent,
      // not a boundary: a screenshot or a phone camera sits outside the browser.
      const combo = (event.ctrlKey || event.metaKey) && ['c', 'v', 'x', 'p', 'u', 's'].includes(event.key.toLowerCase())
      if (combo) {
        event.preventDefault()
        void report('KEYBOARD_SHORTCUT_ATTEMPT', { key: event.key })
      }
    }
    // A dropped connection is not misconduct, and the server records it with
    // zero severity. It is reported so the timeline explains a quiet gap.
    const onOffline = () => void report('NETWORK_DISCONNECTED')
    const onOnline = () => void report('NETWORK_RECONNECTED')

    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
    document.addEventListener('fullscreenchange', onFullscreen)
    document.addEventListener('copy', onCopy)
    document.addEventListener('cut', onCut)
    document.addEventListener('paste', onPaste)
    document.addEventListener('contextmenu', onContextMenu)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('offline', onOffline)
    window.addEventListener('online', onOnline)

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('fullscreenchange', onFullscreen)
      document.removeEventListener('copy', onCopy)
      document.removeEventListener('cut', onCut)
      document.removeEventListener('paste', onPaste)
      document.removeEventListener('contextmenu', onContextMenu)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('offline', onOffline)
      window.removeEventListener('online', onOnline)
    }
  }, [active, report])

  return { report }
}

export async function requestFullscreen(): Promise<boolean> {
  try {
    await document.documentElement.requestFullscreen()
    return true
  } catch {
    // The browser may refuse without a user gesture, and the student may
    // decline. Neither is fatal: the exit is recorded as a signal either way.
    return false
  }
}
