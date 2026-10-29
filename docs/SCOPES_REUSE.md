# 矢量图现成实现比较与复用

核对日期：2026-10-30。先比较公开 README、许可证与可取得的核心源码，再选择当前 Python/Qt 项目可复用的部分。作者声明的性能不等于本机 C1 实测。

| 项目 | 已查到的能力与许可证 | 本项目判断 |
|---|---|---|
| [OpenRV Web](https://github.com/lifeart/openrv-web) | MIT；Vectorscope.ts 有 BT.601 形式 Cb/Cr 投影、六色靶标、123° 肤色参考、CPU 绘图和 GPU 通道 | 本次选择。移植坐标计算、靶标及参考线几何，保留 MIT 声明；不引入浏览器或整套播放器 |
| [FilmCraft scopes](https://github.com/storytold/filmcraft/tree/main/crates/scopes) | MIT/Apache-2.0；独立 Rust 数值核心，YUV/HLS 矢量图、count grid、agent summary、123° 参考 | 很适合 AI 数值设计；当前不为一个指标增加 Rust/WASM 构建与跨语言运行层。仅参考，不复制源码；其默认降采样与本项目 1600 长边口径不同 |
| [ScopeWalker](https://github.com/ScopeWalker/ScopeWalker) | CC0；单文件 Win32 C，矢量图、肤色线、颜色靶标、2× 放大 | 功能直接相关；输入依赖屏幕选区，不能原样作为 C1 数据源。本次未复制源码 |
| [DeoTime/vectorscope](https://github.com/DeoTime/vectorscope) | Python/NumPy/Matplotlib＋Lightroom；JPEG 导出、肤色范围判断、数值、500ms 文件监听 | 技术栈接近，但核对的根目录未见明确 LICENSE，不据公开可见推定可再分发；未复制。其 Lightroom 导出与监听入口也不替代 C1 原生渲染 |
| [Scopes++](https://github.com/MindStudioOfficial/scopes_plusplus) | GPL-3.0；C++23/OpenGL/OpenCL、UV 矢量、NDI、多个色彩空间 | 原生 GPU 方案值得参考；运行依赖和许可不能直接当作本项目 MIT 模块打包。本次未复制 |
| [VSView](https://github.com/Jaded-Encoding-Thaumaturgy/vs-view) | 官方 Histogram 插件文档列出矢量图、肤色线、矩阵和分辨率选择；依赖 VapourSynth，RGB 输入受限 | 不是当前 RGB 预览可直接替换的轻量库；未纳入依赖，未复制代码，未完成许可证复核 |
| [FFmpeg vectorscope](https://github.com/FFmpeg/FFmpeg/blob/master/libavfilter/vf_vectorscope.c) | 已核对滤镜源码，含 graticule 和 601/709 矩阵选项 | 现有波形/Parade 已通过 PyAV 使用 FFmpeg。矢量图还需要可回传的精确计数，选择可拆出的 OpenRV 投影加共享 count grid，避免把绘制灰度当成原始计数 |

## 实际采用

源码来源：[OpenRV Web Vectorscope.ts，固定提交](https://github.com/lifeart/openrv-web/blob/0ad41bb59713cb6abe710dbab8f4738834e82312/src/ui/components/Vectorscope.ts)。使用其 Cb/Cr 系数、颜色靶标布置、123° 参考方向与圆形标尺设计，适配为 `c1ai/vectorscope.py` 中的 NumPy/Pillow 实现。保留 [MIT 许可](../third_party/openrv-web-MIT.txt)。

本项目新增的部分是：从已有 C1 `Pixels` 输入、ROI、全分析图像计数而非上游约一万点抽样、513×513 奇数网格（灰色精确居中）、AI 数值摘要、同帧缓存及新鲜度检查。没有声称安装或运行了完整 OpenRV Web，也没有复制其浏览器 UI/GPU 层。

肤色箭头是参考方向，不是自动识别人脸或肤色的结果。不同光照、风格与背景颜色都可能落在参考扇区；建议指定肤色 ROI 再解释偏差。Adobe RGB/P3 使用从 sRGB 参考色经 ICC 转换后的参考方向，元数据返回实际角度；这仍不是色彩准确性独立标定。
