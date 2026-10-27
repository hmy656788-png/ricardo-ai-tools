<script setup>
import { onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useThemeStore } from '@/stores/theme'

// 独立服务、独立数据库、独立任务队列；这里只提供主面板中的统一入口。
// 使用 #top 让嵌入页每次从仪表盘顶部开始，避免复用 5033 页面上一次滚动到
//「结果/队列」的位置，导致在主面板里看起来像是顶部内容被裁掉。
const toolOrigin = 'http://127.0.0.1:5033'
const theme = useThemeStore()
// 初始主题通过 URL 传递以避免闪烁；后续只传主题消息，不重载业务页面。
const params = new URLSearchParams({
  embedded: '1', theme: theme.dark ? 'dark' : 'light',
  parentOrigin: window.location.origin, ui: 'ricardo-integrated-1',
})
const toolUrl = `${toolOrigin}/?${params}#top`
const frameReady = ref(false)
const connectionSlow = ref(false)
const frameRef = ref(null)
const frameHeight = ref(0)
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
  if (event.origin !== toolOrigin) return
  if (!frameRef.value || event.source !== frameRef.value.contentWindow) return
  if (event.data?.type !== 'change2fa-layout') return
  const height = event.data.height
  if (typeof height === 'number' && Number.isFinite(height) && height >= 200 && height <= 20000) {
    frameHeight.value = Math.ceil(height)
    frameReady.value = true
    connectionSlow.value = false
    window.clearTimeout(connectionTimer)
  }
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
  <div class="page change-2fa-page">
    <header class="change-2fa-header">
      <div>
        <h1>密码/2FA修改</h1>
        <p>批量检查账号，管理密码与双重验证设置。</p>
      </div>
      <div class="header-actions">
        <span class="isolation-badge"><el-icon><Lock /></el-icon> 独立服务 · 数据隔离</span>
      </div>
    </header>

    <el-alert
      v-if="!frameReady"
      :type="connectionSlow ? 'warning' : 'info'"
      :closable="false"
      show-icon
      :title="connectionSlow ? '暂未连接到密码/2FA修改工具' : '正在连接本地安全工具…'"
      :description="connectionSlow ? '请确认 Change-2fa-Password-GPT-Auto 服务已在 127.0.0.1:5033 启动。' : ''"
      class="connection-alert"
    />
    <div class="change-2fa-frame-wrap">
      <iframe
        ref="frameRef"
        :src="toolUrl"
        title="密码/2FA修改工具"
        class="change-2fa-frame"
        :style="frameHeight ? { height: `${frameHeight}px` } : undefined"
        referrerpolicy="no-referrer"
        @load="handleFrameLoad"
      />
    </div>
    <p class="isolation-note">本模块使用独立的账号数据和任务队列，不会读取或修改「帐号资格验证」中的记录。</p>
  </div>
</template>

<style scoped>
.change-2fa-page { max-width: 1200px; margin: 0 auto; min-width: 0; }
.change-2fa-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 8px 0 24px; }
.change-2fa-header h1 { margin: 0 0 10px; color: var(--app-title); font-size: 25px; font-weight: 600; }
.change-2fa-header p { margin: 0; color: var(--el-text-color-secondary); font-size: 14px; line-height: 1.6; }
.header-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.header-actions .el-button span { margin-left: 8px; }
.isolation-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); white-space: nowrap; }
.change-2fa-frame-wrap { min-height: 0; min-width: 0; overflow: visible; background: transparent; }
.change-2fa-frame { display: block; width: 100%; height: 760px; border: 0; background: transparent; }
.isolation-note { margin: 12px 0 4px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.connection-alert { margin-bottom: 16px; }
@media (max-width: 768px) {
  .change-2fa-header { align-items: flex-start; flex-direction: column; gap: 14px; }
}
</style>
