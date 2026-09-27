/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0b1633',
        paper: '#f4f7fb',
        cobalt: '#1e5eff',
        cyan: '#17b6c8',
      },
      boxShadow: {
        panel: '0 18px 50px rgba(16, 36, 80, 0.08)',
      },
    },
  },
  plugins: [],
}
