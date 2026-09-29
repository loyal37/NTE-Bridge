# v0.3.1 验证记录

日期：2026-09-29。使用指定的 Blender 4.5.7 LTS 与隔离 UE 5.6.1 工程。

- 98 项纯 Python 测试通过。新增覆盖固定目录与工程/角色隔离、旧文件和打包暂存清理、失败保留、发布失败回滚、中断恢复、预约失效、同内容不同代报告失效、Windows 跨进程锁与进程退出解锁、真实 junction 越界拒绝，以及角色平铺导出、内部子目录、旧旁文件和目标冲突。
- Blender 4.5.7 的 25 项烘焙/资产选择检查通过：相同角色复用请求与报告、其他角色隔离、预约防止覆盖运行中请求、启动失败释放预约、操作锁保护资产选择、重烘焙清除旧选择并载入新报告；原有筛选、禁选引用、导出位置和缓存失效行为保留。
- 实际连续两次烘焙同一隔离 UE 角色，确认请求、报告和 cooked 路径一致；新烘焙身份替换旧身份，人为加入的过期文件及旧选择/打包暂存全部清除，最终仅保留 `current`。其间实际调用外部打包器生成成品，确认打包持有角色锁。第三次以不存在的引擎路径制造失败，报告明确失败，上次成功文件的全部指纹不变。工程描述与源资产前后未变。
- 实际选中网格、漫射、法线、自定义 MI、Blueprint、AnimBlueprint 共 6 项、12 个文件，直接导出到 `Characters/BridgeFixture`，无 `Player` 层且保留角色内部子目录。外部打包成功，retoc 回读仍为精确 6 个原始 `/Game/Characters/Player/BridgeFixture/...` 包路径；原烘焙文件未变。
- 用户已有 `Characters/Player/078_Nitsa` 已移动至 `Characters/078_Nitsa`，2 个文件 SHA256 全部一致，空 `Player` 目录已移除，另一角色不变。截图中的两份旧烘焙目录在检查时已清空，本次未重复删除。
- 实测曾发现过长的缓存目录名触发 UE 的 260 字符输出路径限制，已改用短角色名加项目/角色身份哈希并重测通过。Windows 短暂文件占用使用有界重试；持续占用时保留目录所有权标记供下次恢复。

本地证据：`artifacts/unit-v0.3.1.log`、`artifacts/cooking_ui_smoke/a6c846d63f/result.json`、`artifacts/recook_smoke/5924ba816b/result.json`、`artifacts/export_031/result.json`、`artifacts/export-path-migration-031.json`。仅更新 main，不创建标签或 Release；游戏内验证仍由后续实际使用确认。

## v0.3.0 验证记录

日期：2026-09-29。使用 Blender 4.5.7 LTS 和隔离 UE 5.6.1 工程。

- 81 项纯 Python 测试通过，包括独立角色目录烘焙、真实资产类及完整文件快照、原引用禁选、只暂存勾选项、报告/文件变化拒绝、导出前预检、原路径导出、旧旁文件更新和未选旧包隔离。旧导入与 manifest 打包接口回归保留。
- Blender 4.5.7 的新界面 19 项检查通过：角色根目录及 fire 子网格默认范围、自定义其他角色、无网格/无最近导入任务烘焙、初始全部不选、原引用和自定义 MI 区分、搜索/类型/目录外依赖筛选、只选择可见可打包项、独立导出位置、打包不启动 UE、缓存/工程/范围变更和报告变化清除旧选择，以及高 DPI 窗口宽度和隐藏资产详情。原缓存 10 项集成回归通过。
- 在真实 Blender 窗口绘制资产选择界面，载入实际 UE 烘焙报告；默认显示 12 个角色资产，目录外 212 个依赖保留在报告并默认隐藏。
- 实际测试工程具有一个网格、四种贴图、原 Material/MI/Skeleton/PhysicsAsset、自定义 MI、Blueprint 和 AnimBlueprint；完整角色目录烘焙成功。另一个名称前缀相同的角色目录不会误入范围，切换后无需重新导入即可单独烘焙。
- 实际选中网格、漫射、法线、自定义 MI、Blueprint、AnimBlueprint 共 6 项，按 `<工程>/Content/Characters/Player/<角色>` 导出其完整旁文件后调用现有外部打包器。实际生成 `.pak/.utoc/.ucas`；retoc 回读容器清单与 6 项选择完全一致。未选贴图、原引用和人为保留在持久导出目录中的旧资产均未混入。打包后原烘焙快照的全部文件指纹不变。
- 实际序列化贴图回读仍为漫射/ID/LightMap `PF_BC7`、法线 `PF_BC5`。独立测试源文件和工程描述文件未变，未操作用户原 HT 资产。

