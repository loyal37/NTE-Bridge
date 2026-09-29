# v0.2.1 验证记录

日期：2026-09-29。Blender 使用指定的 4.5.7 LTS，UE 为 5.6.1。

- 56 项纯 Python 测试通过。新增依赖缺省/明确禁用时的临时加载、缺少 API 时提前拒绝且不修改资产、目标工程已打开时拒绝启动、无报告时清除旧成功状态并给出日志位置等检查。
- 使用用户本次失败任务的 Nitsa FBX 副本，实际导入全新隔离工程。工程描述文件明确关闭 PythonScriptPlugin、EditorScriptingUtilities、GeometryScripting、ControlRig，四项仍通过进程命令行成功加载，导入退出码为 0。
- 保留 11 个独立材质槽、592 根骨骼、83 个形态键和 4 层 UV。按原始完整路径保存 13 个资产：10 个不同材质占位、网格、Skeleton 和 PhysicsAsset；UE 同步报告无警告。
- 原始任务清单、FBX、用户 `HT.uproject` 的 SHA256 前后相同，隔离工程描述文件也逐字节不变。本次真实 Nitsa 输入没有替换贴图，不将本次结果视为贴图集成验证。
- Blender 4.5.7 的 15 项合成导出检查全部通过。源模型带对象、骨骼、形态键三组真实动画；用 Blender 自带解析器检查实际 FBX，无动画堆栈/层/曲线对象，按面平滑数据为 `ByPolygon` 且平面/平滑面标记为 `[0, 1]`。启用动画读取且不忽略叶骨重新导入，仍没有动画和额外叶骨；形态键、UV、独立材质槽及原场景/原文件不变。已有导出设置与用户要求一致，无需改变运行代码。

本地证据：`artifacts/dependency_smoke/run-hq7otpzs/result.json` 及同目录任务日志、`artifacts/blender_smoke/result.json`。未重复与本次依赖修复无关的烘焙/打包或游戏内测试。

## v0.2.0 验证记录

日期：2026-09-29。Blender 固定使用 `D:/blender4,5/blender.exe`：4.5.7 LTS，构建 `a9874eeece8d`。

- 纯 Python 共 52 项：原有 34 项、15 项角色资料发现、3 项本机工具识别。覆盖完整路径证据、多个网格、共享材质、外部父级缺失、参数和贴图元数据、同名路径冲突、异常 JSON、错误/模糊 UE 安装选择等。
- 20 项 Blender 资料集成检查：用真实 Nitsa 解包 JSON 和合成网格验证两候选网格、三路径、11 个原槽、15 份本地原材质资料；包括反序槽、Ml/MI 别名、共享与重复材质、`.001` 后缀、自定义槽、稳定 UUID、重扫和保存重载、自动骨架及重绑定、手动映射保留、材质替换不抢占被移动部件身份、自动占位及同角色保留高级关闭选项。83 份原始元数据文件哈希未改变。
- 原有 13 项 Blender 合成导出回归通过，仍保留独立槽、形态键、骨骼与 UV，原场景和原文件不变。
- 一键发送的 11 项检查通过：真实执行 `发送到 UE` 操作器，连续完成 FBX 导出与 UE 5.6.1 离线导入；自动识别工程关联引擎，两阶段持续忙碌并拒绝并发，2 个槽及 Smile 形态键正确，后续无效模型不会误选上一次成功任务。另用全新 UE 目录验证角色 JSON 自动开启占位、网格/材质/Skeleton/PhysicsAsset/四张贴图实际保存到各自完整路径，三类占位均不进入导出清单。
- 实际 Blender 窗口完成面板绘制与截图检查，未改动用户打开的 Blender 窗口。发行包另外解压并在 4.5.7 中验证注册。

本次未重复 v0.1 的 Fadia 全流程烘焙/打包测试：UE 接收器与外部打包算法未改变。新增同步入口已实际通过隔离工程验证，仍未进行游戏内测试。

本地证据：`artifacts/unit-v0.2.log`、`artifacts/blender_discovery_smoke/result.json`、`artifacts/blender_smoke/result.json`、`artifacts/blender_send_smoke/result.json`、`artifacts/ui_v0.2/sidebar.png`。游戏资料和这些本地产物均不提交、不随插件发布。

## v0.1.0 基线验证

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
- 自动创建 PhysicsAsset 并放置到 JSON 指定路径已在 v0.2 合成网格实际导入中验证；占位不代表复原游戏原物理参数。
- 本次角色使用隔离的测试资源路径，没有安装进游戏。游戏内材质显示、形态键和骨骼表现仍待实际角色测试；容器验证不代表游戏兼容已经确认。
- 节点中的运行时切换、面板生成、自定义 MI 创建和多角色配置尚未实现。含待生成功能的任务不能打包。
