/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Premium dark-first palette: charcoal/near-black surfaces, restrained
        // blue/purple accents, high-contrast typography.
        ink: {
          950: '#06060A',
          900: '#0A0A10',
          850: '#0E0E16',
          800: '#12121C',
          700: '#1A1A26',
          600: '#242433',
          500: '#33334A',
          400: '#4A4A66',
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
        glow: '0 0 0 1px rgba(91,108,255,0.25), 0 8px 40px -12px rgba(91,108,255,0.45)',
        card: '0 1px 0 0 rgba(255,255,255,0.03) inset, 0 12px 32px -18px rgba(0,0,0,0.9)',
        float: '0 24px 60px -24px rgba(0,0,0,0.95)',
      },
      backgroundImage: {
        'grid-dark':
          'linear-gradient(to right, rgba(255,255,255,0.04) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,0.04) 1px, transparent 1px)',
        'hero-glow':
          'radial-gradient(60% 60% at 50% 0%, rgba(91,108,255,0.28) 0%, rgba(124,92,255,0.12) 40%, rgba(6,6,10,0) 75%)',
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
