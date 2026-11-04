import { ref } from 'vue'

// 展示时区:由 /api/info 下发的 settings.display_timezone 填充;为空则按浏览器本地时间.
// 存储层统一为 UTC,展示与定时输入换算均按同一时区.
const displayTimezone = ref('')

export function setDisplayTimezone(tz) {
  displayTimezone.value = tz || ''
}

// 把后端返回的 UTC 字符串渲染为展示时区(默认浏览器本地)时间.
// 后端存的是 UTC 墙钟(如 '2026-09-15 02:00:00'),前端按用户时区展示('2026-09-15 10:00').
export function toLocalDisplay(utcStr) {
  if (!utcStr) return '-'
  const s = utcStr + ''
  // 归一为带 Z 的 ISO,让 JS 按 UTC 解析(否则 new Date 会把无时区串当本地时间)
  const normalized = s.replace(' ', 'T') + (s.includes('Z') ? '' : 'Z')
  const d = new Date(normalized)
  if (isNaN(d.getTime())) return s.slice(0, 16)
  const pad = n => String(n).padStart(2, '0')
  const tz = displayTimezone.value
  if (tz) {
    try {
      return new Intl.DateTimeFormat('sv-SE', {
        timeZone: tz,
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(d).replace('T', ' ')
    } catch (e) {
      // 非法时区回退浏览器本地
    }
  }
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 把指定时区的墙钟时间换算为 UTC 时刻;tz 为空或非法时回退浏览器本地时间.
function wallClockToUtc(y, mo, d, h, mi, tz) {
  if (!tz) return new Date(y, mo - 1, d, h, mi, 0, 0)
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    })
    const tzWallAsUtc = ts => {
      const parts = fmt.formatToParts(new Date(ts))
      const get = t => Number(parts.find(p => p.type === t).value)
      return Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second'))
    }
    const wall = Date.UTC(y, mo - 1, d, h, mi, 0)
    const first = wall - (tzWallAsUtc(wall) - wall)
    const offset = tzWallAsUtc(first) - first
    return new Date(wall - offset)
  } catch (e) {
    return new Date(y, mo - 1, d, h, mi, 0, 0)
  }
}

// 把选择器选出的墙钟时间按展示时区换算为 UTC 墙钟串再提交.
export function localInputToUtc(localStr) {
  if (!localStr) return ''
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(localStr)
  if (!m) return localStr
  const [, y, mo, d, h, mi] = m
  const dt = wallClockToUtc(Number(y), Number(mo), Number(d), Number(h), Number(mi), displayTimezone.value)
  const pad = n => String(n).padStart(2, '0')
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())} ${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:00`
}

// 把存储的 UTC 串回填到 DateTimePicker(展示为本地/配置时区),便于编辑既有定时.
export function utcToLocalInput(utcStr) {
  const disp = toLocalDisplay(utcStr)
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(disp)
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : ''
}
