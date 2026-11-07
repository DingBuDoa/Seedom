# SeedomVFX 热补丁发布标准（协议 1）

适用于拾点VFX（SeedomVFX）1.1.1 及后续版本，平台为 Windows x64。
正式仓库：https://github.com/DingBuDoa/Seedom

## 用户的更新方式

1.1.0 没有在线更新功能，需手动安装 1.1.1 热补丁一次。
1.1.1 起支持启动检查、手动检查、下载、校验，以及安装并重启。
安装前必须等待求解和 Blender 导出结束。每次更新都会备份被替换文件；失败时自动恢复。
自动检查默认开启，在“设置 → 软件更新”关闭后保存即可停用。检查只访问 GitHub，不上传素材或任务信息。

## 每次发布必须提供的文件

在 GitHub 创建正式 Release，标签使用 `v主版本.次版本.补丁版本`，如 `v1.1.2`。
关闭 Draft 和 Pre-release；这两类发布不会被稳定版客户端采用。
将以下文件作为 **Release 附件** 上传，无需推送到仓库根目录：

- `SeedomVFX-1.1.2-Hotfix-YYYYMMDD.zip`：完整的热补丁目录。
- `update.json`：本次发布的在线更新清单，文件名必须保持不变。
- 对应的 `.zip.sha256`：供用户手动核对，可选上传。

应先上传 ZIP，最后上传 `update.json`，确认文件齐全后发布。
所有版本都可沿用 `update.json` 这个附件名，因为它们分别属于各自的 Release。
发布后不要替换已发布 ZIP 的内容。修复补丁时递增版本号并重新发布，避免同版本校验值和缓存不一致。

## 推荐打包命令

在源码根目录更新 `app/app_info.py` 中的 `APP_VERSION`、`BUILD_DATE`，填写 `CHANGELOG-版本.md`。
使用软件随附的 Python，或具有标准库的本机 Python，无需额外打包依赖：

```powershell
python -B tools/build_hotfix.py --base-version 1.1.1
```

默认输出至 `release-assets/目标版本/`。也可以用 `--output-dir` 指定目录。
工具生成 ZIP、逐文件校验清单、离线安装器、说明及 `update.json`，无需手填文件大小和 SHA-256。
输出已有同名文件时会拒绝覆盖，请使用新目录保存重打的草稿。

只有确实验证过的升级来源才加入 `--base-version`。例如累积补丁验证可从两个旧版本升级后：

```powershell
python -B tools/build_hotfix.py --base-version 1.1.0 --base-version 1.1.1
```

工具还会加入目标版本本身，以支持重复安装后的独立备份和回退。
客户端会检查最近 20 个正式发布，优先选择版本最高且兼容当前版本的补丁；需要中间版本时逐次升级。
不能升级或没有标准附件时，会提供前往 GitHub 发布页的入口。

## 在线清单 update.json

参考根目录 `UPDATE-TEMPLATE.json`，正式清单必须由打包工具生成。
JSON Schema 位于 `docs/updates/update.schema.json`。

| 字段 | 规则 |
| --- | --- |
| `schema_version` | 固定整数 `1`，变更协议时先提供兼容客户端 |
| `application` | 固定 `Seedom`；显示名称可变化，此标识保持稳定 |
| `platform` | 固定 `windows-x64` |
| `version` | 三段数字，与源码版本、Release 标签去掉 `v` 后一致 |
| `base_versions` | 允许升级的版本列表，不使用模糊版本范围 |
| `build_date` | `YYYY-MM-DD`，与源码构建日期一致 |
| `notes` | 用户可见的更新说明文本 |
| `package.name` | ZIP 附件名，仅使用英文字母、数字、点、下划线和连字符 |
| `package.url` | 本仓库对应 `v版本` Release 下的 HTTPS 下载地址 |
| `package.bytes` | ZIP 精确字节数，不是解压后的大小 |
| `package.sha256` | ZIP 的 64 位小写 SHA-256 |

客户端会核对 Release 标签、附件名称、URL、大小、ZIP SHA-256、包内版本和全部 payload 文件。
GitHub HTTPS 和固定仓库是更新来源；SHA-256 用于完整性校验，不是独立的数字签名。

## ZIP 和包内清单

```text
SeedomVFX-1.1.2-Hotfix-YYYYMMDD.zip
  SeedomVFX-1.1.2-Hotfix-YYYYMMDD/
    PATCH-MANIFEST.json
    Apply-Hotfix.bat
    apply_hotfix.py
    安装说明.md
    payload/
      app/
      photogrammetry_importer/
      runtime/fastmap/fastmap/  # 随补丁提供的适配源码
      README.md
      CHANGELOG-1.1.2.md
      UPDATE-STANDARD.md
      UPDATE-TEMPLATE.json
      config/runtime-manifest.json
```

`PATCH-MANIFEST.json` 含 `schema_version`、`application`、`platform`、`version`、`base_versions`、
`build_date`、`patch_id`、`project_url`、`files`；其中 `patch_id` 与 ZIP 中顶层目录名一致。
`files` 每项是相对安装目录的 `path`、精确 `bytes` 和 `sha256`。
必须包含 `app/app_info.py`；其版本须与两份清单一致。
清单写明的每个文件必须在 `payload/` 中提供完整内容，不使用二进制差分。

协议 1 限制 ZIP 至多 256 MiB、解压内容至多 512 MiB、最多 4000 个条目。
禁止路径穿越、绝对路径、Windows 特殊路径、重复大小写路径和符号链接。
允许替换应用源码、应用 SVG、摄影测量插件源码、既定 FastMap 适配源码和发布说明。
不覆盖 `config/settings.json`、任务记录、耗时历史、`logs/`、`output/`、视频和 `.blend` 文件。
本协议的热补丁不替换 Python、PySide6、CUDA、求解器 EXE 等二进制运行库。
如果未来需要新增二进制依赖，应先设计并发布相应的运行库升级协议，不能把它们偷偷放入普通补丁。

## 安装、失败和回退

软件下载和解压均在安装目录 `.updates/` 中完成，下载可以取消，不使用 C 盘临时目录。
安装器从当前软件复制独立快照，等待主程序退出后再次校验 payload，才开始备份和替换文件。
后台安装完成后重新启动软件；安装失败记录在 `.updates/installer-*/installer.log`。

备份位于 `patch-backups/目标版本-时间戳/`，其中 `receipt.json` 记录原文件是否存在及校验值。
关闭软件后运行该目录的 `Restore.bat` 回退；多次升级时由新到旧回退。
校验不通过时不修改软件；替换中发生异常则自动恢复。
断电或强制结束进程不能保证完成自动恢复，应先手动运行最近备份的 `Restore.bat`。
备份不会自动删除。确认不再需要回退后，可自行清理旧备份和 `.updates/` 中的旧下载。

## 发放前验证

运行 `tests/test_updates.py`、`tests/test_hotfix.py` 及本次功能相关测试。
用 `tools/verify_update.py 更新文件目录` 检查 ZIP、在线清单、真实安装和回退；此工具仅创建小型模拟安装目录。
核对保留用户设置、队列、耗时记录及项目文件；确认更新成功后的版本号与目标一致。
最后将生成的 ZIP 和 `update.json` 上传到对应 Release 并发布。
