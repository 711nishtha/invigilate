import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test } from '@playwright/test'

/**
 * Inspect the built bundle for proctoring claims it cannot support.
 *
 * These are unusual tests, and deliberate. The easiest way to break a
 * proctoring product is not a bug — it is a sentence. A contributor who adds
 * "cheating is impossible" to a reassuring paragraph, or wires a fake detector
 * so a demo shows green, damages the product's honesty without failing a single
 * behavioural assertion.
 *
 * So the honesty is asserted against what actually ships. Like
 * bundle-leakage.spec.ts, these read `dist/` rather than source, because what
 * reaches a student's browser is the only thing that matters.
 *
 * Run `npm run build` first.
 */

const DIST = join(process.cwd(), 'dist')

function bundleFiles(): string[] {
  const assets = join(DIST, 'assets')
  return readdirSync(assets)
    .filter((name) => name.endsWith('.js') || name.endsWith('.css'))
    .map((name) => join(assets, name))
}

function scan(needles: string[]): string[] {
  const offenders: string[] = []
  for (const file of bundleFiles()) {
    const code = readFileSync(file, 'utf8').toLowerCase()
    for (const needle of needles) {
      if (code.includes(needle.toLowerCase())) offenders.push(`${file}: ${needle}`)
    }
  }
  return offenders
}

test.describe('proctoring claims in the shipped bundle', () => {
  test('never tells a student that cheating is impossible', () => {
    expect(
      scan([
        'cheating is impossible',
        'impossible to cheat',
        'you cannot cheat',
        'cheat-proof',
        'tamper-proof',
        '100% secure',
        'fully secure',
        'foolproof',
        'guarantees integrity',
      ]),
    ).toEqual([])
  })

  test('never claims the browser locks down the computer', () => {
    // "Lockdown" is reserved for OS-level control, which needs a desktop client
    // that does not exist. A browser claiming it would be the single most
    // misleading string this product could ship.
    expect(
      scan([
        'locks down your computer',
        'locks your computer',
        'lockdown mode is active',
        'your computer is locked',
        'all other applications are blocked',
        'we can see your screen at all times',
      ]),
    ).toEqual([])
  })

  test('never claims to detect things no browser can detect', () => {
    expect(
      scan([
        'we will detect your phone',
        'second device detected',
        'we can see the room',
        'virtual machine detected',
        'second monitor detected',
      ]),
    ).toEqual([])
  })

  test('ships no proctoring verdict vocabulary', () => {
    // The server has no field for these and the review UI has no control for
    // them. A string here would mean some screen invented one.
    expect(
      scan(['cheating_detected', 'is_cheating', 'suspicion_score', 'cheatscore', 'guilt']),
    ).toEqual([])
  })

  test('ships the honest statements it is supposed to', () => {
    // The inverse check: the limitation text must actually be present, so the
    // caveats cannot be quietly deleted to tidy up the UI.
    //
    // Only strings the *frontend* owns are asserted here. The reviewer-facing
    // caveats ("a browser cannot see the room") come from the server at
    // runtime, so they are covered by the backend tests instead.
    //
    // Asserted as booleans rather than with toContain, because a failing
    // toContain against a 500 KB bundle prints the whole bundle.
    const code = bundleFiles()
      .map((file) => readFileSync(file, 'utf8'))
      .join('\n')
      .toLowerCase()

    for (const phrase of [
      // The student is told the clock is not running during the checks.
      'the timer begins only',
      // Monitoring failure is surfaced to the student, not hidden.
      'monitoring is not working',
      // The limits section on the precheck screen.
      'what this monitoring cannot do',
      // A signal is not a finding.
      'a signal is not a finding',
    ]) {
      expect(code.includes(phrase), `missing required honest statement: ${phrase}`).toBe(true)
    }
  })

  test('ships no bundled face or object detection model', () => {
    // A multi-megabyte model would mean someone added a detector without
    // revisiting the capability reporting, and every student would pay for it
    // in battery and framerate.
    const assets = join(DIST, 'assets')
    const heavy = readdirSync(assets).filter((name) => {
      const size = readFileSync(join(assets, name)).byteLength
      return size > 2_000_000
    })
    expect(heavy, `unexpectedly large assets: ${heavy.join(', ')}`).toEqual([])
  })

  test('declares object detection as unsupported rather than pretending', () => {
    const code = bundleFiles()
      .map((file) => readFileSync(file, 'utf8'))
      .join('\n')
    // The capability is reported, and it is reported false. If a detector is
    // ever added, this test should be updated deliberately rather than
    // silently passing because the string moved.
    expect(code).toContain('object_detection')
  })
})

test.describe('deployment headers permit what proctoring needs', () => {
  /**
   * vercel.json's Permissions-Policy is the one piece of deployment config that
   * can disable a proctoring feature without failing a single test or showing an
   * error anywhere useful. A policy of `microphone=()` blocks getUserMedia for
   * audio at the browser level, so a quiz with require_microphone would fail its
   * precheck for every student with no indication why.
   */
  const policy = (): string => {
    const config = JSON.parse(
      readFileSync(join(process.cwd(), 'vercel.json'), 'utf8'),
    ) as { headers: Array<{ headers: Array<{ key: string; value: string }> }> }
    const header = config.headers
      .flatMap((entry) => entry.headers)
      .find((h) => h.key.toLowerCase() === 'permissions-policy')
    return header?.value ?? ''
  }

  for (const feature of ['camera', 'microphone', 'display-capture']) {
    test(`allows ${feature} for this origin`, () => {
      // `feature=(self)` permits it; `feature=()` blocks it for everyone.
      expect(policy()).toContain(`${feature}=(self)`)
    })
  }

  test('still denies features the product never uses', () => {
    for (const feature of ['geolocation', 'payment', 'usb']) {
      expect(policy()).toContain(`${feature}=()`)
    }
  })
})
