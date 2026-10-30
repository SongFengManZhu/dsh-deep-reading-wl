/**
 * dsh-deep-reading-wl —— 扫描版教材的「读之前先看清地形」工具。
 *
 * 这个插件把一条完整流水线包成四个动作，每个动作是独立的工具，串起来用：
 *
 *   ocr    PDF → txt        没有文字层的扫描件，先把它变成字
 *   scan   txt → 骨架报告    几章几节、每章多厚、哪几节承重、哪几页有表
 *   map    txt → 阅读清单    这一章要付出多少代价，先读哪、后读哪、哪里略读
 *   digest txt → 更短的原文  把 OCR 碎片压掉，只留值得亲读的正文
 *
 * 为什么是「分诊台」而不是「一键出笔记」：真正写心得时花时间的判断——这章主线
 * 是什么、哪几处在立论、哪里可以略读——现在还得人来做。这四个工具负责把「读
 * 多少」变成「读哪几页」，但不替你读。硬把最后一步也自动化，只会把活的判断
 * 压成填空题。
 *
 * 为什么四个工具而不是一个带 mode 参数的大工具：宿主把每个工具当成一个明确的
 * 动作暴露给模型，四个动作的语义差别足够大（跑 OCR / 看结构 / 排优先级 / 压缩
 * 原文），合成一个只会让参数表变成灰色地带。
 *
 * @module dsh-deep-reading-wl
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dsh-deep-reading-wl'
export const inject = ['tools']

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG_ROOT = resolve(HERE, '..')
const PY_DIR = join(PKG_ROOT, 'python')

const SCRIPTS = {
  ocr: join(PY_DIR, 'fastocr.py'),
  scan: join(PY_DIR, 'scan.py'),
  map: join(PY_DIR, 'map.py'),
  digest: join(PY_DIR, 'digest.py'),
}

/** OCR 单次调用允许的最长挂钟时间。全书 700 页约 15 分钟，留三倍余量。 */
const OCR_TIMEOUT_MS = 45 * 60 * 1000

/** 三个分析脚本都在秒级，给足一分钟防止冷启卡住。 */
const ANALYZE_TIMEOUT_MS = 5 * 60 * 1000

/**
 * 已知的 Python 解释器位置，按优先级排列。
 *
 * 前两个是这台机器上真实存在的位置：DSH Desktop 自带一个装了 numpy / Pillow /
 * pymupdf / rapidocr / openvino 的运行时。`DEEP_READING_PYTHON` 优先级最高，
 * 换机器时不必改代码；保留 `FASTOCR_PYTHON` 是为了让老的环境变量继续有效。
 */
const PYTHON_CANDIDATES = [
  process.env.DEEP_READING_PYTHON,
  process.env.FASTOCR_PYTHON,
  join(
    process.env.LOCALAPPDATA || '',
    'Programs',
    'DSH Desktop',
    'resources',
    'office-runtime',
    'primary-runtime',
    'dependencies',
    'python',
    'python.exe',
  ),
  'C:\\Python312\\python.exe',
  'C:\\Python311\\python.exe',
]

/** 引擎需要的 Python 侧依赖。缺任何一个都只会得到一个看不懂的 ImportError。 */
const REQUIRED_MODULES = ['pymupdf', 'rapidocr', 'openvino', 'numpy']

function resolvePython() {
  for (const candidate of PYTHON_CANDIDATES) {
    if (!candidate) continue
    try {
      if (existsSync(candidate) && statSync(candidate).isFile()) return candidate
    } catch {
      // 权限不足或路径非法，继续找下一个
    }
  }
  return null
}

/** 把 spawn 的 stdout 按行切开，逐行回调。半行留到下一次。 */
function readLines(stream, onLine) {
  let buffer = ''
  stream.setEncoding('utf8')
  stream.on('data', (chunk) => {
    buffer += chunk
    let index
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line.trim()) onLine(line)
    }
  })
  stream.on('end', () => {
    const rest = buffer.trim()
    if (rest) onLine(rest)
  })
}

