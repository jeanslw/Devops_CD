/**
 * 审批单与部署页之间的映射工具。
 * - deployPathForType: 审批单 deploy_type → 对应部署页路由
 * - approvalUrl: 拼跳转链接（带 approval/project 查询参数）
 * - fetchActiveApproval: 查询项目下当前用户的活跃单据（pending/approved/deploying）
 */

export function deployPathForType(deployType = '') {
  const dt = (deployType || '').toLowerCase()
  if (dt.startsWith('k8s')) return '/deploy/k8s'
  if (dt === 'compose' || dt === 'docker') return '/deploy/docker'
  return '/deploy/ssh'
}

export function approvalUrl(approval) {
  if (!approval || !approval.id) return ''
  const path = deployPathForType(approval.deploy_type)
  const qs = new URLSearchParams({ approval: String(approval.id) })
  if (approval.project) qs.set('project', approval.project)
  return `${path}?${qs.toString()}`
}

/**
 * 查询某项目下当前用户最需要处理的活跃审批单（1 条）。
 * 后端 active 查询按可操作性排序：deploying（执行中）> approved（待执行）> pending（待审批），
 * 因此较新的待审批单不会挡住较早的已批准单。
 * 必须按部署形态（kind: ssh|compose|k8s）过滤，否则 Docker 页的 compose 单
 * 会串显到 K8s 页（反之亦然）。
 * 后端列表接口已做 requester 隔离：普通用户只返回自己发起的单据。
 * 失败/无单统一返回 null，调用方按"无活跃单"处理。
 */
export async function fetchActiveApproval(auth, project, kind = '') {
  const items = await fetchActiveApprovals(auth, project, kind, 1)
  return items[0] || null
}

/**
 * 查询某项目下当前用户本人发起的全部活跃审批单（待审批/已批准/执行中，最多 limit 条）。
 * mine=1：即使登录者具备审批人/管理者身份，也只返回他自己申请的单据。
 * 后端 active 查询按可操作性排序：deploying（执行中）> approved（待执行）> pending（待审批）。
 * 必须按部署形态（kind: ssh|compose|k8s）过滤，避免跨部署页串显。
 * 失败统一返回 []，调用方按"无活跃单"处理。
 */
export async function fetchActiveApprovals(auth, project, kind = '', limit = 20) {
  if (!project) return []
  const params = new URLSearchParams({
    active: '1',
    mine: '1',
    project,
    page: '1',
    page_size: String(limit),
  })
  if (kind) params.set('deploy_kind', kind)
  try {
    const r = await fetch(`/api/approvals?${params.toString()}`, { headers: auth.A() })
    if (auth.handle401(r) || !r.ok) return []
    const d = await r.json()
    return d.items || []
  } catch (e) {
    return []
  }
}
