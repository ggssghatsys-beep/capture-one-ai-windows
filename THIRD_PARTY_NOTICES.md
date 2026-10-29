# Third-party materials

The MIT license in this repository covers this project's own Python, C#, PowerShell code and documentation. Capture One and its trademarks belong to their respective owners. This project is unofficial and is not endorsed by Capture One.

Capture One executables, assemblies, SDK files, camera profiles and image-processing components are **not distributed**. Install and license Capture One separately. The release includes only this project's own compiled bridge assemblies and their source.

Python packages are installed separately by pip and retain their own licenses, including the MCP Python SDK and Pillow and their dependencies. Python, PowerShell and the .NET runtime are not bundled.

Image analysis uses NumPy (BSD-3-Clause), Pillow/ImageCms, optional PySide6-Essentials (Qt/PySide LGPLv3/GPLv3/commercial terms as applicable), and PyAV 17.1 (BSD-3-Clause) to call FFmpeg's waveform filter in process. FFmpeg and the libraries contained in a particular PyAV wheel retain their own LGPL/GPL licenses; the project's MIT license does not relicense them. These packages are installed by pip, not copied into this source release. See [PyAV's license](https://github.com/PyAV-Org/PyAV/blob/v17.1.0/LICENSE.txt) and [FFmpeg licensing](https://ffmpeg.org/legal.html) before redistributing a bundled executable.

Scopes research examined ScopeWalker (CC0-1.0, commit 2b04fab7cdaa34a65c7e508abc6a4112c5cdf983), FilmCraft scopes (MIT OR Apache-2.0, commit 8d925e9df32d8755f22bd2635e400a6b3e93f69c), Scopes++ (GPL-3.0), and LiveLegacy (MIT). No source from these projects was copied. ScopeWalker uses screen capture; our source is Capture One's existing internal renderer. Source links and selection rationale are in docs/SCOPES.md.

The small ICC parameter profiles in c1ai/profiles were generated with QColorSpace's standard AdobeRgb and DisplayP3 definitions. They are not copies of Capture One, Adobe, or Apple application profile files. Adobe RGB and Display P3 names identify the color encodings; no endorsement is implied.

`c1ai/vectorscope.py` adapts the RGB-to-CbCr projection, target placement and skin-reference/graticule geometry from [OpenRV Web Vectorscope.ts](https://github.com/lifeart/openrv-web/blob/0ad41bb59713cb6abe710dbab8f4738834e82312/src/ui/components/Vectorscope.ts), copyright (c) 2024 Alex Kanunnikov, MIT. The full notice is preserved in [third_party/openrv-web-MIT.txt](third_party/openrv-web-MIT.txt). This is a NumPy/Pillow adaptation, not a bundled browser application. Count grids, ROI, numerical summaries and C1 input are additions. Other reviewed vectorscope repositories are listed in [docs/SCOPES_REUSE.md](docs/SCOPES_REUSE.md); code without confirmed redistribution permission was not copied.

Design research included [c1cmd](https://github.com/kiri11/c1cmd), [capture-one-mcp](https://github.com/byjustinjones/capture-one-mcp), [ToneRelay](https://github.com/ReefRuan/tonerelay-lightroom-mcp) and [Automaat/lightroom-mcp](https://github.com/Automaat/lightroom-mcp). Project records describe these as design references, without copying their implementation or prompts. Their code and licenses are not part of this distribution.