/**
 * 通用的一次子进程调用：拿最后一行 JSON 当结果。
 *
 * 为什么要 `--json` 而不是解析人类可读文本：文本会因为改措辞而变，JSON 不会。
 * 为什么只认「最后一个以 { 开头的行」：脚本会先打一堆人读的进度/标题，最后才
 * 吐汇总；中途的任何 { 多半是正文里被蒸出来的括号，不能当结果。
 */
function runJson({ python, script, argv, timeoutMs, onProgress }) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(python, [script, ...argv], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })

    let lastJson = null
    let stderr = ''
    let settled = false

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      child.kill()
      reject(new Error(`执行超时（${Math.round(timeoutMs / 60000)} 分钟）已终止：${script}`))
    }, timeoutMs)

    readLines(child.stdout, (line) => {
      const trimmed = line.trim()
      if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        try {
          lastJson = JSON.parse(trimmed)
          return
        } catch {
          // 不是合法 JSON 的汇总行，按进度行处理
        }
      }
      onProgress?.(trimmed)
    })

    readLines(child.stderr, (line) => {
      stderr += line + '\n'
      onProgress?.(line.trim())
    })

    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`无法启动 Python（${python}）：${error.message}`))
    })

    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (lastJson) {
        resolvePromise(lastJson)
        return
      }
      const tail = stderr.trim().split('\n').slice(-12).join('\n')
      reject(new Error(`脚本执行失败（退出码 ${code}）：${script}\n${tail || '没有输出'}`))
    })
  })
}

/** 探测环境：Python 是否在、依赖是否齐。 */
function probe({ python, pdf }) {
  return new Promise((resolvePromise) => {
    const code = [
      'import json,sys',
      'info={"python":sys.version.split()[0]}',
      // 模块名列表要字面写进生成的代码里。曾经想用 JSON.stringify 拼，结果产生的是
      // 双引号 JSON 而不是合法的 Python 字面量，整个 -c 直接语法错、一行输出都没有，
      // 于是体检报"依赖全缺"。别再用字符串拼代码。
      'for m in ["pymupdf","rapidocr","openvino","numpy"]:',
      '    try:',
      '        __import__(m); info[m]=True',
      '    except Exception as e:',
      '        info[m]=str(e)[:120]',
      'print(json.dumps(info))',
    ].join('\n')

    const child = spawn(python, ['-c', code], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let out = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (c) => (out += c))
    child.on('error', () => resolvePromise({ python, ok: false, error: '启动失败' }))
    child.on('close', () => {
      try {
        const info = JSON.parse(out.trim().split('\n').pop())
        const missing = REQUIRED_MODULES.filter((m) => info[m] !== true)
        resolvePromise({
          ...info,
          pdf_exists: Boolean(pdf && existsSync(pdf)),
          ok: missing.length === 0,
          missing,
        })
      } catch {
        resolvePromise({ python, ok: false, error: '探测输出无法解析', raw: out.slice(0, 200) })
      }
    })
  })
}

/**
 * 剥掉逐页明细，只留汇总。
 *
 * `pages` 数组是逐页正文，一整本 624 页的书序列化后约 3.4 MB。整包交给宿主会
 * 撞上它的无损 JSON 校验（实测报 `value is not lossless JSON`），而模型这边
 * 也用不上——正文应该走 out_file 落盘，或者用预览截取开头。
 */
const OCR_SUMMARY_KEYS = [
  'pdf',
  'start',
  'end',
  'page_count',
  'pages_done',
  'pages_ocr',
  'pages_skipped',
  'chars',
  'seconds',
  'avg_seconds',
  'out_file',
]

function decouplePages(result) {
  const summary = {}
  for (const key of OCR_SUMMARY_KEYS) {
    if (result[key] !== undefined) summary[key] = result[key]
  }
  // 页明细单独挂在不可枚举属性上：工具返回值只看可枚举字段，写盘分支能读到。
  Object.defineProperty(summary, 'pages', {
    value: Array.isArray(result.pages) ? result.pages : [],
    enumerable: false,
    configurable: true,
  })
  return summary
}

/** 把用户给的路径解析成绝对路径，顺便报个像样的错。 */
function requireFile(rawPath, label) {
  const text = String(rawPath || '').trim()
  if (!text) throw new Error(`缺少 ${label} 参数：给一个绝对路径。`)
  const absolute = isAbsolute(text) ? text : resolve(text)
  if (!existsSync(absolute)) throw new Error(`${label} 不存在：${absolute}`)
  return absolute
}

