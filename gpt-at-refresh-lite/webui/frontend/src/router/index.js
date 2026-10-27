import { createRouter, createWebHashHistory } from 'vue-router'
import NProgress from 'nprogress'

NProgress.configure({ showSpinner: false, trickleSpeed: 120, minimum: 0.15 })

// hash 路由：不依赖后端做 SPA 回退，FastAPI / 未来 Gin 都零配置可用。
//
// 轻量版（AT Refresh Lite）：账号列表、运行记录和代理池统一在导入账号页管理。
// 客户场景 = 把现成账号（邮箱----密码----2FA）导进来，配好代理，一键刷新 AT。
// 其余注册/批量/配置等页面全部裁掉；密码 / 2FA 是单独安全工具模块，
// 不与账号导入页共用数据和操作入口。
const routes = [
  {
    path: '/',
    redirect: '/registered',
  },
  {
    path: '/proxy',
    name: 'proxy',
    // 兼容旧书签：代理池与账号共享配置，整合为导入账号页内标签。
    redirect: (to) => ({ path: '/registered', query: { ...to.query, tab: 'proxy' } }),
  },
  {
    path: '/registered',
    name: 'registered',
    component: () => import('@/views/Registered.vue'),
    meta: { title: '帐号资格验证', icon: 'CircleCheck', group: '账号' },
  },
  {
    path: '/change-2fa',
    name: 'change-2fa',
    component: () => import('@/views/Change2FA.vue'),
    meta: { title: '密码/2FA修改', icon: 'Lock', group: '安全工具' },
  },
  {
    path: '/2fa-code',
    name: '2fa-code',
    component: () => import('@/views/TwoFactorCode.vue'),
    meta: { title: '2FA验证码获取', icon: 'Timer', group: '安全工具' },
  },
  {
    path: '/gpt-register',
    name: 'gpt-register',
    component: () => import('@/views/TurbRegister.vue'),
    meta: { title: 'GPT 注册机', icon: 'GptMark', group: '自动化工具' },
  },
  {
    path: '/updates',
    name: 'updates',
    component: () => import('@/views/UpdateCenter.vue'),
    meta: { title: '更新中心', icon: 'Refresh', group: '配置', hideFromMenu: true },
  },
  {
    path: '/muse-register',
    name: 'muse-register',
    component: () => import('@/views/MuseRegister.vue'),
    meta: { title: 'Muse 注册机', icon: 'MuseMark', group: '自动化工具' },
  },
  {
    path: '/runs',
    name: 'runs',
    // 兼容旧书签：运行记录已并入导入账号，不再生成独立菜单项。
    redirect: (to) => ({ path: '/registered', query: { ...to.query, tab: 'runs' } }),
  },
  // 兜底：手输旧路径（/import /auto /settings/... 等）一律回导入账号页
  {
    path: '/:pathMatch(.*)*',
    redirect: '/registered',
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 路由切换顶部进度条
router.beforeEach((to, from, next) => {
  NProgress.start()
  if (to.meta?.title) document.title = `${to.meta.title} · Ricardo AI 工具`
  next()
})
router.afterEach(() => {
  NProgress.done()
})

export default router
