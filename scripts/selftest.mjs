/**
 * 离线自测：在不启动 DSH 宿主的前提下，把 lib/index.js 的 apply() 跑在一份假的
 * ctx 上，真调四个工具。
 *
 * 这就是插件能"自证好用"的最小闭环——不然用户要重启 DSH、开一轮对话，才知道
 * 工具注册有没有写错。
 *
 * 用法（在插件包根目录）：
 *     node scripts/selftest.mjs <某个 OCR txt 的绝对路径> [某个 PDF 的绝对路径]
 *
 * 只给 txt 时跑 doctor + scan + map + digest；两个都给时再加跑 ocr 的 page 模式。
 */
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG = resolve(HERE, '..')

// @deepseek-ai/* 是宿主单例，插件自己的 node_modules 里本来不会有它们。
// 自测要跑真 defineTool，所以先用 scripts/pull.host.mjs 把 asar 里的依赖链
// 复制到本地 node_modules（一次性动作），这里再按裸包名解析。
//
// 注意：必须用独立 Node 跑自测（& "C:\Program Files\nodejs\node.exe" ...）。
// DSH 里的 node 是 Electron（ELECTRON_RUN_AS_NODE=1），它的 fs 会把 app.asar
// 内部路径当成归档成员去读，行为和独立 Node 不一致。
let toolsModule
try {
  toolsModule = await import('@deepseek-ai/dsh-tools')
} catch (error) {
  console.error('✗ 解析不到 @deepseek-ai/dsh-tools，先跑一次：')
  console.error('    & "C:\\Program Files\\nodejs\\node.exe" scripts\\pull.host.mjs')
  console.error(`  原始错误：${error.code} ${String(error.message).split('\n')[0]}`)
  process.exit(1)
}

if (typeof toolsModule.defineTool !== 'function') {
  console.error('✗ @deepseek-ai/dsh-tools 里没有 defineTool，抽出来的 shim 不对')
  process.exit(1)
}
console.log('宿主模块：真 defineTool（来自 app.asar 抽取）')

// Windows 上 import() 只接受 file:// URL，裸盘符路径会被当成 c: 协议拒掉。
const { name, inject, apply } = await import(pathToFileURL(resolve(PKG, 'lib', 'index.js')).href)

const registered = new Map()
const progress = []
const fakeCtx = {
  tools: {
    register(def) {
      registered.set(def.name, def)
    },
  },
  emit(event, payload) {
    if (event === 'progress') progress.push(payload)
  },
}

apply(fakeCtx)

console.log(`插件名：${name}`)
console.log(`声明依赖：${JSON.stringify(inject)}`)
console.log(`注册的工具：${[...registered.keys()].join(', ') || '（无）'}`)

const EXPECTED = ['ocr', 'scan', 'map', 'digest']
for (const toolName of EXPECTED) {
  if (!registered.has(toolName)) {
    console.error(`✗ 没有注册 ${toolName} 工具`)
    process.exit(1)
  }
}

// 参数表结构自检：enum 必须是数组
for (const [toolName, tool] of registered) {
  for (const [key, spec] of Object.entries(tool.parameters || {})) {
    if (spec.type === 'string' && spec.enum && !Array.isArray(spec.enum)) {
      console.error(`✗ ${toolName} 的参数 ${key} 的 enum 不是数组`)
      process.exit(1)
    }
  }
}
console.log('参数表：' + EXPECTED.map((t) => `${t}(${Object.keys(registered.get(t).parameters || {}).join('/')})`).join('  '))

// ① 体检测试
const ocrTool = registered.get('ocr')
const doctor = await ocrTool.execute({ mode: 'doctor' })
console.log('\n[ocr doctor]')
console.log(ocrTool.output.render({}, doctor)[0].text)

if (doctor.ok !== true) {
  console.error('\n✗ 环境体检没过，后面的真跑测试跳过')
  process.exit(1)
}

const txt = process.argv[2]
if (!txt) {
  console.log('\n（没给 txt 路径，只跑了 doctor）')
  process.exit(0)
}

/** 跑一个分析工具，打印结果全文。 */
async function runAnalyzer(toolName, args) {
  const tool = registered.get(toolName)
  const value = await tool.execute(args)
  const text = tool.output.render(args, value)[0].text
  console.log(`\n[${toolName}]`)
  console.log(text)
  return value
}

// ② scan（不落盘 = 纯文本报告）
await runAnalyzer('scan', { txt, limitFormulas: 5 })

// ③ scan 带 --out：这条路径走 --json，能拿到结构化字段。用它验证解析链路，
//    顺便给后面 map 的章号过滤提供真实章号。
const scanOut = resolve(PKG, '_selftest_scan.json')
const scanValue = await registered.get('scan').execute({ txt, out: scanOut, limitFormulas: 0 })
if (!Array.isArray(scanValue.chapters) || scanValue.chapters.length === 0) {
  console.error('✗ scan 的 --json 输出里没有 chapters')
  process.exit(1)
}
console.log(`\n[scan --out] ${scanValue.chapters.length} 章，已写入 ${scanOut}`)

// ④ map（按章过滤）
const firstChapter = scanValue.chapters[0]
const mapValue = await runAnalyzer('map', { txt, chapter: String(firstChapter.number) })
if (!mapValue.text) {
  console.error('✗ map 没有输出正文')
  process.exit(1)
}

// ⑤ digest（限一个页区间，避免全长输出）
const startPage = firstChapter.pdf || 1
const digestValue = await registered.get('digest').execute({
  txt,
  from: startPage,
  to: startPage + 12,
})
console.log('\n[digest]')
console.log(String(digestValue.text).split('\n').slice(0, 24).join('\n'))
if (!digestValue.text) {
  console.error('✗ digest 没产出正文')
  process.exit(1)
}

// ⑤ 可选：跑一次真 OCR 单页
const pdf = process.argv[3]
if (pdf) {
  const one = await ocrTool.execute({ mode: 'page', pdf, page: 60, preview: 200 })
  console.log(`\n[ocr page] PDF61：${one.chars} 字，${one.avg_seconds} 秒/页`)
  if (one.chars <= 0) {
    console.error('✗ OCR 一个字都没出')
    process.exit(1)
  }
}

console.log(`\n进度行数：${progress.length}`)
console.log('\n✓ 自测通过')