"""Validate the installed CUDA wheel and execute a small real BF16 GPU operation."""
import json
import torch
import gradio
import diffusers
import transformers
import accelerate

if not torch.cuda.is_available():
    raise RuntimeError("无法使用 NVIDIA 显卡。请更新 NVIDIA 官方驱动后重试。")
if not torch.cuda.is_bf16_supported(including_emulation=False):
    raise RuntimeError("显卡不支持原生 BF16；本包需要 NVIDIA Ampere 或更新架构。")
x = torch.ones((16, 16), device="cuda", dtype=torch.bfloat16)
y = x @ x
torch.cuda.synchronize()
assert y[0, 0].item() == 16
print(json.dumps({"torch": torch.__version__, "cuda": torch.version.cuda,
                  "gpu": torch.cuda.get_device_name(0), "bf16_test": "passed"}))
