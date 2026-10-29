# 交给桌面 AI 的安装提示

适用于能在本机读取文件、执行命令及配置 MCP 的桌面 AI。Windows 版目前只验证 **Capture One 16.8.6.3784 + Session**；不要把此包当作 Mac 版本。没有本机权限的网页聊天不能直接完成安装。

把下面文字和本项目 GitHub 地址或已解压文件夹一起交给 AI：

> 请帮我安装这个 Capture One Windows MCP 项目，并安装配套图像分析窗口。先阅读项目 README、AGENTS.md 和 docs/INSTALL.md，核对 Capture One 是否为 16.8.6.3784，以及 Windows、Python、PowerShell、.NET 是否符合要求。请使用固定安装目录，运行 Install.ps1 -WithScopes，并按我的实际 Session 配置 MCP；不要编造路径或绕过版本校验。
>
> 请把“Capture One + AI”和“Capture One 图像分析”两个启动器放到我的桌面，启用“随 C1 配套启动器打开”，默认打开直方图。使用项目自带 Setup-Shortcuts.ps1，不覆盖其他应用的快捷方式。以后我通过“Capture One + AI”启动 C1；若分析窗口被关闭，我可以双击“Capture One 图像分析”单独重开。
>
> 请安装 0.3.0a2 或更新的适用 Windows 版，保留窗口默认“跟随当前图层蒙版”。告诉我如何选中皮肤蒙版查看肤色矢量图，以及通过“在预览中框选区域…”进一步缩小分析范围。若是升级，先按 docs/INSTALL.md 处理旧分析窗口、MCP 和本项目驻留 owner，避免后台继续使用旧代码；不要停止其他应用或其他 Python 进程。
>
> 保留原照片及已有设置，不自动强制退出 C1。若首次加载桥接需要我保存工作、正常退出再打开，请说明原因。最后检查 MCP 的 context、只读预览和 analyze_image，并告诉我安装位置、桌面入口和已验证／未验证项。不要为了验证安装修改照片。

## 安装 AI 应如何执行

- 用户只请求安装 MCP 时，不擅自启用窗口或创建桌面图标；可先提示：“需要同时安装波形／直方图窗口，并把启动器放到桌面吗？”用户已使用上面的提示或已明确要求，就直接执行，不重复询问。
- 初装：`Install.ps1 -WithScopes -SessionPath '用户真实会话路径'`。此选项安装 Qt/PyAV、启用联动并创建两个桌面快捷方式。
- 已配置会话、只补窗口：`Install.ps1 -WithScopes -EnvironmentOnly`。会更新 Python 包和 MCP 配置，不切换 Session。
- 只补桌面快捷方式：`Setup-Shortcuts.ps1`，只写快捷方式，不复制程序或照片到桌面。程序文件夹移动后需要更新快捷方式和 MCP 路径。
- 指定启动图表：`Scopes.ps1 -Mode histogram`。选择会被记住，后续不带 Mode 就沿用；其他值是 waveform、parade、vectorscope（带肤色参考箭头）。
- 自动联动设置在窗口“⋯ → 随 C1 配套启动器打开”；命令方式为 `Scopes.ps1 -AutoStart on -ConfigureOnly`，关闭用 off。
- 联动仅针对项目启动器，原厂快捷方式保持原状；不添加开机常驻监控，不自动重启运行中的 C1。非默认 C1 安装路径需要按 INSTALL.md 传 CaptureOneExe 并相应配置启动入口。

## AI 与人的共用入口

AI 调用 `analyze_image()` 可直接得到数值，窗口不必打开。打开给人看的直方图用 `analyze_image(action="open_window", view="histogram")`；它只是请求打开／重用窗口，不表示图像已经更新。

局部／肤色判断用 `analyze_image(analysis_scope="active_layer", include_vectorscope=True)`，可叠加归一化 roi。Background 表示整图，其他层使用当前整张合成蒙版权重，读取的是最终画面颜色。默认不传 analysis_scope 仍为整图，避免旧 AI 客户端语义改变。肤色线不是自动皮肤检测；`freshness.state="empty"` 表示没有可统计覆盖，不应据此调整参数或改用整图冒充。

AI 若要分析人正在看的那份快照，用窗口“⋯ → 区域、数值与状态”中的 analysis_id 调用 `analyze_image(action="get", analysis_id="...")`。核对 freshness、照片、色彩空间和裁剪；不能把旧快照或另一色彩空间的统计称为当前结果。此界面不提供自动修图按钮，实际修改仍经已有候选保护的 read/adjust 通道。
