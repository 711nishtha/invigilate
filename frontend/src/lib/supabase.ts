import { createClient } from '@supabase/supabase-js'

import { env } from './env'

/**
 * Supabase client for teacher authentication only.
 *
 * The browser never queries application tables through Supabase — all quiz,
 * attempt and result data goes through the FastAPI backend, which is the only
 * component that can authorize a request. This client exists to sign in, sign
 * out, and hand the resulting JWT to the API.
 */
export const supabase = createClient(env.supabaseUrl, env.supabaseAnonKey, {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
  },
})

export async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession()
  return data.session?.access_token ?? null
}
