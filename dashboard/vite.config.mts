import react from '@vitejs/plugin-react'
import autoprefixer from 'autoprefixer'
import tailwindcss from 'tailwindcss'
import { defineConfig } from 'vite'
import svgr from 'vite-plugin-svgr'
import tsconfigPaths from 'vite-tsconfig-paths'
import path from 'path'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  base: process.env.BASE_URL,
  clearScreen: false,
  server: {
    host: true,
  },
  css: {
    postcss: {
      plugins: [tailwindcss, autoprefixer],
    },
  },
  build: {
    outDir: 'build',
    assetsDir: 'statics',
  },
  resolve: {
    alias: [
      {
        find: '@',
        replacement: path.resolve(__dirname, 'src'),
      },
    ],
  },
  plugins: [
    react(),
    svgr(),
    tsconfigPaths(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Marzban',
        short_name: 'Marzban',
        description: 'Marzban: Modern dashboard for managing proxies and users.',
        theme_color: '#1b1b1d',
        background_color: '#1b1b1d',
        display: 'standalone',
        start_url: '/',
        scope: '/',
        icons: [
          {
            src: '/statics/favicon/android-chrome-192x192.png',
            sizes: '192x192',
            type: 'image/png'
          },
          {
            src: '/statics/favicon/android-chrome-512x512.png',
            sizes: '512x512',
            type: 'image/png'
          },
          {
            src: '/statics/favicon/apple-touch-icon.png',
            sizes: '180x180',
            type: 'image/png'
          }
        ]
      }
    })
  ],
})
