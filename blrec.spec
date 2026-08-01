from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH)
source_root = project_root / 'src'

datas = collect_data_files('blrec')
hiddenimports = collect_submodules('blrec') + [
    'uvicorn.lifespan.auto',
    'uvicorn.lifespan.off',
    'uvicorn.lifespan.on',
    'uvicorn.loops.asyncio',
    'uvicorn.loops.auto',
    'uvicorn.loops.uvloop',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.http.httptools_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl',
    'uvicorn.protocols.websockets.wsproto_impl',
]


a = Analysis(
    [str(source_root / 'blrec' / '__main__.py')],
    pathex=[str(source_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='blrec',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
