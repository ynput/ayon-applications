import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Fonts are provided by AYON frontend, don't bundle them (they take ~4.5MB)
const stripFontFaces = () => ({
  name: 'strip-font-faces',
  enforce: 'pre',
  transform(code, id) {
    if (!/@ynput[\\/]ayon-react-components[\\/].*\.css/.test(id)) return null
    return { code: code.replace(/@font-face\s*\{[^}]*\}/g, ''), map: null }
  },
})

export default ({ mode }) => {
  Object.assign(process?.env, loadEnv(mode, process?.cwd(), ''))

  const SERVER_URL = process?.env?.SERVER_URL || 'http://localhost:5000'

  return defineConfig({
    server: {
      proxy: {
        '/api': {
          target: SERVER_URL,
          changeOrigin: true,
        },
        '/addons': {
          target: SERVER_URL,
          changeOrigin: false,
        },
      },
    },
    plugins: [stripFontFaces(), react()],
    base: '',
  })
}
