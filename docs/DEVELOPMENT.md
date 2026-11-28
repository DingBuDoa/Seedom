# Seedom 2.0 开发环境

本说明对应 `2.0.0-dev`。1.X 的运行环境与发行步骤请查看 [1.x-maintenance](https://github.com/DingBuDoa/Seedom/tree/1.x-maintenance) 中的文档。

## 源码与运行文件分离

```text
app/                        Qt 界面、求解调度、项目管理、Blender 导出
photogrammetry_importer/     本地适配的 Blender 插件源码
config/runtime-*.{json,txt}  运行环境索引
docs/                       使用说明、验证记录与许可
tests/test_*.py              回归测试
tools/                      历史打包、校验与维护工具
runtime/                    本地运行库；二进制不进入 Git
```

Git 仓库不包含 Python 解释器、Qt / CUDA DLL、原生求解器、FFmpeg 或模型权重，也不包含个人设置、任务队列、视频、计算缓存及导出的 Blender 工程。历史 `runtime/fastmap` 适配源码保留，但 2.0 的新任务入口只有 VGGT-Omega 与 GLOMAP。

## 两种本地布局

### 已有开发工作区

当前开发机使用如下结构，共享运行库和模型，避免重复复制大文件：

```text
workspace/
  src/
    Seedom/                       本仓库，分支 2.0.0-dev
    solvers/colmap/                单独维护的原生 COLMAP 研究源码与构建目录
  archive/legacy-1.x/runtime/      共享的历史 Python、FFmpeg 与几何求解运行库
  third_party/
    vggt-omega/                   官方模型源码
    models/VGGT-Omega/
      vggt_omega_1b_512.pt         权重，另行获取
```

在 `src/Seedom` 双击 `Start-Dev.bat`。启动器在 Python 搜索路径前端加入本分支 `app/`，避免从共享解释器误加载 1.X 应用模块。`Start-Dev-CUDA.bat` 是原生 COLMAP CUDA 开发入口；VGGT 是否使用 CUDA 由任务硬件开关和 GPU 选择控制，不依赖这个启动器。

### 独立源码目录

需要自行准备与当前应用兼容的运行库。启动器 `Start.bat` 使用 `runtime/python/pythonw.exe`，终端调试使用同目录的 `python.exe`；解释器应能导入当前仓库的应用模块和 Qt 组件。不要假设任意 1.X 便携包复制过来就与 2.0 完全兼容。

- **Python / UI：**当前验证环境为 Python 3.13.11、PySide6 6.12.0。依赖索引见 `config/runtime-requirements.txt`；这是本地环境记录，不是跨平台的一键安装配方。
- **视频：**准备 `runtime/ffmpeg/ffmpeg.exe` 和 `ffprobe.exe` 及其所需运行文件。
- **VGGT：**准备兼容的 PyTorch、NumPy、Pillow，按下一节配置上游模型源码与权重。
- **GLOMAP：**需要已适配的旧版独立 GLOMAP 和配套 COLMAP 3.12 工具；它们必须使用兼容的数据库 Schema。相关路径解析见 `app/paths.py` 和 `app/tracker_core.py`。不能把新版 COLMAP 可执行文件直接当作旧版 GLOMAP 的配套工具替换。
- **Blender：**单独安装并通过软件检测或手动添加可执行文件。

GPU 运行依赖驱动与所选 PyTorch / 原生求解器构建的兼容性。运行已有 VGGT 模型不需要 nvcc 或 C++ 编译；重建原生 GLOMAP / COLMAP 才涉及其独立构建工具链。

## VGGT-Omega 配置

从 [官方项目](https://github.com/facebookresearch/vggt-omega) 获取模型源码，并按其官方渠道与许可取得权重。当前适配固定源码提交：

```text
48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6
```

权重名称为 `vggt_omega_1b_512.pt`，大小 4,576,706,117 字节，当前使用文件的 SHA-256：

```text
c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934
```

独立目录默认查找 `runtime/vggt-omega/` 和 `runtime/models/VGGT-Omega/`。也可以创建不入 Git 的 `config/vggt-runtime.json`：

```json
{
  "source": "D:/models/vggt-omega",
  "checkpoint": "D:/models/VGGT-Omega/vggt_omega_1b_512.pt"
}
```

`SEEDOM_VGGT_SOURCE`、`SEEDOM_VGGT_CHECKPOINT` 环境变量优先于该文件；显式配置无效时会报具体路径，不静默替换其他模型。

当前核心推理使用 PyTorch 2.9.1+cu130、NumPy 2.3.5 与 Pillow，通过等价 PIL / NumPy 预处理读取图像。没有要求安装上游 Gradio 演示、训练或评测的全部依赖。许可、显存档位和输出含义见 [VGGT-OMEGA.md](VGGT-OMEGA.md)。

## 回归与质量验证

在源码根目录，用已准备好的解释器运行：

```powershell
& .\runtime\python\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

共享开发环境可将解释器替换为工作区 `archive/legacy-1.x/runtime/python/python.exe`。嵌入式解释器若未自动加载当前应用目录，可用：

```powershell
& ..\..\archive\legacy-1.x\runtime\python\python.exe -X utf8 -c "import sys,unittest; sys.path[:0]=['app','tests']; suite=unittest.defaultTestLoader.loadTestsFromNames(['test_vggt','test_vggt_adaptive','test_blender_sequence']); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())"
```

Qt 用例使用 offscreen 与临时数据；序列导出测试需要 FFmpeg。真实模型推理和 Blender 读回还需要相应资产与程序。生成物写入 `tests/artifacts/`，不纳入 Git。代码回归通过不能证明某段素材的相机轨迹准确；真实素材验证和未解决案例单独记录在 [VGGT-VALIDATION.json](VGGT-VALIDATION.json)。

## 原生求解研究与发行

自定义 COLMAP 动态焦距源码、既有构建与应用兼容分支保留，见 [研究归档](COLMAP-RESEARCH-ARCHIVE.md)。它们不是 VGGT 推理依赖；本仓库也不承诺仅靠 Python 依赖列表就能从零复现原生 CUDA / cuDSS 构建。

`tools/build_release.py` 和 `tools/verify_release.py` 是历史发行工具。2.0 的模型许可、权重外置、两种求解器运行库及独立启动仍需针对正式发行验证；在验证完成前，不能将历史脚本成功运行视为生成了可直接分发的完整 VGGT 2.0 发行包。

提交与发布时保持以下区分：应用源码进入开发分支，原生研究源码保留其专用分支；已验证便携包进入 Releases；模型、运行库、用户项目和测试素材不随源码提交。不得因 Seedom 自身源码开放而将 VGGT 权重或其适用输出视为不受上游许可约束。
