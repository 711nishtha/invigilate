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

// In development a missing VITE_API_URL is a convenience; in a production
// build it is a deployment mistake. Falling back to localhost there would ship
// a bundle that silently calls a machine the visitor does not have, so the
// build-time mode decides whether the fallback is allowed.
function apiUrl(): string {
  const configured = import.meta.env.VITE_API_URL
  if (configured) return configured.replace(/\/$/, '')
  if (import.meta.env.PROD) {
    throw new Error(
      'Missing VITE_API_URL. A production build must be given the backend URL; ' +
        'set it in the Vercel project environment.',
    )
  }
  return 'http://localhost:8000'
}

export const env: PublicEnv = {
  apiUrl: apiUrl(),
  supabaseUrl: required('VITE_SUPABASE_URL', import.meta.env.VITE_SUPABASE_URL),
  supabaseAnonKey: required('VITE_SUPABASE_ANON_KEY', import.meta.env.VITE_SUPABASE_ANON_KEY),
}
