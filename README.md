# NTE Bridge / 异环桥接

Blender 4.5 LTS 与 Unreal Engine 5.6 的角色 Mod 制作桥接。独立项目，沿用 LoyalTools 的节点操作思路，使用 NTE 资产路径、FBX 和 UE 导入流程。

当前版本 **0.1.0，开发预览**：提供角色配置、简单节点图、FBX 任务导出、UE 资产同步、清单筛选和外部打包。一个任务对应一个骨骼网格与其骨架。

## 当前可以做什么

- 角色配置和节点图随 `.blend` 保存；为每个材质槽保留独立的部件标识。同一材质可被多个槽引用。
- 在临时副本中导出网格与骨架，保留形态键、UV 与权重；导出操作不保存原 Blender 工程。
- `部件 → 组合 → 循环 → 角色输出` 节点可描述不同数量的部件组，校验连线、默认状态、重复部件与按键冲突。
- 指定完整 `/Game/...` 路径，向准确匹配的 UE 工程发送 FBX，分配材质、Skeleton 和 PhysicsAsset。缺失的占位资产仅在开启对应选项后创建。
- 导入替换贴图并设置用途：漫射 BC7+sRGB；ID/LightMap BC7 非 sRGB；法线 Normalmap/BC5 非 sRGB。
- 从成功的同步任务烘焙资源，筛选网格与明确替换的贴图，调用现有外部打包器，验证 `.pak/.utoc/.ucas` 三个输出文件。

**本版尚未把节点中的切换生成为 UE 运行逻辑。** 节点图用于配置保存和校验；同步报告会明确标出未生成的切换，含这些功能的任务不能打包。第一阶段请使用只有基础角色输出、没有连接循环功能的图，验证原模型流程。后续版本会增加受管蓝图区域更新，再接入现有切换与面板生成器。

## 安装和首次设置

1. 在 Blender 4.5 的偏好设置中使用“从磁盘安装”，选择 `NTE-Bridge-v0.1.0-Blender.zip`，启用 **NTE Bridge / 异环桥接**。
2. 在 3D 视图的侧栏打开 **NTE Bridge**，选择一个角色网格和它绑定的骨架，创建角色节点图并整理材质槽映射。
3. 设置目标 `.uproject`、UE 引擎目录、网格路径和 Skeleton 路径，按需设置 PhysicsAsset。资源路径写为 `/Game/Characters/.../资源名`，不加 `.资源名` 后缀。
4. 为每个槽填写真实游戏材质的完整路径。支持角色目录以外的共享材质。首次建立工程占位时可开启“创建缺失占位”；已有骨架会先检查骨名和层级。
5. 添加本次替换的贴图、目标路径和用途。未替换的共享贴图无需加入任务。
6. 设置任务目录和最终 Mod 输出位置。任务目录保存 FBX、清单和执行报告，便于定位失败阶段。

启用中的非 Armature 修改器会阻止导出。需要影响最终几何的修改应先按你的形态键工作流程处理，再导出；插件不会悄悄忽略镜像、细分等效果，也不会为应用修改器而丢弃形态键。

## 同步和打包

目标 UE 工程需要启用 **Python Editor Script Plugin**、**Editor Scripting Utilities**、**Geometry Script**（核验 UV）和 **Control Rig**（只读检查骨骼层级）。发送到已经打开的编辑器时，在项目设置的 Python 中开启远程执行，通信限制在本机。插件会核对完整工程路径；不会选择列表中的第一个编辑器。

“导出桥接任务”先生成独立的任务目录。“发送最近任务到 UE”使用后台进程运行导入，任务目录中的 `ue_report.json` 记录实际槽号、形态键、资产路径和错误。也可以选择“UE 关闭时后台导入”；此模式要求目标工程的编辑器已关闭，并自动启动后台 UE 完成导入后退出。

“烘焙并打包最近任务”使用已成功同步的任务。第一版要求打包前关闭目标 UE 编辑器，以免导入/保存与独立烘焙交错；其他工程不受影响。原游戏材质、Skeleton、PhysicsAsset 不进入最终暂存目录。已有打包器的 Morph 修复、retoc 容器转换和验证由同一服务执行；本插件不会重新实现其兼容算法。

外部打包器目录需包含 `NteMorphTargetPatch.exe`、`retoc.exe`、`oo2core_9_win64.dll`，并安装 .NET 8 Runtime。开发源码方式首次构建适配器另需 .NET 8 SDK 和打包器源码；发布 ZIP 会包含已编译适配器，不携带外部工具本体。

若目标输出已存在，请改用新 Mod 名称或新输出位置。本版不自动覆盖旧成品。

## 命令行

使用普通 Python 3.11+ 或 UE 自带 Python。`python` 在部分 Windows 环境中只是应用商店占位符，需使用实际解释器路径。

```powershell
& '<Python.exe>' blender_addon/nte_bridge/cli.py validate --manifest '<任务目录>/manifest.json'
& '<Python.exe>' blender_addon/nte_bridge/cli.py sync --manifest '<任务目录>/manifest.json' --engine-dir '<UE_5.6目录>' --mode commandlet
& '<Python.exe>' blender_addon/nte_bridge/cli.py package --manifest '<任务目录>/manifest.json' --engine-dir '<UE_5.6目录>' --packager-source '<外部打包器目录>' --output-dir '<成品目录>' --mod-name 'MyCharacter_P'
```

## 开发与验证

```powershell
& '<Python.exe>' -m unittest discover -s tests -p 'test_*.py' -v
& '<Python.exe>' tools/create_test_project.py
& '<Blender.exe>' --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/blender_smoke.py
& '<Python.exe>' tools/build_release.py
```

Blender 测试的 `artifacts/blender_smoke/result.json` 给出生成的清单路径。使用该路径运行上面的 `cli.py sync --mode commandlet`，即可在测试工程验证实际 UE 导入。`tests/unreal_smoke.py` 进一步验证同任务重导入，输入由环境变量 `NTE_BRIDGE_SMOKE_MANIFEST` 指定，并拒绝在其他工程执行。

测试使用 `artifacts/` 内的独立工程和合成模型，不要求重导入作者的实际游戏资产。适配器构建见 [tools/packager_cli/README.md](tools/packager_cli/README.md)，发布打包前必须先构建它。实际测试记录见 [docs/VALIDATION.md](docs/VALIDATION.md)。任务协议见 [docs/CONTRACT.md](docs/CONTRACT.md)。

## 边界

- 当前只支持一个网格、一个单根骨架；资产名称使用 ASCII 字母、数字和下划线。
- 新的材质实例创建、自定义多槽材质状态、UE 切换/面板自动生成、按住生效和条件链仍待实现。
- 保留形态键不等于提供游戏内形态键滑块。
- 占位材质用于路径和引用，不能复现游戏定制着色器的完整编辑器预览。
- 导入失败可能留下已创建或变更的编辑器资产；报告列出部分变更，不声称完整回滚。每个任务有独立日志和结果，不复用旧成功报告。
- 游戏中的最终纹理显示、形态键与骨骼表现仍需使用实际角色验证。自动检查与容器验证不能代替游戏运行验证。
