/**
 * Attempt token storage.
 *
 * The token is shown exactly once, at attempt creation, so it has to survive a
 * refresh or the student loses access to their own running exam. It lives in
 * sessionStorage: scoped to this tab, cleared when the tab closes, and never
 * sent anywhere except as the `X-Attempt-Token` header.
 *
 * This is convenience storage, not authority. Nothing here decides anything —
 * the server validates the token on every request, and an absent or forged
 * value simply produces a 404. Deleting it locally does not end the attempt;
 * the server's clock and state are untouched by whatever the browser does.
 */

const PREFIX = 'sq.attempt.'

function key(attemptId: string): string {
  return `${PREFIX}${attemptId}`
}

export function rememberAttemptToken(attemptId: string, token: string): void {
  try {
    sessionStorage.setItem(key(attemptId), token)
  } catch {
    // Private mode or blocked storage. The exam still works for as long as the
    // page is not reloaded, so this is not worth failing over.
  }
}

export function recallAttemptToken(attemptId: string): string | null {
  try {
    return sessionStorage.getItem(key(attemptId))
  } catch {
    return null
  }
}

export function forgetAttemptToken(attemptId: string): void {
  try {
    sessionStorage.removeItem(key(attemptId))
  } catch {
    /* nothing to clean up */
  }
}
