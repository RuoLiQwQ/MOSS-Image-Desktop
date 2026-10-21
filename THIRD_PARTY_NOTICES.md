# 来源与许可

本包是基于 OpenMOSS/MOSS-Image 制作的独立包装器，不表示获得官方背书。

## MOSS-Image 源码

- 来源：https://github.com/OpenMOSS/MOSS-Image
- 固定提交：c1355e6b1986dfe8fba3c0455783a05899212342
- 许可：Apache License 2.0，完整文本位于 `vendor/moss_image/LICENSE`。
- 携带未改动文件：`gradio_app.py`、`gradio_presets.py`、`inference_runtime.py`、`assets/logo.svg`、`assets/prompt_defaults.txt`。
- `assets/__init__.py` 是包装器新增的空文件，用于明确资源包位置；上游示例大图未收录。
- 新增安装器、中文简化界面和相关测试不属于上游官方代码。

## MOSS-Image-SFT 模型及随附代码

- Hugging Face：https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT
- ModelScope：https://modelscope.cn/models/openmoss/MOSS-Image-SFT
- 固定版本和每个文件的 SHA256 见 `model-manifest.json`。
- 模型仓库随附 Apache-2.0 LICENSE；下载清单保留 LICENSE 和 README。
- 模型自定义 Python 代码同样固定并校验；推理使用本地已下载文件。请仅采用可信来源的更新清单。

## 首次运行下载的工具与依赖

- uv 0.13.1：https://github.com/astral-sh/uv （MIT / Apache-2.0）。从 PyPI 固定轮子下载并校验 SHA256；轮子解包保留其许可证目录。
- CPython 3.12.12：由 uv 管理的 python-build-standalone 分发，保留下载环境内许可证。
- PyTorch、Diffusers、Transformers、Accelerate、Gradio、Pillow 等：来源、版本与哈希由 `requirements.lock` 固定；安装后各自许可证和元数据位于环境的 `site-packages` 内。
- NVIDIA 驱动与 Microsoft VC++ 运行库不在包内分发；文档提供原厂安装入口。

如重新分发包含环境或模型的离线整合包，应同时保留对应第三方许可证和通知；当前交付物仅为联网引导包。
