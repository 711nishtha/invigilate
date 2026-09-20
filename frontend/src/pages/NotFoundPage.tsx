import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="card max-w-md p-8 text-center">
        <p className="text-sm font-medium text-brand-600">404</p>
        <h1 className="mt-2 text-xl font-semibold text-slate-900">Page not found</h1>
        <p className="mt-2 text-sm text-slate-600">
          If you were given a quiz link, check it with your teacher — quiz codes are short and
          case-sensitive.
        </p>
        <Link to="/" className="btn-secondary mt-6">
          Back to start
        </Link>
      </div>
    </div>
  )
}
