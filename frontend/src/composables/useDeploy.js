import { ref, reactive } from 'vue'
import { useAuth } from './useAuth'
import { useSseStream } from './useSseStream'

export function useDeploy() {
  const auth = useAuth()
  const { output, loading, stream } = useSseStream()

  const projects = ref([])
  const selectedProject = ref('')
  const pipelineData = reactive({ tag: '', iid: '', created: '' })
  const pipelineLoading = ref(true)

  // Tag pagination
  const tagState = reactive({ tags: [], page: 1, total: 0, totalPages: 1, loading: false })
  const selectedTag = ref('')

  async function loadProjects() {
    try {
      const r = await fetch('/api/projects', { headers: auth.A() })
      if (auth.handle401(r)) return
      projects.value = await r.json()
      if (projects.value.length && !selectedProject.value) {
        selectedProject.value = projects.value[0].job_name
      }
    } catch (e) {}
  }

  async function loadPipeline(project) {
    if (!project) return
    pipelineLoading.value = true
    pipelineData.tag = ''
    pipelineData.iid = ''
    pipelineData.created = ''
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(project)}/pipeline`, { headers: auth.A() })
      const d = await r.json()
      pipelineData.tag = d.latest_tag || ''
      pipelineData.iid = d.pipeline?.iid
      pipelineData.created = d.pipeline?.created_at || ''
      // 如果 tag 列表还没有默认值，用 pipeline 最新 tag 兜底
      if (pipelineData.tag && !selectedTag.value) {
        selectedTag.value = pipelineData.tag
      }
    } catch (e) {} finally {
      pipelineLoading.value = false
    }
  }

  async function changeProject(project) {
    selectedProject.value = project
    // 切换项目时清空旧 tag，避免错位
    selectedTag.value = ''
    tagState.tags = []
    await Promise.all([loadPipeline(project), loadTags(project, 0)])
  }

  async function loadTags(project, delta) {
    if (!project) return
    if (project !== tagState._project) {
      tagState._project = project
      tagState.page = 1
    }
    if (delta) tagState.page = Math.max(1, tagState.page + delta)

    tagState.loading = true
    try {
      const r = await fetch(
        `/api/projects/${encodeURIComponent(project)}/tags?page=${tagState.page}&page_size=50`,
        { headers: auth.A() }
      )
      const d = await r.json()
      const items = d.items || []
      tagState.tags = items
      tagState.page = d.page || 1
      tagState.total = d.total || 0
      tagState.totalPages = d.total_pages || 1

      // 优先级: pipelineData.tag > items[0].tag
      if (items.length > 0 && !selectedTag.value) {
        const pipeTag = pipelineData.tag
        const match = pipeTag && items.find(t => t.tag === pipeTag)
        selectedTag.value = match ? match.tag : items[0].tag
      }
    } catch (e) {
      tagState.tags = []
    } finally {
      tagState.loading = false
    }
  }

  function changeTagPage(delta) {
    loadTags(selectedProject.value, delta)
  }

  return {
    projects, selectedProject, pipelineData, pipelineLoading,
    tagState, selectedTag,
    output, loading,
    loadProjects, loadPipeline, changeProject, loadTags, changeTagPage,
    stream
  }
}
