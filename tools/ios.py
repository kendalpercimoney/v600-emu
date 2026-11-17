"""Build the iPhone app fork with this emulator's front end (PORTING.md phase 6).

    .venv/bin/python tools/ios.py [--server=http://localhost:8654] [--device[=UDID]]
                                                                      (or ./phone ios)

Stages the page in build/app-firmware/: `make web` (the WebAssembly core, web/ and, if
firmware/v600.shx exists, the firmware gzipped: a private build), or with --server the
http transport instead (web/ plus a config.js with the emulator's address, tools/live.py).
Runs the fork's package builder on it (Tools/make_v600.py, which embeds the folder named
by V600_FIRMWARE), then builds the app for the iOS Simulator, or with --device for a
connected iPhone (the first one paired, or that UDID), installs it there and starts it. The fork's
location is in local.cfg (git-ignored), a line APP=/path/to/the/fork, so this public repo
never names it. Prints the built .app's path.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / 'build' / 'app-firmware'


def app_dir():
    cfg = ROOT / 'local.cfg'
    for line in cfg.read_text().splitlines() if cfg.exists() else []:
        if line.startswith('APP='):
            return Path(os.path.expanduser(line[4:].strip()))
    sys.exit('Put the app fork\'s folder in local.cfg as a line APP=/path/to/fork')


def main(argv):
    server = next((a[9:] for a in argv if a.startswith('--server=')), None)
    device = next((a[9:] or 'first' for a in argv if a.startswith('--device')), None)
    app = app_dir()
    if STAGE.exists():
        shutil.rmtree(STAGE)
    if server:
        shutil.copytree(ROOT / 'web', STAGE)
        (STAGE / 'config.js').write_text(f'window.V600_SERVER = {server!r};\n')
    else:
        subprocess.run(['make', '-s', '-C', str(ROOT), 'web'], check=True)
        shutil.copytree(ROOT / 'build' / 'web', STAGE)
    env = dict(os.environ, V600_FIRMWARE=str(STAGE))
    subprocess.run([sys.executable, str(app / 'Tools' / 'make_v600.py')], check=True, env=env)
    photo = app / 'Retrophonia' / 'Resources' / 'Devices' / 'v600-photo.retrodevice'
    if photo.exists():                  # the personal photo-skinned package wraps the same
        subprocess.run([sys.executable, str(app / 'Tools' / 'make_v600_photo.py')], check=True, env=env)
    derived = app / 'build'
    project = ['xcodebuild', '-project', str(app / 'Retrophonia.xcodeproj'), '-scheme', 'Retrophonia',
               '-derivedDataPath', str(derived), '-quiet']
    if device:
        udid = paired_iphone() if device == 'first' else device
        subprocess.run(project + ['-configuration', 'Release', '-destination', f'id={udid}',
                                  '-allowProvisioningUpdates', 'build'], check=True)
        built = derived / 'Build' / 'Products' / 'Release-iphoneos' / 'Retrophonia.app'
        subprocess.run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', udid, str(built)],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['xcrun', 'devicectl', 'device', 'process', 'launch', '--device', udid,
                        bundle_id(built)], check=True, stdout=subprocess.DEVNULL)
        print(f'installed and started on {udid}')
        return 0
    subprocess.run(project + ['-sdk', 'iphonesimulator', '-configuration', 'Debug', 'build'], check=True)
    built = derived / 'Build' / 'Products' / 'Debug-iphonesimulator' / 'Retrophonia.app'
    print(f'built {built}')
    return 0


def paired_iphone():
    """The UDID of the first connected, paired physical iPhone."""
    out = subprocess.run(['xcrun', 'devicectl', 'list', 'devices'], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ('available (paired)' in line or ' connected ' in line) and 'physical' in line:
            return next(w for w in line.split() if w.count('-') == 1 and len(w) > 20)
    sys.exit('No paired iPhone connected (xcrun devicectl list devices)')


def bundle_id(app):
    import plistlib
    return plistlib.loads((app / 'Info.plist').read_bytes())['CFBundleIdentifier']


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
