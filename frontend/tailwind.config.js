/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // deep petrol — technical, distinct from generic Tailwind blue
        brand: {
          50: '#effaf9',
          100: '#d7f0ee',
          200: '#aee0dd',
          300: '#7cc8c4',
          400: '#45a8a3',
          500: '#1f8d88',
          600: '#0f716c',
          700: '#0d5a56',
          800: '#0e4845',
          900: '#0f3b39',
        },
      },
      fontFamily: {
        sans: [
          'Inter', 'Inter var', 'system-ui', '-apple-system', 'BlinkMacSystemFont',
          '"Segoe UI"', 'Roboto', 'sans-serif',
        ],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(16 24 40 / 0.06), 0 1px 3px 0 rgb(16 24 40 / 0.04)',
        'card-hover': '0 4px 8px -2px rgb(16 24 40 / 0.08), 0 2px 4px -2px rgb(16 24 40 / 0.05)',
      },
    },
  },
  plugins: [],
}
