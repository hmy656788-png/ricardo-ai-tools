<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useThemeStore } from '@/stores/theme'
import GptMark from '@/components/GptMark.vue'
import MuseMark from '@/components/MuseMark.vue'

const route = useRoute()
const router = useRouter()
const theme = useThemeStore()
const brandLogo = '/static/ricardo-ai-logo-gold.png'

const collapse = ref(false)
const sidebarWidth = computed(() => collapse.value ? '64px' : '220px')
const serviceStatus = ref({ services: [], ok: false })
let serviceStatusTimer

const serviceSummary = computed(() => {
  const services = serviceStatus.value.services || []
  const online = services.filter((item) => item.up).length
  return `${online}/${services.length || 4}`
})

const serviceStatusTooltip = computed(() => {
  const services = serviceStatus.value.services || []
  if (!services.length) return '正在检查本地服务…'
  return services.map((item) => {
    const state = item.up ? '正常' : '离线'
    const latency = item.up && item.latency_ms != null ? ` · ${item.latency_ms} ms` : ''
    return `${item.name}：${state}${latency}`
  }).join('\n')
})

async function refreshServiceStatus() {
  try {
    const response = await fetch('/api/services/status', { cache: 'no-store' })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    serviceStatus.value = await response.json()
  } catch (_) {
    serviceStatus.value = { ok: false, services: [] }
  }
}

// 保留原有入口顺序，不显示分组标题；模块的功能与数据隔离保持不变。
const GROUP_ORDER = ['账号', '配置', '安全工具', '自动化工具']
const menuItems = computed(() => {
  const map = {}
  for (const r of router.getRoutes()) {
    if (!r.meta?.title || r.meta.hideFromMenu) continue
    const g = r.meta.group || '功能'
    ;(map[g] ||= []).push(r)
  }
  return GROUP_ORDER.flatMap((g) => map[g] || [])
})

const activeMenu = computed(() => route.path)
// 顶部只显示当前页面名称，避免重复显示左侧菜单分组（例如“账号 / 帐号资格验证”）。
const crumb = computed(() => [route.meta.title].filter(Boolean))

onMounted(() => {
  theme.apply()
  refreshServiceStatus()
  serviceStatusTimer = window.setInterval(refreshServiceStatus, 15000)
})