/** 可选的输出路径：给了解析成绝对路径，没给返回 null。 */
function optionalTarget(rawPath) {
  const text = String(rawPath || '').trim()
  if (!text) return null
  return isAbsolute(text) ? text : resolve(text)
}

/**
 * 把脚本写出的 JSON 文件读回来。
 *
 * 为什么要从文件读而不是从 stdout 收：三个分析脚本在给了 --out 之后，结果只
 * 落文件，stdout 只剩「写入 X」一行提示（实测）。这不是 bug，是它们本来的
 * 契约——人类看文本，机器看文件。所以壳子这边必须会读文件。
 */
function readJsonFile(path) {
  if (!existsSync(path)) throw new Error(`脚本没有写出结果文件：${path}`)
  const text = readFileSync(path, 'utf8')
  try {
    return JSON.parse(text)
  } catch (error) {
    throw new Error(`结果文件不是合法 JSON：${path}\n${String(error.message).slice(0, 200)}`)
  }
}

/** 进度顺手进事件流（宿主有 ctx.emit 就发，没有就算了）。 */
function makeProgress(ctx) {
  return (line) => {
    try {
      ctx.emit?.('progress', line)
    } catch {
      // 事件总线是可选的，不能因为它在不在改变主流程
    }
  }
}

// ── 文本渲染 ──────────────────────────────────────────────────────────────

function renderOcr(value) {
  const lines = []
  lines.push(
    `OCR 完成：${value.pages_done} 页（识别 ${value.pages_ocr}，跳过残页 ${value.pages_skipped}），共 ${value.chars} 字`,
  )
  lines.push(
    `耗时 ${value.seconds} 秒，均 ${value.avg_seconds} 秒/页（页码范围 PDF${value.start + 1}–PDF${value.end}，全书 ${value.page_count} 页）`,
  )
  if (value.out_file) lines.push(`文本已写入：${value.out_file}`)
  if (value.head) {
    lines.push('')
    lines.push('开头预览：')
    lines.push(value.head)
  }
  return lines.join('\n')
}

function renderDoctor(value) {
  const lines = []
  lines.push(`Python：${value.python || '未找到'}`)
  if (value.python_version) lines.push(`版本：${value.python_version}`)
  lines.push(value.ok ? '依赖：齐全' : `依赖：缺 ${(value.missing || []).join('、')}`)
  lines.push(`脚本：${value.scripts_ok ? '四个都在' : `缺失 ${(value.scripts_missing || []).join('、')}`}`)
  lines.push('')
  lines.push(value.ok ? '可以用了。' : '先补依赖再跑：pip install pymupdf rapidocr openvino numpy')
  return lines.join('\n')
}

/**
 * scan/map/digest 的文本渲染交给脚本自己。
 *
 * 为什么不在 JS 里重排：三个脚本的中文报告排版是反复调过的（对齐宽度、缩进、
 * 「承重墙」这类字段名），在 Node 侧重写一遍只会产生第二份维护负担，而脚本本来
 * 就有 `--out` 和不带 `--json` 的纯文本模式。所以惯例是：不给 --json 时脚本
 * 直接吐人读的文本，JS 只负责把它原样交出去。
 */
function renderRawText(value) {
  return value.text || '（没有结果）'
}

// ── 工具定义 ──────────────────────────────────────────────────────────────

