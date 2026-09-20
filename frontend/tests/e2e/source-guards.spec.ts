import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test } from '@playwright/test'

/**
 * Source-level guards for two project rules that are easy to break silently.
 *
 * These read the source rather than the rendered page because the rules are
 * about what the code is allowed to do, and because the screens they protect
 * (exam runner, results tables) are built in later phases — the guard needs to
 * be in place *before* the code that could violate it is written.
 */

const SRC = join(process.cwd(), 'src')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) return sourceFiles(full)
    return /\.(ts|tsx)$/.test(entry) ? [full] : []
  })
}

function withoutCommentsOrStrings(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '``')
}

test.describe('project rules enforced in source', () => {
  test('no fabricated statistics, attempts or results in the UI', () => {
    // Arrays of objects carrying student-shaped fields are how fake tables get
    // in. Real data arrives from the API and is typed in src/lib/api.ts.
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      if (file.endsWith(join('lib', 'api.ts'))) continue // type declarations, not data
      const code = withoutCommentsOrStrings(readFileSync(file, 'utf8'))
      if (/\[\s*\{[^}]*\b(studentName|student_email|score|percentage|violationCount)\b/.test(code)) {
        offenders.push(`${file}: literal array of result-shaped objects`)
      }
      if (/\b(mockData|fakeData|sampleAttempts|dummyResults|seedData)\b/.test(code)) {
        offenders.push(`${file}: mock data identifier`)
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('the exam countdown is never driven by the browser clock', () => {
    // The authoritative deadline is attempts.expires_at on the server. The UI
    // may render a countdown, but it must derive from the server_time the API
    // returns, never from the student's own clock.
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      if (!/attempt|exam|timer|countdown/i.test(file)) continue
      const code = withoutCommentsOrStrings(readFileSync(file, 'utf8'))
      for (const [index, line] of code.split('\n').entries()) {
        if (/Date\.now\(\)|new Date\(\s*\)/.test(line)) {
          offenders.push(`${file}:${index + 1}: browser clock in exam timing code`)
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('no service-role or provider secret is referenced in frontend source', () => {
    const forbidden = [
      'SERVICE_ROLE',
      'GEMINI_API_KEY',
      'RESEND_API_KEY',
      'SUPABASE_JWT_SECRET',
      'DATABASE_URL',
    ]
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      const code = readFileSync(file, 'utf8')
      for (const secret of forbidden) {
        // env.ts names them only to warn against them; allow that one mention.
        if (code.includes(secret) && !file.endsWith(join('lib', 'env.ts'))) {
          offenders.push(`${file}: references ${secret}`)
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })
})
