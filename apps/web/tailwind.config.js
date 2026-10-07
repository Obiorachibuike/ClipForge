/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Semantic surface and text scales are CSS-variable backed so every
        // existing utility switches cleanly when the root theme class changes.
        ink: {
          950: 'rgb(var(--ink-950) / <alpha-value>)',
          900: 'rgb(var(--ink-900) / <alpha-value>)',
          850: 'rgb(var(--ink-850) / <alpha-value>)',
          800: 'rgb(var(--ink-800) / <alpha-value>)',
          700: 'rgb(var(--ink-700) / <alpha-value>)',
          600: 'rgb(var(--ink-600) / <alpha-value>)',
          500: 'rgb(var(--ink-500) / <alpha-value>)',
          400: 'rgb(var(--ink-400) / <alpha-value>)',
        },
        slate: {
          50: 'rgb(var(--slate-50) / <alpha-value>)',
          100: 'rgb(var(--slate-100) / <alpha-value>)',
          200: 'rgb(var(--slate-200) / <alpha-value>)',
          300: 'rgb(var(--slate-300) / <alpha-value>)',
          400: 'rgb(var(--slate-400) / <alpha-value>)',
          500: 'rgb(var(--slate-500) / <alpha-value>)',
          600: 'rgb(var(--slate-600) / <alpha-value>)',
        },
        accent: {
          DEFAULT: '#5B6CFF',
          50: '#EEF0FF',
          100: '#DCE0FF',
          200: '#B9C0FF',
          300: '#96A0FF',
          400: '#7C8CFF',
          500: '#5B6CFF',
          600: '#4351E0',
          700: '#333EB8',
          800: '#262E8C',
          900: '#1B2063',
        },
        violet: {
          400: '#9D7CFF',
          500: '#7C5CFF',
          600: '#6A45F0',
        },
        signal: {
          success: '#34D399',
          warning: '#FBBF24',
          danger: '#F87171',
          info: '#60A5FA',
        },
      },
      fontFamily: {
        sans: [
          'Inter var',
          'Inter',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: ['ui-monospace', 'SFMono-Regular', 'SF Mono', 'Menlo', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      borderRadius: {
        card: '1rem',
        pill: '999px',
      },
      boxShadow: {
        glow: 'var(--shadow-glow)',
        card: 'var(--shadow-card)',
        float: 'var(--shadow-float)',
      },
      backgroundImage: {
        'grid-dark':
          'linear-gradient(to right, var(--grid-line) 1px, transparent 1px), linear-gradient(to bottom, var(--grid-line) 1px, transparent 1px)',
        'hero-glow':
          'radial-gradient(60% 60% at 50% 0%, var(--hero-glow-strong) 0%, var(--hero-glow-soft) 40%, transparent 75%)',
        'accent-line': 'linear-gradient(90deg, #5B6CFF 0%, #7C5CFF 60%, #34D399 100%)',
      },
      backgroundSize: {
        grid: '32px 32px',
      },
      keyframes: {
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        pulseRing: {
          '0%': { transform: 'scale(0.9)', opacity: '0.7' },
          '70%': { transform: 'scale(1.35)', opacity: '0' },
          '100%': { opacity: '0' },
        },
      },
      animation: {
        shimmer: 'shimmer 1.8s linear infinite',
        pulseRing: 'pulseRing 1.8s cubic-bezier(0.24, 0.6, 0.35, 1) infinite',
      },
      transitionTimingFunction: {
        premium: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    },
  },
  plugins: [],
};
