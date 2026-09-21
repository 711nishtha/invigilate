import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { expect, test } from '@playwright/test'

/**
 * Inspect the built production bundle for answer keys or grading logic.
 *
 * The student exam runner ships to an untrusted device. If a correct answer or
 * a scoring rule were ever bundled, a student with DevTools would simply read
 * it. These read dist/ directly, so they check what is actually shipped rather
 * than what the source appears to do.
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
    const code = readFileSync(file, 'utf8')
    for (const needle of needles) {
      if (code.includes(needle)) offenders.push(`${file}: ${needle}`)
    }
  }
  return offenders
}

test.describe('production bundle', () => {
  test('contains no server-internal field names', () => {
    // None of these has any legitimate client use. Any appearing means some
    // screen is reading data it should never have been sent.
    const offenders = scan([
      'correct_answer',
      'options_order_json',
      'content_hash',
      'validation_errors_json',
      'approved_by_id',
      'attempt_token_hash',
    ])
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('answer-key field names are referenced only by teacher screens', () => {
    // `correct_option`, `numeric_answer_json` and `generation_config_json` DO
    // appear in the bundle, and that is correct: the teacher variant-review
    // screen renders the key for the quiz owner, which is its whole purpose.
    // Those are property names in code, not answer data, and the API returns
    // the fields only to an authenticated owner.
    //
    // What must stay true is that no student-facing screen reads them. This
    // checks the source, because bundling erases the boundary the rule is about.
    const studentDirs = [
      join(process.cwd(), 'src', 'pages', 'student'),
      join(process.cwd(), 'src', 'hooks'),
    ]
    const forbidden = [
      'correct_option',
      'numeric_answer_json',
      'generation_config_json',
      // Not sensitive — it is a UUID saying a variant was replaced — but the
      // teacher review screen is the only place with any reason to read it.
      'superseded_by_id',
    ]
    const offenders: string[] = []
    for (const dir of studentDirs) {
      for (const name of readdirSync(dir)) {
        const code = readFileSync(join(dir, name), 'utf8')
        for (const needle of forbidden) {
          if (code.includes(needle)) offenders.push(`${dir}/${name}: ${needle}`)
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('contains no baked-in answer data', () => {
    // A key could leak as data rather than as a field name, so look for the
    // shapes an embedded key would take.
    const patterns = [/"correct_option"\s*:\s*\d/, /correctOption\s*[:=]\s*\d/, /answerKey/i]
    const offenders: string[] = []
    for (const file of bundleFiles()) {
      const code = readFileSync(file, 'utf8')
      for (const pattern of patterns) {
        if (pattern.test(code)) offenders.push(`${file}: ${pattern}`)
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('contains no grading or scoring logic', () => {
    // The client must never be able to compute a score. Marking lives in
    // app/services/grading.py and nowhere else.
    const offenders = scan([
      'isCorrect',
      'is_correct',
      'calculateScore',
      'computeScore',
      'gradeAnswer',
    ])
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  test('contains no backend secret names', () => {
    const offenders = scan([
      'SERVICE_ROLE',
      'GEMINI_API_KEY',
      'RESEND_API_KEY',
      'SUPABASE_JWT_SECRET',
      'DATABASE_URL',
      'generativelanguage.googleapis.com',
    ])
    expect(offenders, offenders.join('\n')).toEqual([])
  })
})