export function apply(ctx) {
  const onProgress = makeProgress(ctx)

  /**
   * ocr —— 扫描版 PDF 极速转文字。
   *
   * 保留 mode 参数（run/doctor/page）而不拆成三个工具，因为这三者是同一个动作
   * 的不同强度：检查环境、试读一页、整段跑。拆开会让模型在「先体检还是先试读」
   * 上纠结，而实际顺序永远是 formal 体检 → 试读 → 全量。
   */
  ctx.tools.register(
    defineTool({
      name: 'ocr',
      description:
        '用本地 RapidOCR + OpenVINO 引擎对扫描版 PDF 做 OCR，实测约 1.2 秒/页，比同机 ONNXRuntime 方案快 4–5 倍。' +
        '适用于没有文字层的扫描件（用 PyMuPDF 取字数为 0 的 PDF）。' +
        '三种模式：run = OCR 一个页区间并可选写入 txt；doctor = 检查环境；page = 只跑一页试读。' +
        '页号 0 基（与 PyMuPDF 一致）。自动跳过扫描残页（墨迹极少且只在上半页）。' +
        '产物是带 "===== 名 PDF{页} =====" 页标记的 txt，可直接喂给 scan / map / digest。',
      parameters: {
        mode: {
          type: 'string',
          enum: ['run', 'doctor', 'page'],
          description: 'run = 批量 OCR 页区间（默认）；doctor = 只体检不跑；page = 只 OCR 一页试读。',
        },
        pdf: { type: 'string', description: 'PDF 文件绝对路径。mode=doctor 时可省略。' },
        start: { type: 'number', description: '起始页，0 基，含端点。默认 0。' },
        end: { type: 'number', description: '结束页，0 基，不含端点。不填则跑到最后一页。' },
        page: { type: 'number', description: 'mode=page 时的单页页号，0 基。' },
        out: { type: 'string', description: '把 OCR 文本写入这个 txt 路径。不给则只返回统计与预览。' },
        dpi: { type: 'number', description: '渲染 dpi，默认 120。加 dpi 只多出很少的字，却明显变慢。' },
        threads: { type: 'number', description: '推理线程数，默认 16。要单进程多线程，不要多进程。' },
        preview: { type: 'number', description: '返回前多少字符的文本预览，默认 1200。给 0 则不返回预览。' },
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [
          { type: 'text', text: value.mode === 'doctor' ? renderDoctor(value) : renderOcr(value) },
        ],
      },
      execute: async (args) => {
        const mode = String(args.mode ?? 'run')
        const python = resolvePython()
        const scriptOk = existsSync(SCRIPTS.ocr)

        if (mode === 'doctor') {
          const allOk = Object.values(SCRIPTS).every((p) => existsSync(p))
          if (!python) {
            return {
              mode: 'doctor',
              ok: false,
              python: null,
              scripts_ok: allOk,
              scripts_missing: Object.entries(SCRIPTS)
                .filter(([, p]) => !existsSync(p))
                .map(([k]) => k),
              message: '没找到 Python 解释器。设置 DEEP_READING_PYTHON 环境变量指向 python.exe。',
            }
          }
          const info = await probe({ python, pdf: args.pdf })
          return {
            mode: 'doctor',
            ok: info.ok === true,
            python,
            python_version: info.python,
            missing: info.missing || [],
            scripts_ok: allOk,
            scripts_missing: Object.entries(SCRIPTS)
              .filter(([, p]) => !existsSync(p))
              .map(([k]) => k),
          }
        }

        if (!scriptOk) throw new Error(`插件文件缺失，重装 dsh-deep-reading-wl：${SCRIPTS.ocr}`)
        if (!python) {
          throw new Error(
            '没找到 Python 解释器。设置环境变量 DEEP_READING_PYTHON 指向 python.exe，然后重试；' +
              '或者用 mode=doctor 看看缺什么。',
          )
        }

        const absolute = requireFile(args.pdf, 'pdf')
        const start = mode === 'page' ? Number(args.page ?? 0) : Number(args.start ?? 0)
        const end = mode === 'page' ? start + 1 : args.end === undefined ? 1e9 : Number(args.end)

        const raw = await runJson({
          python,
          script: SCRIPTS.ocr,
          argv: [
            absolute,
            '--json',
            '--start',
            String(Math.max(0, Math.floor(start))),
            '--end',
            String(Math.max(0, Math.floor(end))),
            '--dpi',
            String(Math.floor(Number(args.dpi ?? 120))),
            '--threads',
            String(Math.floor(Number(args.threads ?? 16))),
          ],
          timeoutMs: OCR_TIMEOUT_MS,
          onProgress,
        })

        const result = decouplePages(raw)
        const pages = result.pages ?? []
        const previewLimit = Number(args.preview ?? 1200)
        const head =
          previewLimit > 0
            ? pages
                .filter((p) => !p.skipped)
                .map((p) => p.text)
                .join('\n')
                .slice(0, previewLimit)
            : undefined

        const summary = {
          mode: 'run',
          pdf: result.pdf,
          start: result.start,
          end: result.end,
          page_count: result.page_count,
          pages_done: result.pages_done,
          pages_ocr: result.pages_ocr,
          pages_skipped: result.pages_skipped,
          chars: result.chars,
          seconds: result.seconds,
          avg_seconds: result.avg_seconds,
          out_file: result.out_file,
          head,
        }

        const target = optionalTarget(args.out)
        if (target) {
          // 插件侧也写一份：脚本只在有 --out 时写文件，而这里保证「要了文件就有文件」。
          const { writeFileSync, mkdirSync } = await import('node:fs')
          mkdirSync(dirname(target), { recursive: true })
          const body = pages
            .map((p) => {
              const header = `\n===== ${absolute.split(/[\\/]/).pop()} PDF${p.page_label} =====\n`
              return header + (p.skipped ? `[扫描残页，墨迹 ${p.ink}，已跳过]` : p.text)
            })
            .join('\n')
          writeFileSync(target, body, 'utf8')
          summary.out_file = target
        }

        return summary
      },
    }),
  )

  /** 三个分析脚本共用的壳：参数形状几乎一样，差别只在 argv 和超时。 */
  function registerAnalyzer({ name: toolName, description, script, parameters, buildArgv, hint }) {
    ctx.tools.register(
      defineTool({
        name: toolName,
        description,
        parameters,
        output: {
          schema: { type: 'object', additionalProperties: true },
          render: (_args, value) => [{ type: 'text', text: renderRawText(value) }],
        },
        execute: async (args) => {
          const python = resolvePython()
          const target = SCRIPTS[script]
          if (!existsSync(target)) {
            throw new Error(`插件文件缺失，重装 dsh-deep-reading-wl：${target}`)
          }
          if (!python) {
            throw new Error(
              '没找到 Python 解释器。设置环境变量 DEEP_READING_PYTHON 指向 python.exe，然后重试。',
            )
          }

          const txt = requireFile(args.txt, 'txt')
          const out = optionalTarget(args.out)
          const argv = [txt, ...buildArgv(args)]

          // 三条路径，按脚本的真实行为分：
          //
          // 1) 给了 --out：脚本把结果（--json 时是 JSON，否则是文本）写进文件，
          //    **stdout 一个字都不吐**（实测：`--json --out X` 只打印「写入 X」那
          //    一行）。所以这里既要 --json 又要读文件，不能指望 stdout。
          // 2) 没给 --out：脚本把排好版的中文报告打到 stdout，原样转交即可。
          //    排版（对齐宽度、缩进、「承重墙」这类字段名）是在 Python 里反复调
          //    过的，在 Node 侧重写只会多一份维护负担。
          if (out) {
            await runPlainText({
              python,
              script: target,
              argv: [...argv, '--out', out, '--json'],
              onProgress,
            })
            const raw = readJsonFile(out)
            return { txt, out_file: out, ...raw, text: `结果已写入：${out}\n\n${hint}` }
          }

          const text = await runPlainText({ python, script: target, argv, onProgress })
          return { txt, out_file: null, text: text || '（脚本没有输出正文）' }
        },
      }),
    )
  }

  registerAnalyzer({
    name: 'scan',
    script: 'scan',
    hint: '打开这个文件看骨架报告：几章几节、每章多厚、哪几节承重、哪几页有表。',
    description:
      '扫描一本教材 OCR 出的 txt，输出「骨架 + 失真地图」：几章几节、每章起止页与篇幅、' +
      '哪几节公式/定义/推导最密（承重墙）、哪几页有数据表（只标位置，行列须回原图看）、' +
      '哪些行 OCR 已损坏。速度约 0.1 秒/本。' +
      '这是读之前的第一站：先知道这本书有多大、哪里重、哪里 OCR 已经不可信，再决定怎么读。',
    parameters: {
      txt: { type: 'string', description: 'OCR 产出的 txt 绝对路径（ocr 工具的产物即可）。' },
      out: { type: 'string', description: '把报告写入这个文件。不给则直接在返回里给全文。' },
      section: { type: 'string', description: '只看某一节（只影响文本输出），如「11.5」。' },
      limitFormulas: {
        type: 'number',
        description: '文本输出里列多少条公式，默认 40。给 0 则一条都不列。',
      },
    },
    buildArgv: (args) => {
      const argv = []
      if (args.section !== undefined && args.section !== null && String(args.section).trim()) {
        argv.push('--section', String(args.section).trim())
      }
      if (args.limitFormulas !== undefined && args.limitFormulas !== null) {
        argv.push('--limit-formulas', String(Math.max(0, Math.floor(Number(args.limitFormulas)))))
      }
      return argv
    },
  })

  registerAnalyzer({
    name: 'map',
    script: 'map',
    hint: '打开这个文件看阅读清单：重心节排名、损坏表页码、可略读的节。',
    description:
      '把 scan 的结论翻译成一组具体动作：这一章每个节有多重（承重密度 = 公式+定义+推导密度）、' +
      '先读哪几节、哪几节可以略读、哪几张表的数据必须回原图取、全章是否该按节拆读。' +
      '按章过滤（chapter 参数）时也给出这一章的篇幅与节数。' +
      '用它决定「这一章花多少时间」，而不是决定「这一章讲了什么」。',
    parameters: {
      txt: { type: 'string', description: 'OCR 产出的 txt 绝对路径。' },
      chapter: {
        type: 'string',
        description: '只看这一章：章号（如「十一」或「11」）或章标题片段（如「统计热力学」）。',
      },
      out: { type: 'string', description: '把报告写入这个文件。不给则直接在返回里给全文。' },
    },
    buildArgv: (args) => {
      const argv = []
      const chapter = String(args.chapter ?? '').trim()
      if (chapter) argv.push('--chapter', chapter)
      return argv
    },
  })

  registerAnalyzer({
    name: 'digest',
    script: 'digest',
    hint: '打开这个文件读压缩后的正文：表格碎片已折成一行占位，节标题与公式编号原样保留。',
    description:
      '把 OCR 出的 txt 压成一份更短的原文（通常保留 87%）：逐行分诊为正文/公式/表格碎片/噪声，' +
      '表格碎片折成一行「[表 X 在此，N 行已省略]」占位，节标题、公式编号、定义句原样保留。' +
      '输出不是摘要，是删掉废话之后的原文——用来喂给真正逐行读的那一步，省 token。' +
      '可按页区间压缩（from / to，1 基 PDF 页）。',
    parameters: {
      txt: { type: 'string', description: 'OCR 产出的 txt 绝对路径。' },
      from: { type: 'number', description: '起始 PDF 页（含），1 基。不给则从头。' },
      to: { type: 'number', description: '结束 PDF 页（不含），1 基。不给则到末尾。' },
      out: { type: 'string', description: '把压缩后的正文写入这个文件。不给则直接在返回里给全文。' },
    },
    buildArgv: (args) => {
      const argv = []
      if (args.from !== undefined && args.from !== null) {
        argv.push('--from', String(Math.max(1, Math.floor(Number(args.from)))))
      }
      if (args.to !== undefined && args.to !== null) {
        argv.push('--to', String(Math.max(1, Math.floor(Number(args.to)))))
      }
      return argv
    },
  })
}

/**
 * 跑一次纯文本模式（不带 --json），拿回脚本排好版的中文报告。
 *
 * 单独一个函数而不是给 runJson 加开关：runJson 的契约是「最后一行必须是 JSON」，
 * 混进一个「可能没有 JSON」的模式会让它的错误处理含糊。
 */
function runPlainText({ python, script, argv, onProgress }) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(python, [script, ...argv], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    let stderr = ''
    let settled = false

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      child.kill()
      reject(new Error(`执行超时已终止：${script}`))
    }, ANALYZE_TIMEOUT_MS)

    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      out += chunk
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      stderr += chunk
      const text = String(chunk).trim()
      if (text) onProgress?.(text)
    })

    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`无法启动 Python（${python}）：${error.message}`))
    })

    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (code === 0) {
        resolvePromise(out.trim())
        return
      }
      const tail = stderr.trim().split('\n').slice(-12).join('\n')
      reject(new Error(`脚本执行失败（退出码 ${code}）：${script}\n${tail || '没有输出'}`))
    })
  })
}