onBeforeUnmount(() => {
  window.clearInterval(serviceStatusTimer)
})
</script>
<template>
  <el-container class="admin" :class="{ 'is-collapsed': collapse }" :style="{ '--sidebar-width': sidebarWidth }">
    <el-aside id="sidebar-navigation" :width="sidebarWidth" class="sidebar">
      <div class="brand">
        <span class="logo">
          <img :src="brandLogo" alt="Ricardo AI" />
        </span>
        <span class="brand-name" :aria-hidden="collapse">Ricardo AI 工具</span>
      </div>
      <el-scrollbar>
        <!-- Keep the menu DOM stable: ElMenu's collapse mode remounts the list and
             runs a separate width transition. The sidebar alone controls width. -->
        <el-menu :default-active="activeMenu" router :collapse="false" :collapse-transition="false" class="side-menu">
          <el-tooltip
            v-for="r in menuItems" :key="r.path"
            :content="r.meta.title" :disabled="!collapse"
            placement="right" :show-after="120" :hide-after="0"
          >
            <el-menu-item :index="r.path" :aria-label="r.meta.title">
              <el-icon v-if="r.meta.icon === 'GptMark'" class="gpt-menu-icon"><GptMark /></el-icon>
              <el-icon v-else-if="r.meta.icon === 'MuseMark'" class="muse-menu-icon"><MuseMark /></el-icon>
              <el-icon v-else><component :is="r.meta.icon" /></el-icon>
              <template #title><span class="menu-label" :aria-hidden="collapse">{{ r.meta.title }}</span></template>
            </el-menu-item>
          </el-tooltip>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <button
      type="button"
      class="sidebar-toggle"
      :aria-label="collapse ? '展开侧边栏' : '收起侧边栏'"
      :title="collapse ? '展开侧边栏' : '收起侧边栏'"
      :aria-expanded="!collapse"
      aria-controls="sidebar-navigation"
      @click="collapse = !collapse"
    >
      <el-icon class="toggle-icon" :size="12" aria-hidden="true"><Fold /></el-icon>
    </button>

    <el-container class="workspace">
      <el-header class="topbar">
        <div class="left">
          <el-breadcrumb separator="/">
            <el-breadcrumb-item v-for="c in crumb" :key="c">{{ c }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="right">
          <el-tooltip :content="serviceStatusTooltip" placement="bottom" effect="light">
            <span class="service-health" :class="{ offline: !serviceStatus.ok }" aria-label="本地服务状态">
              <i aria-hidden="true" />服务 {{ serviceSummary }}
            </span>
          </el-tooltip>
          <el-tooltip content="更新中心">
            <el-button class="update-center-button" circle text aria-label="更新中心" @click="router.push({ name: 'updates' })">
              <el-icon :size="18"><Refresh /></el-icon>
            </el-button>
          </el-tooltip>
          <el-tooltip :content="theme.dark ? '浅色模式' : '深色模式'">
            <el-button circle text @click="theme.toggle">
              <el-icon :size="18"><Moon v-if="!theme.dark" /><Sunny v-else /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
      </el-header>

      <el-main class="content">
        <router-view v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.admin {
  --sidebar-duration: 260ms;
  --sidebar-easing: cubic-bezier(0.22, 1, 0.36, 1);
  height: 100vh;
  position: relative;
}
.sidebar {
  background: var(--app-sidebar-bg);
  border-right: 1px solid var(--app-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  transition: width var(--sidebar-duration) var(--sidebar-easing);
}
.sidebar-toggle {
  position: absolute;
  left: var(--sidebar-width);
  top: 50%;
  transform: translate(-50%, -50%);
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--app-border);
  border-radius: 11px;
  background: var(--app-sidebar-bg);
  color: var(--el-text-color-secondary);
  box-shadow: 0 1px 4px rgb(0 0 0 / 6%);
  cursor: pointer;
  transition: left var(--sidebar-duration) var(--sidebar-easing), color 0.2s, border-color 0.2s, background-color 0.2s;
}
.toggle-icon { transition: transform var(--sidebar-duration) var(--sidebar-easing); }
.is-collapsed .toggle-icon { transform: rotate(180deg); }
.service-health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  white-space: nowrap;
}
.service-health i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #67c23a;
  box-shadow: 0 0 0 3px rgb(103 194 58 / 12%);
}
.service-health.offline i {
  background: #e6a23c;
  box-shadow: 0 0 0 3px rgb(230 162 60 / 12%);
}
.sidebar-toggle:hover {
  color: var(--brand);
  border-color: var(--brand);
  background: var(--el-fill-color-light);
}
.sidebar-toggle:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 3px;
}
.sidebar-toggle:active { background: var(--el-fill-color); }
.brand {
  height: 56px;
  width: 220px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 17px;
  font-size: 16px;
  font-weight: 600;
  color: var(--app-title);
  border-bottom: 1px solid var(--app-border);
  white-space: nowrap;
  overflow: hidden;
}
.brand .logo {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: #fff;
  border: 1px solid color-mix(in srgb, #c7a34e 32%, var(--app-border));
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
  box-shadow: 0 2px 8px color-mix(in srgb, #c7a34e 14%, transparent);
}
.brand .logo img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.brand-name { letter-spacing: 0.1px; line-height: 1.4; }
.menu-label { font-size: 14px; font-weight: 400; }
.side-menu :deep(.el-menu-item.is-active) .menu-label { font-weight: 500; }
.brand-name, .menu-label {
  display: inline-block;
  white-space: nowrap;
  opacity: 1;
  transform: translateX(0);
  transition: opacity 160ms ease, transform var(--sidebar-duration) var(--sidebar-easing);
}
.is-collapsed .brand-name,
.is-collapsed .menu-label {
  opacity: 0;
  transform: translateX(-6px);
  pointer-events: none;
}
.side-menu { width: 100%; border-right: none; }
.side-menu :deep(.el-menu-item .el-icon) { flex-shrink: 0; }
.side-menu :deep(.gpt-menu-icon),
.side-menu :deep(.el-menu-item.is-active .gpt-menu-icon) { color: var(--el-text-color-primary); }
.side-menu :deep(.muse-menu-icon),
.side-menu :deep(.el-menu-item.is-active .muse-menu-icon) { color: var(--el-text-color-primary); }
.sidebar :deep(.el-scrollbar__wrap) { overflow-x: hidden; }
.workspace { min-width: 0; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--app-header-bg);
  border-bottom: 1px solid var(--app-border);
  padding: 0 16px;
}
.topbar .left { display: flex; align-items: center; gap: 12px; }
.topbar .right { display: flex; align-items: center; gap: 14px; }
.update-center-button { color: var(--el-text-color-secondary); }
.update-center-button:hover { color: var(--brand); }
.topbar :deep(.el-breadcrumb) { font-size: 13px; line-height: 1.5; }
.topbar :deep(.el-breadcrumb__inner) { font-weight: 400; }
.content { background: var(--app-content-bg); padding: 16px; }
@media (max-width: 768px) {
  .search-box { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar, .sidebar-toggle, .toggle-icon,
  .brand-name, .menu-label { transition: none; }
}
</style>
