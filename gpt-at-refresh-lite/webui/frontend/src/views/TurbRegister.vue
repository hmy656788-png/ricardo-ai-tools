<script setup>
import { onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Reading } from '@element-plus/icons-vue'
import { useThemeStore } from '@/stores/theme'
import TurbRegisterUsageGuide from '@/components/TurbRegisterUsageGuide.vue'

// 只聚合独立服务的 UI，不代理它的 API，也不共享 8768 的账号库/任务队列。
// 使用 localhost 而不是 127.0.0.1，让两个本地服务使用不同的 host-only Cookie。
const toolOrigin = 'http://localhost:5050'
const theme = useThemeStore()
const params = new URLSearchParams({
  embedded: '1', theme: theme.dark ? 'dark' : 'light',
  parentOrigin: window.location.origin, ui: 'ricardo-integrated-1',
})
const toolUrl = `${toolOrigin}/?${params}#register`
const frameRef = ref(null)
const frameReady = ref(false)
const frameHeight = ref(0)
const connectionSlow = ref(false)
const guideVisible = ref(false)
let connectionTimer

function syncTheme() {
  frameRef.value?.contentWindow?.postMessage({
    type: 'ricardo-ui-theme', theme: theme.dark ? 'dark' : 'light',
  }, toolOrigin)
}

function handleFrameLoad() {
  syncTheme()
  window.clearTimeout(connectionTimer)
  if (!frameReady.value) connectionTimer = window.setTimeout(() => { connectionSlow.value = true }, 6000)
}

function handleFrameMessage(event) {
  if (event.origin !== toolOrigin || !frameRef.value || event.source !== frameRef.value.contentWindow) return
  if (event.data?.type !== 'turb-register-layout') return
  const height = event.data.height
  if (typeof height !== 'number' || !Number.isFinite(height) || height < 200 || height > 20000) return
  frameHeight.value = Math.ceil(height)
  frameReady.value = true
  connectionSlow.value = false
  window.clearTimeout(connectionTimer)
}

watch(() => theme.dark, syncTheme)
onActivated(syncTheme)
onMounted(() => {
  window.addEventListener('message', handleFrameMessage)
  connectionTimer = window.setTimeout(() => { if (!frameReady.value) connectionSlow.value = true }, 6000)
})
onBeforeUnmount(() => {
  window.removeEventListener('message', handleFrameMessage)
  window.clearTimeout(connectionTimer)
})

</script>

<template>
  <div class="page turb-register-page">
    <header class="turb-register-header">
      <div>
        <h1>GPT 注册机</h1>
        <p>统一查看注册任务、账号与邮箱资源。</p>
      </div>
      <div class="header-actions">
        <span class="isolation-badge"><el-icon><Lock /></el-icon> 独立服务 · 数据隔离</span>
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
      :title="connectionSlow ? '暂未连接到 GPT 注册机' : '正在连接本地注册控制台…'"
      :description="connectionSlow ? '请确认独立服务已在 localhost:5050 启动。' : ''"
      class="connection-alert"
    />
    <div class="turb-register-frame-wrap">
      <iframe
        ref="frameRef"
        name="ricardo-gpt-register"
        :src="toolUrl"
        title="独立 GPT 注册机"
        class="turb-register-frame"
        :style="frameHeight ? { height: `${frameHeight}px` } : undefined"
        sandbox="allow-scripts allow-forms allow-same-origin allow-downloads"
        referrerpolicy="no-referrer"
        @load="handleFrameLoad"
      />
    </div>
    <p class="isolation-note">授权、邮箱池、短信 / 代理配置和输出文件由本模块独立管理；不读取主面板账号库，请勿在此导入主面板凭证。</p>
    <TurbRegisterUsageGuide v-model="guideVisible" />
  </div>
</template>

<style scoped>
.turb-register-page { max-width: 1200px; margin: 0 auto; min-width: 0; }
.turb-register-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 8px 0 24px; }
.turb-register-header h1 { margin: 0 0 10px; color: var(--app-title); font-size: 25px; font-weight: 600; }
.turb-register-header p { margin: 0; color: var(--el-text-color-secondary); font-size: 14px; line-height: 1.6; }
.header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.header-actions .el-button + .el-button { margin-left: 0; }
.header-actions .el-button span { margin-left: 8px; }
.isolation-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); white-space: nowrap; }
.turb-register-frame-wrap { min-height: 0; min-width: 0; overflow: visible; background: transparent; }
.turb-register-frame { display: block; width: 100%; height: 760px; border: 0; background: transparent; }
.isolation-note { margin: 12px 0 4px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; }
.connection-alert { margin-bottom: 16px; }
@media (max-width: 768px) {
  .turb-register-header { align-items: flex-start; flex-direction: column; gap: 14px; }
}
</style>
