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
    title="GPT 注册机 · 使用教程"
    width="min(760px, calc(100vw - 32px))"
    top="4vh"
    class="turb-register-usage-guide"
    destroy-on-close
  >
    <div class="guide-content">
      <p class="guide-intro">先完成配置，再用一个本人或获授权的邮箱验证流程。打开本教程不会启动注册、接码或修改配置。</p>
      <ol class="guide-steps">
        <li>
          <h3>检查服务与配置</h3>
          <p>确认下方注册控制台已连接。进入「配置」，按实际环境选择注册驱动、配置「邮箱 / OTP」，然后点击「保存配置」；如提示需要重启，请按提示操作。</p>
          <p class="hint">这里是独立服务，不会沿用主面板的帐号、代理或邮箱设置；出现授权提示时，使用本服务的授权码。</p>
        </li>
        <li>
          <h3>准备邮箱素材</h3>
          <p>使用邮箱池来源时，到「邮箱池」选择对应类型并导入；由 API 自动生成邮箱的来源，按该来源的配置要求准备即可。</p>
          <p class="hint">供<strong>新注册</strong>使用的邮箱，不要勾选「导入后默认视为“注册成功账号”」。该选项用于已有帐号补授权，会将邮箱标记为已用。具体格式见下方说明。</p>
        </li>
        <li>
          <h3>先运行一个任务</h3>
          <p>在「注册」中将注册数量和并发线程数都设为 <strong>1</strong>，确认配置后点击「开始注册」。首次排查可把失败自动重试次数设为 0，并点击「保存次数」。</p>
          <p class="hint">「开始注册」会真正创建任务，重复点击会追加任务；邮箱、代理、浏览器或短信服务可能产生费用，请先核对权限和余额。</p>
        </li>
        <li>
          <h3>跟进状态与日志</h3>
          <p>在「任务列表」查看进度，点击任务行的「补跑日志」查看日志；失败时结合「失败现场」（如有）排查。手动验证码模式需填写手动注册邮箱，数量只能为 1，并在提示时提交邮箱验证码。</p>
          <p class="hint">「取消所有排队」只处理未开始的任务；正在运行的任务需单独点「停止」，并等待步骤检查点退出。</p>
        </li>
        <li>
          <h3>查看与保存结果</h3>
          <p>在本模块「账号」查看注册结果，按需「下载TXT」；需要 Codex 时，再到「Codex 授权」核对授权结果。</p>
          <p class="hint"><strong>注册成功不等于 Codex 授权成功</strong>，也不代表获得任何优惠或资格。结果不会自动同步到主面板「帐号资格验证」。</p>
        </li>
      </ol>

      <el-collapse v-model="openTopics" class="guide-topics">
        <el-collapse-item title="邮箱素材怎么导入？与主面板有什么不同？" name="email">
          <p>在「邮箱池 → 导入邮箱素材」选择类型，每行一条：</p>
          <dl class="guide-definitions">
            <dt>Outlook 邮箱池</dt>
            <dd><code class="format-example">邮箱----密码----clientId----refreshToken</code></dd>
            <dt>通用 API 取码邮箱</dt>
            <dd><code class="format-example">邮箱----取码地址</code></dd>
          </dl>
          <p>这里支持 ---- 或 ==== 分隔，不使用主面板的两、三、四个减号混用规则，也不是「邮箱 / 密码 / 2FA密钥」格式。不要直接粘贴主面板凭证。</p>
          <p>新注册素材不要勾选「导入后默认视为“注册成功账号”」；已有帐号需要补 Codex 授权时才使用该选项，并核对导入类型和凭证。</p>
        </el-collapse-item>
        <el-collapse-item title="必须启用 Codex 或购买短信接码吗？" name="codex">
          <p>不是所有注册都需要短信。是否尝试注册后自动授权，由「配置 → 功能开关 → 启用 Codex OAuth」控制；仅需要注册时可关闭。</p>
          <p>需要 Codex 授权时，再配置对应驱动与「Codex」选项；若授权流程要求手机验证，需自行配置受支持的「接码平台」，核对可用地区、余额与实际费用。邮箱验证码与短信验证码是两种不同验证。</p>
          <p>能否完成注册或授权取决于实际服务与平台要求；请遵守服务规则，不要尝试绕过验证。</p>
        </el-collapse-item>
        <el-collapse-item title="主面板的代理池能直接用吗？" name="proxy">
          <p>不会自动同步。需要代理时，在本模块「代理管理」中单独配置，并确认当前驱动的代理选项；不要假定主面板选中的代理在这里生效。</p>
          <p>只使用可信代理。配置代理不保证注册或授权成功，也不保证消除风控。</p>
        </el-collapse-item>
        <el-collapse-item title="重试、停止、取消排队和删除有什么区别？" name="tasks">
          <dl class="guide-definitions">
            <dt>重试 / 重试选中</dt><dd>重新处理选定任务，先看日志与帐号状态。已经创建帐号的任务可能只需补 Codex 授权，不要重复提交新注册。</dd>
            <dt>停止</dt><dd>请求停止正在运行的任务，需等待步骤检查点，不一定立即结束。</dd>
            <dt>取消所有排队</dt><dd>只取消未开始的任务，不影响正在运行的任务。</dd>
            <dt>删除 / 删除选中</dt><dd>删除任务及其日志，运行中任务不能直接删除。需要排查的日志请先妥善保留。</dd>
          </dl>
          <p>「手动刷新」只更新任务列表；「保存次数」只保存失败自动重试次数，都不是启动注册。</p>
        </el-collapse-item>
        <el-collapse-item title="打不开、收不到验证码或只成功了一部分？" name="troubleshooting">
          <p>控制台打不开：确认独立服务可在 localhost:5050 访问，然后刷新主面板；如出现授权提示，使用独立服务的授权码。本教程在服务未连接时仍可查看。</p>
          <p>收不到邮箱验证码：检查所选邮箱来源、取码凭证、接口状态和可用素材；新邮箱若被标记为注册成功，会被视为已用。手动模式请按界面提示提交邮箱验证码。</p>
          <p>短信或外部服务失败：核对配置、权限、余额与服务状态；遇到平台限制时遵循官方提示。先看具体错误，不要连续追加任务。</p>
          <p>注册成功但 Codex 失败：分别查看「账号」与「Codex 授权」状态，根据日志处理补授权，无需据此再次注册。</p>
        </el-collapse-item>
        <el-collapse-item title="导出和分享前，需要保护哪些资料？" name="privacy">
          <p>帐号密码、邮箱令牌、短信 / API 密钥、Codex JSON、任务日志和输出文件都可能包含敏感信息。导出后妥善保存，不要公开分享，也不要混入发给他人的代码包。</p>
          <p>注册机拥有自己的帐号、任务记录、邮箱池与代理配置；主面板不会自动汇总这些数据。</p>
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
.format-example {
  display: block;
  padding: 8px 12px;
  margin: 8px 0;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-family: var(--app-font-mono);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.guide-topics { margin-top: 8px; }
.guide-topics :deep(.el-collapse-item__header) { height: auto; min-height: 44px; line-height: 1.5; font-weight: 500; padding: 8px 0; gap: 8px; }
.guide-topics :deep(.el-collapse-item__content) { line-height: 1.7; padding-bottom: 12px; }
.guide-definitions { margin: 0; }
.guide-definitions dt { font-weight: 600; color: var(--app-title); }
.guide-definitions dd { margin: 2px 0 12px; }
</style>
