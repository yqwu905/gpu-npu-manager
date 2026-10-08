import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时把 /api 转发到本机的中心服务；构建产物由中心服务在 /ui/ 下托管
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
