"""Create a local app/desktop entry using this checkout's explicit Python."""
from pathlib import Path
import json
import plistlib
import shlex
import sys


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This shortcut is for macOS')
    repo = Path(__file__).resolve().parents[1]
    python = repo / '.venv/bin/python'
    if not python.is_file():
        raise SystemExit('Create the project .venv with the scopes dependencies first')
    app = Path.home() / 'Applications/Capture One 图像分析.app'
    info = app / 'Contents/Info.plist'
    identity = 'local.captureoneai.scopes'
    if app.exists():
        previous = plistlib.loads(info.read_bytes()) if info.is_file() else {}
        if previous.get('CFBundleIdentifier') != identity or previous.get('C1AIProjectRoot') != str(repo):
            raise SystemExit('The app entry already exists and belongs to another installation')
    desktop = Path.home() / 'Desktop' / app.name
    if desktop.exists() or desktop.is_symlink():
        if not desktop.is_symlink() or desktop.resolve() != app.resolve():
            raise SystemExit('The desktop entry already exists; it was not replaced')
    executable = app / 'Contents/MacOS/launch-scopes'
    resources = app / 'Contents/Resources'
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    state = Path.home() / 'Library/Application Support/CaptureOneAI'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    q = shlex.quote
    # The app is a one-shot launcher. A running menu-bar viewer must not make
    # LaunchServices swallow later desktop opens as an unhandled reopen event.
    executable.write_text('#!/bin/sh\numask 077\ncd ' + q(str(repo)) + ' || exit 1\n' +
                          q(str(python)) + ' -m c1ai.scopes_window "$@" >> ' +
                          q(str(state / 'scopes-window.log')) + ' 2>&1 < /dev/null &\n')
    executable.chmod(0o755)
    from PIL import Image, ImageDraw
    icon = Image.new('RGBA', (512, 512)); draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((8, 8, 504, 504), radius=110, fill='#151b24')
    draw.line([(70, 310), (130, 310), (175, 150), (230, 410),
               (285, 90), (340, 330), (395, 210), (448, 210)], fill='#8bd5ed', width=23)
    icon.save(resources / 'Scopes.icns')
    info.write_bytes(plistlib.dumps({'CFBundleIdentifier': identity, 'CFBundleExecutable': executable.name,
        'CFBundleName': 'Capture One 图像分析', 'CFBundleDisplayName': 'Capture One 图像分析',
        'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'CFBundleIconFile': 'Scopes.icns',
        'LSUIElement': True, 'NSHighResolutionCapable': True, 'C1AIProjectRoot': str(repo)}))
    desktop.parent.mkdir(exist_ok=True)
    if not desktop.is_symlink():
        desktop.symlink_to(app, target_is_directory=True)
    print(json.dumps({'app': str(app), 'desktop': str(desktop), 'python': str(python)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
