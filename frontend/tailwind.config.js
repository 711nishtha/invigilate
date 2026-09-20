/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Teacher surfaces: a calm, dense SaaS palette.
        brand: {
          50: '#eef4ff',
          100: '#d9e6ff',
          200: '#bcd2ff',
          300: '#8eb3ff',
          400: '#5a8bfa',
          500: '#3665ef',
          600: '#2449d4',
          700: '#1d39ab',
          800: '#1d3387',
          900: '#1d2f6b',
        },
        // Exam-runner status colours. The timer and integrity badge must read
        // instantly, so these are used sparingly and only for status.
        calm: '#0f766e',
        warn: '#b45309',
        danger: '#b91c1c',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
