// Captures browser-side console output and uncaught errors into an in-memory
// ring buffer for the whole session, so an admin can include it in the
// diagnostics download (Admin.tsx) alongside the backend/nginx logs -- the
// only source of the three that can ever exist client-side. Capture runs for
// every session (nothing leaves the browser on its own); only the download
// action itself is admin-gated.

const MAX_ENTRIES = 1000
const buffer: string[] = []

function push(line: string): void {
  buffer.push(line)
  if (buffer.length > MAX_ENTRIES) buffer.shift()
}

function formatArg(arg: unknown): string {
  if (typeof arg === 'string') return arg
  if (arg instanceof Error) return arg.stack ?? `${arg.name}: ${arg.message}`
  try {
    return JSON.stringify(arg)
  } catch {
    return String(arg)
  }
}

function record(level: string, args: unknown[]): void {
  const line = `[${new Date().toISOString()}] [${level}] ${args.map(formatArg).join(' ')}`
  push(line)
}

let installed = false

export function installConsoleCapture(): void {
  if (installed) return
  installed = true

  const original = {
    log: console.log.bind(console),
    info: console.info.bind(console),
    warn: console.warn.bind(console),
    error: console.error.bind(console),
  }

  console.log = (...args: unknown[]) => { record('LOG', args); original.log(...args) }
  console.info = (...args: unknown[]) => { record('INFO', args); original.info(...args) }
  console.warn = (...args: unknown[]) => { record('WARN', args); original.warn(...args) }
  console.error = (...args: unknown[]) => { record('ERROR', args); original.error(...args) }

  window.addEventListener('error', (event) => {
    record('UNCAUGHT', [event.error ?? event.message])
  })
  window.addEventListener('unhandledrejection', (event) => {
    record('UNHANDLED_REJECTION', [event.reason])
  })
}

export function getConsoleLogText(): string {
  return buffer.join('\n')
}
