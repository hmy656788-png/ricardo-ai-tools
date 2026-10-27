<script setup>
import { onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Connection, Reading } from '@element-plus/icons-vue'
import { useThemeStore } from '@/stores/theme'
import MuseRegisterUsageGuide from '@/components/MuseRegisterUsageGuide.vue'

// Muse 注册机保持为独立 Flask 服务，主面板不代理 API，也不共享数据库。
const toolOrigin = 'http://127.0.0.1:8801'
const BUILD_VERSION = '20261027-muse-layout-v3'
const theme = useThemeStore()
const params = new URLSearchParams({
  embedded: '1',
  theme: theme.dark ? 'dark' : 'light',
  parentOrigin: window.location.origin,
  ui: 'ricardo-integrated-1',
  v: BUILD_VERSION,
})
const toolUrl = `${toolOrigin}/?${params}`
const frameRef = ref(null)
const frameReady = ref(false)
const frameHeight = ref(0)
const connectionSlow = ref(false)
const guideVisible = ref(false)
let connectionTimer

function syncTheme() {
  // 新旧 Muse 页面均可安全忽略未知 postMessage；不向 iframe 传递账号或凭据。
  frameRef.value?.contentWindow?.postMessage({
    type: 'ricardo-ui-theme',
    theme: theme.dark ? 'dark' : 'light',
  }, toolOrigin)
}

function handleFrameLoad() {
  frameReady.value = true
  connectionSlow.value = false
  window.clearTimeout(connectionTimer)
  syncTheme()
}

function handleFrameMessage(event) {
  if (event.origin !== toolOrigin || !frameRef.value || event.source !== frameRef.value.contentWindow) return
  if (event.data?.type !== 'muse-register-layout') return
  const height = event.data.height
  if (typeof height !== 'number' || !Number.isFinite(height) || height < 240 || height > 20000) return
  frameHeight.value = Math.ceil(height)
  frameReady.value = true
  connectionSlow.value = false
  window.clearTimeout(connectionTimer)
}

watch(() => theme.dark, syncTheme)
onActivated(syncTheme)
onMounted(() => {
  window.addEventListener('message', handleFrameMessage)
  connectionTimer = window.setTimeout(() => {
    if (!frameReady.value) connectionSlow.value = true
  }, 6000)
})
onBeforeUnmount(() => {
  window.removeEventListener('message', handleFrameMessage)
  window.clearTimeout(connectionTimer)
})
</script>

<template>
  <div class="page muse-register-page">
    <header class="muse-register-header">
      <div>
        <h1>Muse 注册机</h1>
        <p>独立运行 Muse 注册控制台；邮箱池、卡池、代理池和任务数据保持隔离。</p>
      </div>
      <div class="header-actions">
        <span class="isolation-badge"><el-icon><Connection /></el-icon> 独立服务 · 数据隔离</span>
        <el-button plain @click="guideVisible = true">
          <el-icon><Reading /></el-icon><span>使用教程</span>
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="!frameReady"
      :type="connectionSlow ? 'warning' : 'info'"
      :closable="false"
      show-icon
      :title="connectionSlow ? '暂未连接到 Muse 注册机' : '正在连接 Muse 注册控制台…'"
      :description="connectionSlow ? '请确认 Muse 服务已在 127.0.0.1:8801 启动。' : ''"
      class="connection-alert"
    />

    <div class="muse-register-frame-wrap">
      <iframe
        ref="frameRef"
        name="ricardo-muse-register"
        :src="toolUrl"
        title="Muse 注册机"
        class="muse-register-frame"
        :style="frameHeight ? { height: `${frameHeight}px` } : undefined"
        sandbox="allow-scripts allow-forms allow-same-origin allow-downloads allow-modals"
        referrerpolicy="no-referrer"
        @load="handleFrameLoad"
      />
    </div>
    <p class="isolation-note">主面板不会读取或写入 Muse 的邮箱、银行卡、代理、Token 或任务数据库。</p>
    <MuseRegisterUsageGuide v-model="guideVisible" />
  </div>
</template>

<style scoped>
.muse-register-page { max-width: 1200px; margin: 0 auto; min-width: 0; }
.muse-register-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 8px 0 24px; }
.muse-register-header h1 { margin: 0 0 10px; color: var(--app-title); font-size: 25px; font-weight: 600; }
.muse-register-header p { margin: 0; color: var(--el-text-color-secondary); font-size: 14px; line-height: 1.6; }
.header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.header-actions .el-button + .el-button { margin-left: 0; }
.header-actions .el-button span { margin-left: 8px; }
.isolation-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); white-space: nowrap; }
.muse-register-frame-wrap { min-height: 0; min-width: 0; overflow: visible; background: transparent; }
.muse-register-frame { display: block; width: 100%; height: 720px; border: 0; background: transparent; }
.isolation-note { margin: 12px 0 4px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.connection-alert { margin-bottom: 16px; }
@media (max-width: 768px) {
  .muse-register-header { align-items: flex-start; flex-direction: column; gap: 14px; }
}
</style>
