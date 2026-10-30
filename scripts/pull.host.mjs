/**
 * 递归从 app.asar 里抽出 @deepseek-ai 的整条依赖链，写到 node_modules 下，
 * 让离线自测能跑真正的宿主 defineTool。
 *
 * 用法：& "C:\Program Files\nodejs\node.exe" scripts\pull.shim.mjs
 * 判定"够了"的标准：import('@deepseek-ai/dsh-tools') 成功。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

if (process.versions.electron) {
  console.error('必须用独立 Node：& "C:\\Program Files\\nodejs\\node.exe" scripts\\pull.shim.mjs')
  process.exit(2)
}

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG = resolve(HERE, '..')
const ASAR = 'C:/Users/Lenovo/AppData/Local/Programs/DSH Desktop/resources/app.asar'
const OUT = resolve(PKG, 'node_modules')

const buf = readFileSync(ASAR)
const headerSize = buf.readUInt32LE(12)
const header = JSON.parse(buf.subarray(16, 16 + headerSize).toString('utf8'))
const dataStart = 16 + headerSize

function payload(node, rel) {
  const off = dataStart + Number(node.offset)
  const size = Number(node.size)
  const raw = buf.subarray(off, Math.min(off + size + 4096, buf.length))
  const isJson = rel.endsWith('.json')
  let start = 0
  if (isJson) {
    while (start < raw.length && raw[start] !== 0x7b) start++
    let depth = 0, inStr = false, esc = false
    for (let i = start; i < raw.length; i++) {
      const c = raw[i]
      if (inStr) {
        if (esc) esc = false
        else if (c === 0x5c) esc = true
        else if (c === 0x22) inStr = false
        continue
      }
      if (c === 0x22) inStr = true
      else if (c === 0x7b) depth++
      else if (c === 0x7d) { depth--; if (depth === 0) return raw.subarray(start, i + 1) }
    }
    return raw.subarray(start, start + size)
  }
  while (start < raw.length && (raw[start] === 0x0a || raw[start] === 0x0d)) start++
  if (start > 2) start = 0
  let end = Math.max(size, start)
  while (end < raw.length && raw[end] !== 0x0a) end++
  return raw.subarray(start, end + 1)
}

function extract(node, rel) {
  if (node.files) {
    for (const [name, child] of Object.entries(node.files)) extract(child, join(rel, name))
    return
  }
  const target = join(OUT, rel)
  if (existsSync(target)) return
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, payload(node, rel))
}

const NODE_MODULES = header.files.node_modules.files
function pkgNode(name) {
  if (name.startsWith('@deepseek-ai/')) {
    const short = name.slice('@deepseek-ai/'.length)
    const scope = NODE_MODULES['@deepseek-ai']
    return scope && scope.files[short] ? { node: scope.files[short], rel: join('@deepseek-ai', short) } : null
  }
  return NODE_MODULES[name] ? { node: NODE_MODULES[name], rel: name } : null
}

// 广度优先，按 imports 里出现的裸包名递归
const seen = new Set()
const queue = ['@deepseek-ai/dsh-tools']
let pulled = 0
while (queue.length) {
  const name = queue.shift()
  if (seen.has(name)) continue
  seen.add(name)
  const found = pkgNode(name)
  if (!found) { console.log(`missing: ${name}`); continue }
  extract(found.node, found.rel)
  pulled++
  const pj = found.node.files && found.node.files['package.json']
  if (!pj) continue
  let meta
  try { meta = JSON.parse(payload(pj, 'package.json').toString('utf8')) } catch { continue }
  for (const dep of Object.keys(meta.dependencies || {})) queue.push(dep)
  for (const dep of Object.keys(meta.peerDependencies || {})) queue.push(dep)
}
console.log(`抽出 ${pulled} 个包 → ${OUT}`)