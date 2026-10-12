// ilk2 gate reporter for vitest: one JSON report of test ids, states and error messages.
// vitest's built-in JSON reporter (3.2) replaces a timeout's message with "STACK_TRACE_ERROR",
// so timeout-class failures could not be recognized (docs/02 §2.5.5, S21). This reads the
// task results directly. Output path: --outputFile.
import { writeFileSync } from 'node:fs'
import { relative } from 'node:path'

export default class Ilk2Reporter {
  onInit(ctx) { this.ctx = ctx }

  onFinished(files = [], errors = []) {
    const root = this.ctx?.config?.root || process.cwd()
    let out = this.ctx?.config?.outputFile
    if (out && typeof out === 'object') out = out['ilk2'] || Object.values(out)[0]
    const report = { tests: [], collect_errors: [], unhandled: (errors || []).map(e => String(e?.message ?? e)) }
    const msg = (r) => ((r && r.errors) || []).map(e => `${e.name || 'Error'}: ${e.message || ''}`).join('\n')
    const frames = (r) => {
      const st = ((r && r.errors) || [])[0]?.stack || ''
      return st.split('\n').slice(1).map(l => {
        const m = l.match(/\(?((?:file:\/\/)?[^\s()]+):(\d+):\d+\)?\s*$/)
        return m ? { path: m[1].replace('file://', ''), lineno: Number(m[2]), message: '' } : null
      }).filter(Boolean).filter(f => !f.path.includes('node_modules')).slice(0, 5)
    }
    for (const file of files) {
      const rel = relative(root, file.filepath || file.name)
      const tests = []
      const walk = (task, path) => {
        if (task.type === 'test' || task.type === 'custom') {
          const r = task.result || {}
          tests.push({
            nodeid: `${rel}::${[...path, task.name].join(' > ')}`,
            outcome: r.state === 'fail' ? 'failed' : r.state === 'pass' ? 'passed' : 'skipped',
            message: msg(r), type: ((r.errors || [])[0] || {}).name || null, frames: frames(r),
            duration: (r.duration || 0) / 1000,
          })
          return
        }
        for (const t of task.tasks || []) walk(t, task === file ? path : [...path, task.name])
      }
      walk(file, [])
      if (file.result?.state === 'fail' && tests.length === 0) {
        report.collect_errors.push({ nodeid: rel, message: msg(file.result), type: 'CollectError', frames: [] })
      }
      report.tests.push(...tests)
    }
    report.collected = report.tests.length
    if (out) writeFileSync(out, JSON.stringify(report))
  }
}
