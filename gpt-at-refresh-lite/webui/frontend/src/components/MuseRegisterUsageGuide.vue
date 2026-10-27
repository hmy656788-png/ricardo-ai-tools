<script setup>
import { ref } from 'vue'

defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue'])
const openTopics = ref([])
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
    title="Muse 注册机 · 使用教程"
    width="min(760px, calc(100vw - 32px))"
    top="4vh"
    class="muse-register-usage-guide"
    destroy-on-close
  >
    <div class="guide-content">
      <p class="guide-intro">本教程只说明本地控制台的使用流程。请仅处理你本人或已获授权的账号、邮箱和支付资料，不要用它绕过平台验证或限制。</p>
      <ol class="guide-steps">
        <li>
          <h3>启动并确认独立服务</h3>
          <p>确认 Muse 服务已在 <code>127.0.0.1:8801</code> 启动；主面板连接成功后，页面会显示注册控制台。连接失败时检查该地址的服务状态并刷新主面板。</p>
          <p class="hint">Muse 使用独立的数据库、邮箱池、卡池、代理池和任务队列，不会读取主面板的帐号资格验证数据。</p>
        </li>
        <li>
          <h3>先配置基础环境</h3>
          <p>进入「配置」检查注册驱动、邮箱来源和必要的浏览器连接配置；使用 RoxyBrowser 等外部驱动时，先启动对应客户端并确认本地 API 可用。</p>
          <p class="hint">Token、Workspace ID、邮箱授权和其他密钥只放在本机配置中，不要粘贴到聊天、截图或代码包。</p>
        </li>
        <li>
          <h3>准备邮箱池</h3>
          <p>进入「邮箱池」选择 Outlook、Generic API 或 IMAP 等来源，按页面提示导入或配置取码服务。先用一个测试邮箱确认验证码可以正常读取。</p>
          <p class="hint">邮箱密码、refresh token 和 API 密钥属于敏感资料；不要把主面板的账号格式直接当作 Muse 邮箱素材格式。</p>
        </li>
        <li>
          <h3>按需配置卡池与代理池</h3>
          <p>流程确实需要时再配置「卡池（可选）」和「代理池」。代理请使用可信来源，并先检查连通性；配置代理不保证注册成功，也不代表可以绕过风控。</p>
          <p class="hint">银行卡和代理凭证会留在 Muse 本地数据中，使用前确认设备、权限和费用范围。</p>
        </li>
        <li>
          <h3>先运行一个小任务</h3>
          <p>回到「注册」，把注册数量和注册线程都设为 <strong>1</strong>，确认邮箱来源和邀请码（如有）后点击「开始注册」。首次排查建议关闭自动重试，避免重复提交。</p>
          <p class="hint">「开始注册」会实际创建任务；不要连续点击，也不要在未确认结果前重复使用同一邮箱。</p>
        </li>
        <li>
          <h3>查看任务与结果</h3>
          <p>在「注册任务」查看状态、错误和日志。失败时先阅读具体原因，修正配置后再重试；不要只根据“失败”标签盲目追加任务。</p>
          <p class="hint">删除任务只删除本地任务记录及日志，不会自动撤销已经创建的外部账号或服务状态。</p>
        </li>
      </ol>

      <el-collapse v-model="openTopics" class="guide-topics">
        <el-collapse-item title="为什么页面显示连接失败？" name="connection">
          <p>确认 Muse 服务使用 <code>127.0.0.1:8801</code> 启动，浏览器可以打开该地址；然后刷新主面板。主面板只是嵌入界面，不会代替 Muse 服务启动后端。</p>
        </el-collapse-item>
        <el-collapse-item title="邮箱验证码收不到怎么办？" name="otp">
          <p>依次检查邮箱来源、取码凭证、接口状态和邮箱是否仍可用。先用一个邮箱测试，确认邮件筛选条件和轮询间隔，再进行批量任务。</p>
        </el-collapse-item>
        <el-collapse-item title="卡池和代理池是不是必须的？" name="resources">
          <p>不是所有流程都需要卡池或代理池，是否需要取决于实际页面要求和你的合规使用场景。留空代理表示直连；使用代理也不保证成功或降低限制。</p>
        </el-collapse-item>
        <el-collapse-item title="任务失败后怎么排查？" name="troubleshooting">
          <p>先看任务日志中的具体错误，再区分邮箱取码、浏览器驱动、网络、平台验证或配置问题。修正后只重试一个任务；涉及平台限制时遵循官方提示，不要连续重试。</p>
        </el-collapse-item>
        <el-collapse-item title="数据会不会和主面板混在一起？" name="isolation">
          <p>不会。Muse 使用自己的数据库和任务队列；主面板也不会自动读取 Muse 的邮箱、银行卡、代理、Token 或输出文件。导出或分享前请先脱敏。</p>
        </el-collapse-item>
      </el-collapse>
    </div>
    <template #footer>
      <el-button type="primary" @click="emit('update:modelValue', false)">我知道了</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.guide-content { max-height: min(64vh, 560px); overflow-y: auto; padding-right: 8px; line-height: 1.7; }
.guide-intro { margin: 0 0 18px; color: var(--el-text-color-secondary); }
.guide-steps { margin: 0; padding-left: 24px; }
.guide-steps li { padding-left: 8px; margin-bottom: 18px; }
.guide-steps li::marker { color: var(--brand-dark); font-weight: 600; }
.guide-steps h3 { margin: 0 0 5px; color: var(--app-title); font-size: 15px; font-weight: 600; }
.guide-content p { margin: 0 0 8px; }
.guide-steps strong { font-weight: 600; color: var(--app-title); }
.hint { color: var(--el-text-color-secondary); font-size: 13px; }
.guide-content code { padding: 1px 5px; border-radius: 4px; background: var(--el-fill-color-lighter); font-family: var(--app-font-mono); font-size: 12px; }
.guide-topics { margin-top: 8px; }
.guide-topics :deep(.el-collapse-item__header) { height: auto; min-height: 44px; line-height: 1.5; font-weight: 500; padding: 8px 0; gap: 8px; }
.guide-topics :deep(.el-collapse-item__content) { line-height: 1.7; padding-bottom: 12px; }
</style>
