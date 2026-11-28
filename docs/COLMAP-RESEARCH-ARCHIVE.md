# COLMAP 原生动态焦距研究归档

2026-11-28 起，Seedom 2.0 的常用求解入口收敛为 VGGT-Omega（默认）和旧版 GLOMAP。COLMAP Global Mapper 不再作为新任务的可选求解器。

## 已保留的工作

- 应用中的原生 COLMAP 求解分支、动态焦距传参、诊断与历史进度解析代码。
- 完整 COLMAP C++ 源码及 CPU/CUDA 构建目录，仍在工作区 `src/solvers/colmap/`。
- 自定义动态焦距 BA 与平滑约束（包括 `src/colmap/estimators/cost_functions/dynamic_focal.h`），以及 mapper 的相关改动。
- 既有 COLMAP 任务、历史结果、耗时记录和 Blender 工程。历史任务不会因默认项变化而被重写为 VGGT 或 GLOMAP。

COLMAP 源码仓库的当前分支为 `v2.0.0-cpu-dynamic-focal`，提交为 `113262b6b7aa1fa9cec9a35306da65c91d4c7571`。该提交之后尚未提交的 `src/colmap/sfm/global_mapper.cc` 改动也已备份。

## 本地备份

修改入口前的备份：

```text
工作区/backups/Seedom-before-VGGT-primary-20261128-092405.zip
```

包含应用源码、测试、文档、当时的设置与任务记录，以及两个源码仓库的提交信息、工作区补丁和自定义焦距代码快照。源码压缩包没有复制模型权重、编译产物或视频项目；完整底层开发环境仍保留在原目录。

归档分支仍可通过 Python 的显式 `Options(backend='colmap')` 调用，用于研究和读取原任务。正式新建任务与常用 CLI 仅列出 VGGT-Omega、GLOMAP。编辑历史 COLMAP 任务时会展示当前可用选项，只有用户提交修改才替换其任务参数。

## COLMAP 工具仍然需要

GLOMAP 使用配套 COLMAP 3.12 完成特征提取、匹配及模型导出，因此不能删除这部分运行文件。新版 COLMAP 4.3 自定义求解器则作为研究环境保留；二者的数据库和参数版本仍分开管理。

本次模型接入与入口调整没有重新编译底层代码。应用源码同步至 `2.0.0-dev` 不等于发布新发行版；原生研究代码仍通过专用归档分支保留，不与应用源码目录混合提交。
