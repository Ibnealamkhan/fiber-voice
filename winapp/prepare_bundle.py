"""Build the Windows x64 gateway payload on Python 3.14+, on any host OS."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
BUILD = HERE / 'build'
PAYLOAD = BUILD / 'payload'
DOWNLOADS = {
    'python-3.13.16-embed-amd64.zip': (
        'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip',
        '97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297'),
    'opencore.pkg.tar.zst': (
        'https://repo.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-opencore-amr-0.1.6-1-any.pkg.tar.zst',
        '65c6b5a2795ff12fd1839fa52b59785210ca1c2154fcbb73d7ea39767ea11fc4'),
    'vo-amrwbenc.pkg.tar.zst': (
        'https://repo.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-vo-amrwbenc-0.1.3-2-any.pkg.tar.zst',
        '731b4ebecd81d8b5738a396bc8d0fddbd435c550c77876dadf6b2ce4f1d6003a'),
    'MicrosoftEdgeWebview2Setup.exe': (
        'https://go.microsoft.com/fwlink/p/?LinkId=2124703',
        'cf2f2445fef0c5c14e372045f84998771eeb8bb01d59be4afd00692fa160082e'),
}


def prepare():
    BUILD.mkdir(exist_ok=True)
    for name, (url, digest) in DOWNLOADS.items():
        target = BUILD / name
        if not target.exists():
            print('Downloading', name, flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, target.open('wb') as output:
                shutil.copyfileobj(response, output)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'Hash mismatch: {target}. Remove it and retry.')
    wheels = BUILD / 'wheels'
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--dest', str(wheels),
                    '--platform', 'win_amd64', '--python-version', '3.13',
                    '--implementation', 'cp', '--abi', 'cp313', '--only-binary=:all:',
                    '--require-hashes', '--no-deps', '-r', str(HERE / 'requirements-windows.txt')], check=True)
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    python = PAYLOAD / 'python'
    python.mkdir(parents=True)
    with zipfile.ZipFile(BUILD / 'python-3.13.16-embed-amd64.zip') as archive:
        archive.extractall(python)
    packages = python / 'Lib/site-packages'
    packages.mkdir(parents=True)
    # Only extract the wheels listed in the hash-locked download requirements.
    locked = {line.split(' --hash=')[1].split(':')[1].strip()
              for line in (HERE / 'requirements-windows.txt').read_text().splitlines()
              if line and not line.startswith('#')}
    found = set()
    for wheel in sorted(wheels.glob('*.whl')):
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest not in locked:
            continue
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(packages)
        found.add(digest)
    if found != locked:
        raise RuntimeError('Missing locked Windows wheels')
    (python / 'python313._pth').write_text('python313.zip\n.\nLib/site-packages\n../gateway\nimport site\n')
    gateway = PAYLOAD / 'gateway'
    gateway.mkdir()
    for source in (HERE.parent / 'gateway').glob('*.py'):
        shutil.copy2(source, gateway / source.name)
    shutil.copytree(HERE.parent / 'gateway/static', gateway / 'static')
    codecs = PAYLOAD / 'codecs'
    codecs.mkdir()
    for name in ('opencore.pkg.tar.zst', 'vo-amrwbenc.pkg.tar.zst'):
        with tarfile.open(BUILD / name, 'r:zst') as archive:
            for member in archive:
                if member.isfile() and member.name.startswith('ucrt64/bin/') and member.name.endswith('.dll'):
                    with archive.extractfile(member) as source, (codecs / Path(member.name).name).open('wb') as dest:
                        shutil.copyfileobj(source, dest)
    shutil.copy2(HERE.parent / 'LICENSE', PAYLOAD / 'LICENSE')
    shutil.copy2(HERE / 'THIRD-PARTY-NOTICES.txt', PAYLOAD / 'THIRD-PARTY-NOTICES.txt')
    shutil.copy2(HERE / 'APACHE-2.0.txt', codecs / 'LICENSE-APACHE-2.0.txt')
    shutil.copy2(BUILD / 'MicrosoftEdgeWebview2Setup.exe', PAYLOAD / 'MicrosoftEdgeWebview2Setup.exe')
    bundle = BUILD / 'gateway.zip'
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(PAYLOAD.rglob('*')):
            if source.is_file():
                archive.write(source, source.relative_to(PAYLOAD))
    print(f'Created {bundle} ({bundle.stat().st_size // 1024 // 1024} MB)', flush=True)


if __name__ == '__main__':
    prepare()
