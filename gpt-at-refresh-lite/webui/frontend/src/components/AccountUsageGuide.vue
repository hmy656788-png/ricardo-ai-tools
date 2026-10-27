<script setup>
import { ref } from 'vue'

defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue', 'navigate'])
const openTopics = ref([])
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
    title="帐号资格验证 · 使用教程"
    width="min(760px, calc(100vw - 32px))"
    top="4vh"
    class="account-usage-guide"
    destroy-on-close
  >
    <div class="guide-content">
      <p class="guide-intro">建议先用一个有权管理的帐号完成下面的流程，确认成功后再处理更多帐号。</p>
      <ol class="guide-steps">
        <li>
          <h3>添加帐号</h3>
          <p>点击「＋ 添加账号」，每行粘贴一个帐号的邮箱、密码和 2FA 密钥。</p>
          <code class="format-example">邮箱|密码|2FA密钥</code>
          <p class="hint">支持英文 |、全角 ｜（可混用），也保留 --、---、---- 混用，导入时自动统一为四减号。竖线格式最多三段；密码含竖线、连续或边界减号时，请用四减号分隔，并在分隔符两侧加空格。有歧义或无法无损转换的行会提示检查。</p>
        </li>
        <li>
          <h3>准备访问凭证</h3>
          <p>新导入的帐号通常没有 AT。先勾选帐号，点击「刷新AT」并确认，等待成功后再检测。已有可用 AT 的帐号可跳过这一步。</p>
          <p class="hint">AT 是访问凭证。刷新可能执行登录并写回新凭证，不只是更新页面。</p>
        </li>
        <li>
          <h3>检测资格</h3>
          <p>勾选要处理的帐号，点击「检测选中」。也可以点击「检查未检测」，处理<strong>当前页</strong>还没有检测结论的帐号。</p>
          <p class="hint">跨页处理时，请切换页码或调整每页数量后再检测；当前检测逐号执行。</p>
        </li>
        <li>
          <h3>查看结果</h3>
          <p>在「Plus / 时间」列查看帐号状态和检测时间，用上方筛选框筛选结果。提示凭证失效时先检查凭证；网络失败不等于没有资格。</p>
          <p class="hint">可领 Plus、折扣等仅表示检测时接口返回了相关活动，不代表已领取或已开通，最终资格以官方页面为准。</p>
        </li>
      </ol>

      <el-collapse v-model="openTopics" class="guide-topics">
        <el-collapse-item title="刷新、刷新AT、刷新refresh 有什么区别？" name="credentials">
          <dl class="guide-definitions">
            <dt>刷新</dt><dd>只重新加载当前账号列表，不更新登录凭证。</dd>
            <dt>刷新AT</dt><dd>更新访问凭证：优先使用已有 refresh / session 凭证，必要时使用密码与 2FA 登录。</dd>
            <dt>刷新refresh</dt><dd>重新登录授权，补充 refresh_token（RT），不是日常资格检测的必经步骤。此导入帐号流程不依赖邮箱接码，但可能遇到额外的手机号验证；以任务实际结果为准。</dd>
          </dl>
        </el-collapse-item>
        <el-collapse-item title="什么时候需要代理池？" name="proxy">
          <p>网络正常时可以留空直连。需要代理时，在「代理池」标签添加，再回「账号列表」的代理下拉框选择。</p>
          <p>资格检测只使用选定的单个代理，不轮询整个池。「刷新AT / 刷新refresh」在勾选超过 2 个帐号、已选择代理且池中至少有 2 个代理时，才按现有配置轮询整池；其余模式以操作确认提示为准。</p>
          <p>配置代理不保证绕过风控；不要使用来源不明的代理处理帐号凭证。</p>
        </el-collapse-item>
        <el-collapse-item title="为什么运行记录可能为空？" name="runs">
          <p>「运行记录」展示最近 50 条已接入任务的记录，包括补 refresh 等任务，可按错误分类筛选并手动刷新。</p>
          <p>导入帐号、刷新 AT、资格检测目前不写入此列表；独立模块的记录也不会自动汇总。请同时查看当前操作的结果提示。</p>
        </el-collapse-item>
        <el-collapse-item title="导出、删除前需要注意什么？" name="data">
          <p>在「账号列表」的导出菜单中选择所需格式，导出后妥善保存。删除选中、清空全部等操作前，请先核对范围并备份需要保留的数据。</p>
          <p>密码、2FA 密钥、Token 和导出文件都属于敏感资料，不要公开分享，也不要放进发给他人的代码包。</p>
        </el-collapse-item>
      </el-collapse>
    </div>
    <template #footer>
      <div class="guide-footer">
        <el-button @click="emit('navigate', 'proxy')">查看代理池</el-button>
        <el-button type="primary" @click="emit('update:modelValue', false)">我知道了</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.guide-content { max-height: min(64vh, 560px); overflow-y: auto; padding-right: 8px; line-height: 1.7; }
.guide-intro { margin: 0 0 18px; color: var(--el-text-color-secondary); }
.guide-steps { margin: 0; padding-left: 24px; }
.guide-steps li { padding-left: 8px; margin-bottom: 18px; }
.guide-steps li::marker { color: var(--account-accent, var(--brand-dark)); font-weight: 600; }
.guide-steps h3 { margin: 0 0 5px; color: var(--app-title); font-size: 15px; font-weight: 600; }
.guide-content p { margin: 0 0 8px; }
.guide-steps strong { font-weight: 600; color: var(--app-title); }
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
.guide-footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
.guide-footer :deep(.el-button + .el-button) { margin-left: 0; }
</style>
