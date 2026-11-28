# 拾点 VFX · SeedomVFX 2.0 开发版 · 第三方项目

本文件记录当前开发分支使用或保留的第三方组件。组件分别适用各自许可，Seedom 自身的 [LICENSE](LICENSE) 不替代上游许可，也不对模型及其适用输出另行授权。版本索引不表示相关运行文件已包含在本仓库。

## 当前工作流

| 组件 | 版本 / 来源 | 用途 |
| --- | --- | --- |
| [VGGT-Omega](https://github.com/facebookresearch/vggt-omega) | 包版本 0.0.1；固定提交 `48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6` | 相机、逐帧内参与深度预测；默认重建路径。 |
| [GLOMAP](https://github.com/colmap/glomap) | 本地 CPU / CUDA 构建 | 独立传统全局几何重建。 |
| [COLMAP](https://github.com/colmap/colmap) | 3.12 配套工具；另保留 4.3.0.dev 研究构建 | GLOMAP 的特征、匹配与模型工具，以及归档的原生动态焦距 BA 研究。 |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | 7.1.1 essentials build | 视频解码、缩放、抽帧及 Blender 背景序列转换。 |
| [Blender Photogrammetry Importer](https://github.com/SBCV/Blender-Addon-Photogrammetry-Importer) | 3.2.1，本地适配 | 摄影测量数据与相机导入 Blender。 |
| [PySide6 / Qt](https://www.qt.io/qt-for-python) | 6.12.0 核心包 | 桌面界面与媒体预览。 |
| [PyTorch](https://pytorch.org/) | 当前环境 2.9.1+cu130 | 本地 CPU / CUDA 模型推理。 |

Python 来源：[python.org](https://www.python.org/)。其他 Python 包版本见 [runtime-requirements.txt](config/runtime-requirements.txt)；包许可与元数据保留在所用运行库的包目录及 `.dist-info` 中。FFmpeg、原生求解器、Qt 和其他运行组件的适用许可应随任何被允许的二进制分发保留。

## VGGT-Omega 许可

Copyright (c) Meta Platforms, Inc. and affiliates. All rights reserved.

VGGT-Omega 的上游源码、模型权重及适用输出受 **FAIR Noncommercial Research License** 约束。源码开放或可下载并不等于获得商业使用许可；使用与允许的再分发均需遵守上游完整条款。

本仓库不包含模型权重。上游固定提交的许可副本见 [VGGT-Omega-LICENSE.txt](docs/licenses/VGGT-Omega-LICENSE.txt)，配置、权重校验信息与使用范围见 [VGGT-OMEGA.md](docs/VGGT-OMEGA.md)。

## 历史组件

[FastMap](https://github.com/pals-ttic/fastmap) 0.1 的本地适配源码及原有许可保留在 `runtime/fastmap/`，用于历史代码参考。它已不再是 2.0 新任务的可选求解器。COLMAP 原生动态焦距研究的保留范围见 [研究归档](docs/COLMAP-RESEARCH-ARCHIVE.md)。
