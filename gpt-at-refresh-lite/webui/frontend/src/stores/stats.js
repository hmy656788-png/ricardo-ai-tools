import { defineStore } from 'pinia'
import { ref } from 'vue'
import http from '@/api/request'

// 号池统计（总计/可用/进行中/完成/失败）。
// 轻量版：顶栏统计胶囊已裁掉，只有 runtime 的 SSE 回调在 bump 时顺手刷一下，
// 保留 store 只为不破坏 runtime.js 的既有调用点。
export const useStatsStore = defineStore('stats', () => {
  const stats = ref({ total: 0, available: 0, in_use: 0, done: 0, failed: 0 })
  let timer = null

  async function refresh() {
    try {
      const r = await http.get('/api/stats')
      if (r && r.stats) stats.value = r.stats
    } catch (e) {
      // 静默：统计失败不打扰用户
      console.error('stats refresh:', e)
    }
  }

  function startPolling(interval = 5000) {
    refresh()
    if (timer) clearInterval(timer)
    timer = setInterval(refresh, interval)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { stats, refresh, startPolling, stopPolling }
})
