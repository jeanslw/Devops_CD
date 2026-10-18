import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { fetchActiveApprovals } from '@/utils/approvalTarget'

/**
 * 部署页内嵌审批流程的共享状态.
 *
 * - panelApprovalId: 当前展开的审批单(>0 时部署页显示 ApprovalFlowPanel)
 * - activeApprovals: 本项目当前用户本人发起的全部活跃单(逐行列表)
 * - onPending(id): 部署/回滚 SSE 返回 PENDING:<id> 时调用,直接展开面板
 * - refreshActive(): 项目确定/切换/面板关闭后查询活跃单
 *
 * @param kind 部署形态 ssh|compose|k8s:只查询/展示本形态的审批单,
 *             Docker(compose) 与 K8s 页互不串显.
 */
export function useApprovalPanel(auth, selectedProject, kind = '') {
  const { t } = useI18n()
  const panelApprovalId = ref(0)
  const activeApprovals = ref([])

  const activeApproval = computed(() => activeApprovals.value[0] || null)

  const bannerText = computed(() => {
    const s = activeApproval.value?.status
    return s ? t(`approvals.flowActive_${s}`) : ''
  })
  const bannerIcon = computed(() => {
    return ({ pending: '⏳', approved: '✅', deploying: '🚀' })[activeApproval.value?.status] || '📋'
  })

  function openPanel(id) {
    panelApprovalId.value = Number(id) || (activeApproval.value ? activeApproval.value.id : 0)
  }

  async function refreshActive() {
    if (panelApprovalId.value) {
      activeApprovals.value = []
      return
    }
    activeApprovals.value = await fetchActiveApprovals(auth, selectedProject.value, kind)
  }

  function onPending(id) {
    activeApprovals.value = []
    panelApprovalId.value = Number(id) || 0
  }

  async function onPanelClose() {
    panelApprovalId.value = 0
    await refreshActive()
  }

  function resetPanel() {
    panelApprovalId.value = 0
    activeApprovals.value = []
  }

  return {
    panelApprovalId, activeApprovals, activeApproval, bannerText, bannerIcon,
    openPanel, refreshActive, onPending, onPanelClose, resetPanel,
  }
}
