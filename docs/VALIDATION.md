# v0.1.0 验证记录

日期：2026-09-29。环境：Windows x64、Blender 4.5.7 LTS、UE 5.6.1、.NET 8，外部 NTE Mod Packager v2.5 / `09613bb`。

## 已完成

| 层次 | 结果 |
| --- | --- |
| 纯 Python | 34 项通过：清单和节点状态校验、路径边界、共享材质独立槽、项目选择、陈旧报告、源文件变化、A/B 任务覆盖同一 UE 资产、完整 sidecar 指纹、打包输出验证 |
| Blender 合成样本 | 13 项通过：插件注册、图保存重载和 UUID、节点编译、相同材质的两个独立槽、临时导出无源场景变更、FBX 回读形态键/骨骼/UV、四种贴图复制及修改器预拒绝 |
| Blender 真实角色 | Fadia：53,808 顶点、70,774 面、16 槽、284 骨、141 个非 Basis 形态键、4 层 UV。源文件 SHA256 与内存快照前后一致 |
| UE 合成样本 | 10 项通过：首次/再次导入、共享材质独立槽、Smile 形态键、UV、贴图配置、FBX 设置恢复、错误工程/不匹配骨架/缺失占位的提前拒绝 |
| UE 真实角色 | 实际 CLI → 离线 commandlet → 接收器成功。16 槽、284 骨、141 个形态键、4 层 UV；导入尺寸约 91.46 × 54.70 × 176.35 cm |
| 合成样本 cook/package | 五个允许资产（一个网格、四张贴图）进入最终容器；材质和 Skeleton 被排除；Morph 修复与 retoc 验证成功 |
| cooked 纹理 | 从实际序列化的 PixelFormat 字段读取：漫射/ID/LightMap 均为 `PF_BC7`，法线为 `PF_BC5`；UE 保存前已读回对应 sRGB 设置 |
| Fadia cook/package | cook 0 错误、0 警告；Morph 工具处理 141 个形态键；retoc 验证通过；输出三件套，UCAS 为 3,916,373 字节 |

源码树、测试输入和安装包分别验证。发行 ZIP 仅包含 Blender 运行所需 Python、说明文档及 .NET 适配器运行文件；不含游戏资产、原始 `.blend`、日志、测试输出、PDB 或外部打包器工具。安装包使用独立 Blender 进程从解压后的内容注册和创建四种节点。

## 本地证据（不提交、不随安装包分发）

- `artifacts/blender_smoke/result.json`
- `artifacts/fadia_smoke/result.json`
- `artifacts/ue_smoke/smoke_result.json`
- `artifacts/fadia_smoke/transport-result.json`
- `artifacts/package-smoke-report.json`
- `artifacts/cooked-textures-smoke-report.json`
- `artifacts/package-smoke-container-manifest.json`
- `artifacts/package-fadia-report.json`
- `artifacts/release_smoke/result.json`
- `dist/release-verification.json`

## 尚未验证或尚未实现

- 实际端到端传输测试覆盖离线 commandlet；在线编辑器 Remote Execution 已检查协议与工程筛选，但未进行完整在线编辑器往返测试。
- 自动生成 PhysicsAsset 的可选分支未单独进行实际集成测试。
- 本次角色使用隔离的测试资源路径，没有安装进游戏。游戏内材质显示、形态键和骨骼表现仍待实际角色测试；容器验证不代表游戏兼容已经确认。
- 节点中的运行时切换、面板生成、自定义 MI 创建和多角色配置尚未实现。含待生成功能的任务不能打包。