本地证据：`artifacts/unit-v0.3.0.log`、`artifacts/cooking_ui_smoke/latest_result.json`、`artifacts/cache_smoke/latest_result.json`、`artifacts/ui_030/asset-dialog.png`、`artifacts/cooking_fixture/run-fd91160ce6b5/integration_package.json`、同目录 `container_manifest.json` 和 `texture_formats.json`。仅推送 main，不创建标签或 Release。游戏内表现未在本次验证范围内。

## v0.2.2 验证记录

日期：2026-09-29。Blender 固定 4.5.7 LTS；实际导入使用隔离的 UE 5.6.1 工程。

- 66 项纯 Python 测试通过。缓存选择覆盖工程/Blend/角色目录优先级、C 盘排除、无上下文时不回退 AppData、带引号路径及只读识别。预览绑定与打包清单校验覆盖来源、角色和重复冲突；预览依赖即使出现在 cooked 目录也不会进入暂存清单。打包测试启动真实子进程，确认默认临时文件落在任务缓存，父进程环境不变；未重复完整外部打包。
- 10 项 Blender 缓存集成检查通过：自选中文/空格/引号目录实际导出 FBX、任务与报告共用缓存、切换目录清除历史选择、保存重载、已保存相对路径、未保存相对路径拒绝、旧 C 盘配置实际 load_post 迁移、缓存外任务在同步/打包启动前拒绝。旧 C 盘缓存文件清单与源文件保持不变。
- 11 项实际导出→UE 后台发送检查通过；FBX、清单、同步报告与日志均在选定缓存的 `Jobs/<任务 ID>`。测试仍确认独立槽、形态键、四种贴图以及原路径资产创建和占位排除。
- 在独立 Blender 窗口绘制并检查高级设置截图，缓存选择器、派生路径和中文说明正常，用户正在使用的窗口未被操作。
- Blender 漫射预览 17 项检查通过：只读当前生效输出，支持直接图像、转接点与 Diffuse BSDF；文件图、打包图及编辑/生成图正确落在缓存，编辑和生成 PNG 回读像素正确。相同来源复用、同图不同原始路径保留、目标冲突/路径不明拒绝、手动路径补充及替换图复用、DiffuseColorMap 参数别名、预览排除打包及源图像/文件不变均验证。
- 安装 ZIP 使用 Blender 实际的 addon_utils.enable 入口验证注册；缓存迁移在插件启用的受限数据上下文结束后执行，并在打开工程时执行。
- UE 材质预览 11 项检查通过，两次编辑器启动与五次导入验证：可见槽名等于材质名、共用材质仍有独立导入标识、原路径预览贴图 BC7+sRGB、旧空材质修复、连接与元数据保存重载、重复导入无重复节点、改图更新，以及用户材质图/实例/改接后的受管图保留。
- 真实 Nitsa 从原 `.blend` 只读导出，再在四项依赖均被明确禁用的隔离 UE 工程导入，并重启 UE 回读：11 个可见材质名与独立导入标识、592 根骨骼、83 个形态键、4 层 UV；7 张漫射按 JSON 的游戏原路径保存，10 个材质基础颜色连接正确，全部 BC7+sRGB。独立逐槽审计确认源图片与暂存字节一致。自动图片均为预览，打包清单仅 1 个网格。源 `.blend`、图片、原任务及工程文件保持不变。

本地证据：`artifacts/unit-v0.2.2.log`、`artifacts/cache_smoke/latest_result.json`、`artifacts/blender_send_smoke/result.json`、`artifacts/ui_022/sidebar.png`、`artifacts/preview_smoke/latest_result.json`、`artifacts/preview_smoke/run-emx4tw2u/result.json`、`artifacts/preview_nitsa/mapping_audit.json`、`artifacts/dependency_smoke/run-hjrtj2pz/preview_readback.json`。本次仅推送 main，不发布 Release。

## v0.2.1 验证记录

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
