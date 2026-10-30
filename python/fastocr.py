# -*- coding: utf-8 -*-
"""fastocr —— 扫描版 PDF 极速 OCR（RapidOCR v3 + OpenVINO）。

由 `dsh-deep-reading-wl` 插件的 ocr 模式调用，也可单独在命令行用。

设计要点（每一条都是实测结论，不要凭直觉改）：

1. **单进程 + 多线程**。历史上把 OMP_NUM_THREADS 压到 2 又开 8 个进程，
   实测 150–275 秒/页；单进程 16 线程是 1.24 秒/页。差距 90 倍不是算法，
   是自伤配置。绝不要为了"并行"而多开进程——rec 阶段 8 线程就已饱和。
2. **OpenVINO 而不是 ONNXRuntime**，同一台机器同一批页面快 4–5 倍。
3. **120 dpi** 就够。150 dpi 只多几个字（782→865 字/页），却慢 10%。
   det 的 limit_side_len 固定 960，再往上加只耗时不出字。
4. **残页跳过**。整页只有一小条墨迹（扫描件把上一页尾巴扫进来）时，跳过
   OCR，直接用墨迹几何判断，省掉一次无效推理。
5. **按纵坐标还原阅读顺序**。找框顺序不是阅读顺序，必须按 box 重排。

用法：
    python fastocr.py <pdf> [--start N] [--end M] [--out FILE] [--json]
                      [--dpi 120] [--threads 16] [--det-side 960]
                      [--keep-partial] [--only-page N]

参数里的页号都是 0 基（和 PyMuPDF 一致）。输出里写的是 1 基，跟人说话一致。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 必须在 import numpy / onnxruntime / openvino 之前设置，晚一步就不生效。
_THREADS = int(os.environ.get("OCR_THREADS", "16"))
os.environ.setdefault("OMP_NUM_THREADS", str(_THREADS))
os.environ.setdefault("KMP_BLOCKTIME", "1")

import pymupdf  # noqa: E402
from rapidocr import EngineType, RapidOCR  # noqa: E402

DPI = int(os.environ.get("OCR_DPI", "120"))
DET_SIDE = int(os.environ.get("OCR_DET_SIDE", "960"))

# 残页判据：墨迹比例低于这个值，且墨迹只落在页面上半部分。
PARTIAL_INK = 0.02
PARTIAL_LAST_ROW = 0.45


def make_engine(threads: int, det_side: int) -> RapidOCR:
    """引擎参数必须用枚举对象，写成字符串会报 must be Enum Type。"""
    params = {
        "Global.log_level": "error",
        "Det.engine_type": EngineType.OPENVINO,
        "Rec.engine_type": EngineType.OPENVINO,
        "Cls.engine_type": EngineType.OPENVINO,
        "EngineConfig.openvino.inference_num_threads": threads,
        "Det.limit_side_len": det_side,
    }
    return RapidOCR(params=params)


def ink_run(pix):
    """返回 (墨迹比例, 有墨迹的最后一行 y)。用于判断扫描残页与空白页。

    只数"深色且低饱和"的像素，避开蓝色标题条和浅色底纹。
    """
    import numpy as np

    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    a = a[:, :, :3].astype(np.int16)
    mask = (a.min(axis=2) < 170) & ((a.max(axis=2) - a.min(axis=2)) < 45)
    rows = np.where(mask.sum(axis=1) > 3)[0]
    last = int(rows[-1]) if len(rows) else 0
    return float(mask.mean()), last


def is_partial(pix, frac: float, last: int) -> bool:
    return frac < PARTIAL_INK and last < pix.height * PARTIAL_LAST_ROW


def read_page(eng: RapidOCR, doc, index: int, dpi: int, keep_partial: bool = False):
    """OCR 一页。返回 dict：text / lines / n_boxes / partial / elapsed。"""
    t0 = time.time()
    page = doc[index]
    pix = page.get_pixmap(dpi=dpi)

    frac, last = ink_run(pix)
    partial = is_partial(pix, frac, last)

    if partial and not keep_partial:
        # 残页：不是 OCR 失败，是这一页本来就没有内容。要记清楚这个区别，
        # 否则下游会把"跳过"当成"识别不出"。
        return {
            "page_index": index,
            "page_label": index + 1,
            "partial": True,
            "skipped": True,
            "ink": round(frac, 4),
            "lines": [],
            "text": "",
            "n_boxes": 0,
            "elapsed": round(time.time() - t0, 2),
        }

    res = eng(pix.tobytes("png"))
    items = []
    if res is not None and res.boxes is not None:
        for box, text in zip(res.boxes, res.txts):
            items.append((min(p[1] for p in box), min(p[0] for p in box), text))
    # 按纵坐标分桶再按横坐标排：还原阅读顺序，容忍轻微的行内抖动。
    items.sort(key=lambda x: (round(x[0] / 12), x[1]))
    lines = [t for _, _, t in items if t]

    return {
        "page_index": index,
        "page_label": index + 1,
        "partial": partial,
        "skipped": False,
        "ink": round(frac, 4),
        "lines": lines,
        "text": "\n".join(lines),
        "n_boxes": len(items),
        "elapsed": round(time.time() - t0, 2),
    }


def page_header(index: int, source: str) -> str:
    return "\n===== %s PDF%d =====\n" % (source, index + 1)


def ocr_range(
    pdf_path: str,
    start: int = 0,
    end: int | None = None,
    dpi: int = DPI,
    threads: int = _THREADS,
    det_side: int = DET_SIDE,
    out_file: str | None = None,
    progress=None,
) -> dict:
    """OCR [start, end) 区间，返回汇总 dict。进度通过 progress(page_result, state) 回传。"""
    doc = pymupdf.open(pdf_path)
    try:
        total = doc.page_count
        end = total if end is None else min(int(end), total)
        start = max(0, int(start))
        if start >= end:
            raise ValueError("空区间：[%d, %d)，PDF 共 %d 页" % (start, end, total))

        eng = make_engine(threads, det_side)
        pages = []
        t_start = time.time()
        fh = open(out_file, "w", encoding="utf-8") if out_file else None
        try:
            for i in range(start, end):
                result = read_page(eng, doc, i, dpi)
                pages.append(result)

                if fh is not None:
                    fh.write(page_header(i, os.path.basename(pdf_path)))
                    if result["skipped"]:
                        fh.write("[扫描残页，墨迹 %.3f，已跳过]\n" % result["ink"])
                    else:
                        fh.write(result["text"] + "\n")
                    fh.flush()

                if progress is not None:
                    done = len(pages)
                    elapsed = time.time() - t_start
                    avg = elapsed / done
                    progress(
                        result,
                        {
                            "done": done,
                            "total": end - start,
                            "avg_seconds": round(avg, 2),
                            "eta_seconds": round((end - start - done) * avg),
                        },
                    )
        finally:
            if fh is not None:
                fh.close()

        elapsed = time.time() - t_start
        real = [p for p in pages if not p["skipped"]]
        return {
            "pdf": pdf_path,
            "start": start,
            "end": end,
            "page_count": total,
            "pages_done": len(pages),
            "pages_ocr": len(real),
            "pages_skipped": len(pages) - len(real),
            "chars": sum(len(p["text"]) for p in pages),
            "seconds": round(elapsed, 2),
            "avg_seconds": round(elapsed / max(len(pages), 1), 2),
            "out_file": out_file,
            "pages": pages,
        }
    finally:
        doc.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="fastocr",
        description="扫描版 PDF 极速 OCR（RapidOCR v3 + OpenVINO，约 1.2 秒/页）",
    )
    ap.add_argument("pdf", help="PDF 路径")
    ap.add_argument("--start", type=int, default=0, help="起始页（0 基，含）")
    ap.add_argument("--end", type=int, default=None, help="结束页（0 基，不含）")
    ap.add_argument("--only-page", type=int, default=None, help="只跑这一页（0 基）")
    ap.add_argument("--out", default=None, help="输出 txt 路径；不给就打到 stdout")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（含每页明细）")
    ap.add_argument("--dpi", type=int, default=DPI, help="渲染 dpi，默认 %d" % DPI)
    ap.add_argument("--threads", type=int, default=_THREADS, help="推理线程，默认 %d" % _THREADS)
    ap.add_argument("--det-side", type=int, default=DET_SIDE, help="det 边长上限，默认 %d" % DET_SIDE)
    ap.add_argument("--keep-partial", action="store_true", help="不跳过扫描残页")
    ap.add_argument("--quiet", action="store_true", help="不打进度")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.pdf):
        print("找不到 PDF：%s" % args.pdf, file=sys.stderr)
        return 2

    start, end = args.start, args.end
    if args.only_page is not None:
        start, end = args.only_page, args.only_page + 1

    if args.keep_partial:
        global PARTIAL_INK
        PARTIAL_INK = -1.0  # 让 is_partial 永远为假

    def progress(page, state):
        if args.quiet:
            return
        if page["skipped"]:
            print("  PDF%d 残页跳过 (墨迹 %.3f)" % (page["page_label"], page["ink"]), flush=True)
        else:
            print(
                "  PDF%d %d行 %.1fs (均 %.2fs/页, 剩 %d 分)"
                % (
                    page["page_label"],
                    len(page["lines"]),
                    page["elapsed"],
                    state["avg_seconds"],
                    state["eta_seconds"] // 60,
                ),
                flush=True,
            )

    result = ocr_range(
        args.pdf,
        start=start,
        end=end,
        dpi=args.dpi,
        threads=args.threads,
        det_side=args.det_side,
        out_file=args.out,
        progress=progress,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.out is None:
        for p in result["pages"]:
            print(page_header(p["page_index"], os.path.basename(args.pdf)))
            print("[扫描残页，墨迹 %.3f，已跳过]" % p["ink"] if p["skipped"] else p["text"])
        print(
            "\n完成 %d 页（OCR %d / 跳过 %d），%d 字，%.1f 秒，均 %.2fs/页"
            % (
                result["pages_done"],
                result["pages_ocr"],
                result["pages_skipped"],
                result["chars"],
                result["seconds"],
                result["avg_seconds"],
            ),
            file=sys.stderr,
        )
    else:
        print(
            "完成 %d 页（OCR %d / 跳过 %d），%d 字，%.1f 秒，均 %.2fs/页 → %s"
            % (
                result["pages_done"],
                result["pages_ocr"],
                result["pages_skipped"],
                result["chars"],
                result["seconds"],
                result["avg_seconds"],
                result["out_file"],
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())