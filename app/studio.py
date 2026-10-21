"""面向首次使用者的 MOSS-Image 中文本机界面。"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "moss_image"
LOGGER = logging.getLogger("moss_studio")
SIZES = {"square": (512, 512), "landscape": (640, 384), "portrait": (384, 640)}
MEMORY_CHOICES = [
    ("最省显存 · 推荐先用（速度较慢）", "sequential"),
    ("平衡模式 · 需要更多显存", "model"),
    ("全放显卡 · 仅适合大显存设备", "none"),
]
EXAMPLES = [
    ["一只橘猫坐在窗边的木桌上，午后阳光照进房间，背景是绿植，温暖自然的摄影风格。"],
    ["水彩插画：雨后的小镇街道，青石路面倒映着灯光，远处是青山，柔和宁静的氛围。"],
    ["A small cabin beside a clear mountain lake at sunrise, soft golden light, detailed landscape photography."],
]


def import_runtime():
    """Only import the heavyweight, pinned upstream runtime when the UI starts."""
    if not (VENDOR / "gradio_app.py").is_file():
        raise RuntimeError("缺少程序文件 vendor/moss_image/gradio_app.py，请重新解压完整安装包。")
    sys.path.insert(0, str(VENDOR))
    from gradio_app import GenerationConfig, ModelRunner

    return GenerationConfig, ModelRunner


def integer(value, label, minimum, maximum):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}需要填写整数。") from exc
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label}需要填写整数。")
    result = int(number)
    if not minimum <= result <= maximum:
        raise ValueError(f"{label}需要在 {minimum} 到 {maximum} 之间。")
    return result


def make_config(config_type, prompt, shape, steps, cfg, seed, memory, text_length, negative):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("请先填写你想生成的画面，也可以点击下方示例。")
    if shape not in SIZES:
        raise ValueError("请选择方图、横图或竖图。")
    if memory not in ("sequential", "model", "none"):
        raise ValueError("请选择有效的显存模式。")
    sequence_length = integer(text_length, "提示词长度上限", 512, 4096)
    if sequence_length not in (512, 1024, 2048, 4096):
        raise ValueError("提示词长度上限请选择 512、1024、2048 或 4096。")
    try:
        cfg = float(cfg)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("提示词贴合度需要填写数字。") from exc
    if not math.isfinite(cfg) or not 0 <= cfg <= 10:
        raise ValueError("提示词贴合度需要在 0 到 10 之间。")
    width, height = SIZES[shape]
    config = config_type(
        prompt=prompt.strip(), negative_prompt=str(negative or "").strip(),
        width=width, height=height, steps=integer(steps, "生成步数", 1, 100),
        cfg=cfg, seed=integer(seed, "随机种子", -1, 2**53 - 1),
        offload=memory, max_sequence_length=sequence_length,
        num_images=1, compile_transformer=False,
    )
    config.validate()
    return config


def explain_error(exc):
    """Translate common actionable failures while preserving unknown diagnostics."""
    detail = str(exc).strip()
    lowered = detail.lower()
    if any(word in lowered for word in ("winerror 1455", "paging file", "页面文件太小", "defaultcpuallocator", "not enough memory")):
        return (
            "系统内存或 Windows 页面文件不足。请关闭其他大型程序，并确认系统磁盘有足够空间、"
            "Windows 虚拟内存设置为系统管理。省显存模式会占用更多系统内存，调整后重启电脑再试。"
        )
    if any(word in lowered for word in ("out of memory", "memory exhausted", "cuda error: out", "分配内存")):
        return (
            "显存或内存不足。请关闭其他使用显卡的软件，选择“最省显存”、方图 512×512，"
            "并把提示词长度上限改为 512 后重试。仍然失败时请重启本程序；"
            "这套模型较大，省显存模式也无法保证所有 NVIDIA 显卡都能运行。"
        )
    if any(word in lowered for word in ("dll", "winerror 126", "winerror 127", "winerror 1114")):
        return (
            "Windows 无法加载运行库。请运行安装/修复入口，检查 Microsoft Visual C++ "
            "2015–2022 x64 运行库及 NVIDIA 驱动是否完整安装，重启电脑后再试。"
        )
    if any(word in lowered for word in ("cuda gpu is required", "no nvidia driver", "cuda driver", "not compiled with cuda")):
        return "未检测到可用的 NVIDIA 显卡运行环境。请确认电脑使用 NVIDIA 显卡，更新显卡驱动后运行安装/修复入口。"
    if any(word in lowered for word in ("no kernel image", "invalid device function", "bfloat16", "sm_")):
        return "当前显卡或驱动与模型运行环境不兼容。请更新 NVIDIA 驱动，并查看安装包中的显卡要求及诊断日志。"
    if isinstance(exc, FileNotFoundError) or any(word in lowered for word in ("complete local model bundle", "pipeline_moss_image.py", "cannot find the requested files", "localentrynotfounderror")):
        return "模型或程序文件不完整。请关闭本窗口，重新运行安装/修复入口，等待模型下载完成后再启动。"
    if isinstance(exc, PermissionError):
        return "无法保存图片：当前目录没有写入权限。请把完整安装包解压到你有权限的文件夹（例如 D:\\MOSS-Image），再重新运行。"
    if any(word in lowered for word in ("no space left", "not enough space", "disk full", "winerror 112")):
        return "磁盘空间不足。请清理保存图片所在磁盘的空间后重试。"
    return detail[:1200] or "发生未知错误，请查看启动窗口或诊断日志。"


def chinese_progress(progress):
    def report(value, **kwargs):
        description = kwargs.get("desc", "")
        translations = {
            "Loading model": "正在载入模型，首次生成可能需要较长时间，请勿关闭启动窗口",
            "Encoding prompt": "正在理解提示词",
            "Decoding image": "正在绘制最终图片",
            "Saving images and parameters": "正在保存图片和参数",
            "Complete": "生成完成",
        }
        if description.startswith("Sampling"):
            description = "正在生成 · 第 " + description.rsplit(" ", 1)[-1] + " 步"
        else:
            description = translations.get(description, description)
        kwargs.update(desc=description, unit="步")
        progress(value, **kwargs)
    return report


class StudioController:
    def __init__(self, model, output_dir, config_type, runner):
        self.model = str(Path(model).expanduser().resolve())
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.config_type = config_type
        self.runner = runner
        self.lock = threading.Lock()

    def generate(self, prompt, shape, steps, cfg, seed, memory, text_length, negative, progress=None):
        try:
            config = make_config(self.config_type, prompt, shape, steps, cfg, seed, memory, text_length, negative)
            # The lock also protects callers that bypass Gradio's single-worker queue.
            with self.lock:
                paths, metadata, _ = self.runner.generate(
                    self.model, config, progress=chinese_progress(progress) if progress else None,
                )
                if not paths:
                    raise RuntimeError("模型没有返回图片，请查看启动窗口或诊断日志。")
                folder = Path(metadata).resolve().parent
                if not folder.is_relative_to(self.output_dir):
                    raise RuntimeError("保存位置异常，请关闭程序并重新启动。")
                record = json.loads(Path(metadata).read_text(encoding="utf-8"))
                actual_seed = record["parameters"]["seed"]
                elapsed = float(record.get("elapsed_seconds", 0))
                explanation = folder / "使用说明.txt"
                explanation.write_text(
                    "MOSS-Image 图片与参数\n\n"
                    "本文件夹中的 PNG 是原始生成图片，parameters.json 保存实际生成参数、随机种子及模型版本信息。\n"
                    "使用相同模型、提示词、画幅、参数和随机种子可以尝试复现结果；不同硬件或软件版本仍可能产生差异。\n"
                    "随机种子 -1 表示每次随机；本次真实种子已写入 parameters.json。\n"
                    "首次载入模型和省显存模式可能较慢，请勿在生成过程中关闭启动窗口。\n\n"
                    f"本次种子：{actual_seed}\n画幅：{config.width}×{config.height}\n"
                    f"生成耗时：{elapsed:.1f} 秒（包含本次模型载入时间）\n\n"
                    "提示词：\n" + config.prompt + "\n",
                    encoding="utf-8",
                )
                status = (
                    f"生成完成 · {config.width}×{config.height} · 种子 {actual_seed} · {elapsed:.1f} 秒\n"
                    f"图片和参数已自动保存到：{folder}\n下方可分别下载图片、参数和使用说明。"
                )
                return paths[0], [*paths, str(metadata), str(explanation)], status
        except Exception as exc:
            LOGGER.exception("Image generation failed")
            return None, [], "生成未完成：" + explain_error(exc)


def build_app(model, output_dir, memory="sequential"):
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    import gradio as gr

    config_type, runner_type = import_runtime()
    model = Path(model).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    if not model.is_dir():
        raise FileNotFoundError("模型目录不存在，请先运行安装/修复入口并等待下载完成。")
    output_dir.mkdir(parents=True, exist_ok=True)
    controller = StudioController(model, output_dir, config_type, runner_type(output_dir, local_files_only=True))
    with gr.Blocks(title="MOSS-Image · 中文画室", analytics_enabled=False) as app:
        gr.Markdown(
            "# MOSS-Image · 中文画室\n"
            "写下你想看到的画面，选择画幅，点击 **开始生成**。图片在你的电脑上生成并自动保存。\n\n"
            "首次生成需要载入大模型，请耐心等待。默认使用省显存设置；生成时请保持启动窗口打开。"
        )
        with gr.Row():
            with gr.Column(scale=1, min_width=320):
                prompt = gr.Textbox(label="你想生成什么画面？", lines=5, max_lines=12,
                                    placeholder="例如：一只橘猫坐在窗边，午后阳光，温暖的摄影风格。")
                shape = gr.Radio(
                    choices=[("方图 512×512", "square"), ("横图 640×384", "landscape"), ("竖图 384×640", "portrait")],
                    value="square", label="选择画幅",
                )
                generate = gr.Button("开始生成", variant="primary")
                with gr.Accordion("高级参数 · 第一次使用可以不改", open=False):
                    steps = gr.Slider(1, 100, value=50, step=1, label="生成步数", info="更多步数通常耗时更长。")
                    cfg = gr.Slider(0, 10, value=3.0, step=0.1, label="提示词贴合度", info="建议先用默认值 3。")
                    seed = gr.Number(value=-1, precision=0, minimum=-1, maximum=2**53 - 1,
                                     label="随机种子", info="-1 每次随机；填入上次种子可尝试复现结果。")
                    offload = gr.Dropdown(choices=MEMORY_CHOICES, value=memory, label="显存模式",
                                          info="切换后下一次生成会重新载入模型。省显存会增加内存占用和等待时间。")
                    text_length = gr.Dropdown(choices=[512, 1024, 2048, 4096], value=512, label="提示词长度上限",
                                              info="默认 512 可减少显存使用。较长提示词会被截断；4096 是上游完整长度，需要更多资源。")
                    negative = gr.Textbox(label="不希望出现的内容（可留空）", lines=2, value="")
                gr.Examples(examples=EXAMPLES, inputs=[prompt], label="点击示例，填入提示词", cache_examples=False)
            with gr.Column(scale=1, min_width=320):
                picture = gr.Image(label="生成的图片", type="filepath", interactive=False)
                status = gr.Textbox(label="运行状态", value="准备就绪。写下提示词后点击“开始生成”。", lines=4, interactive=False)
                downloads = gr.File(label="下载图片、参数和使用说明", file_count="multiple", interactive=False)
        gr.Markdown(
            "**使用提示：** 同一时间只生成一张。若显存不足，请关闭其他占用显卡的软件，保留默认省显存模式和 512 提示词长度。\n\n"
            "基于 [OpenMOSS / MOSS-Image](https://github.com/OpenMOSS/MOSS-Image)，默认使用 MOSS-Image-SFT。"
            "这套模型体积较大，首次运行和生成速度取决于你的电脑配置。"
        )

        def run(prompt, shape, steps, cfg, seed, memory, text_length, negative, progress=gr.Progress()):
            return controller.generate(prompt, shape, steps, cfg, seed, memory, text_length, negative, progress)

        generate.click(run, [prompt, shape, steps, cfg, seed, offload, text_length, negative],
                       [picture, downloads, status], concurrency_limit=1, concurrency_id="generation", api_name=False)
    return app.queue(default_concurrency_limit=1, max_size=4)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "MOSS-Image-SFT")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "pictures")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--memory", choices=("sequential", "model", "none"), default="sequential")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须为 1024 到 65535 之间的整数。")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        app = build_app(args.model, args.output_dir, args.memory)
        app.launch(server_name="127.0.0.1", server_port=args.port, share=False, inbrowser=False,
                   allowed_paths=[str(args.output_dir.expanduser().resolve())], show_error=False)
    except Exception as exc:
        LOGGER.exception("Studio startup failed")
        print("\n启动失败：" + explain_error(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Upstream metadata uses the interpreter's default encoding. UTF-8 mode
    # preserves Chinese prompts and emoji on Windows, including direct launches.
    if not sys.flags.utf8_mode:
        os.execv(sys.executable, [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(main())
