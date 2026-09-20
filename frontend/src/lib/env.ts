/**
 * Frontend configuration.
 *
 * Everything here is PUBLIC: Vite inlines `VITE_*` values into the bundle that
 * any visitor can download. A service-role key, a Gemini key or a Resend key
 * must never be read here — those live only in the backend environment.
 */

interface PublicEnv {
  apiUrl: string
  supabaseUrl: string
  supabaseAnonKey: string
}

function required(name: string, value: string | undefined): string {
  if (!value) {
    // Failing loudly at boot beats a blank screen and a 401 later.
    throw new Error(
      `Missing ${name}. Copy .env.example to .env and fill in the public Supabase values.`,
    )
  }
  return value
}

export const env: PublicEnv = {
  apiUrl: (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/\/$/, ''),
  supabaseUrl: required('VITE_SUPABASE_URL', import.meta.env.VITE_SUPABASE_URL),
  supabaseAnonKey: required('VITE_SUPABASE_ANON_KEY', import.meta.env.VITE_SUPABASE_ANON_KEY),
}
