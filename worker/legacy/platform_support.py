"""OS differences shared by the source CLI and the packaged worker."""
import os
import sys
from pathlib import Path


def private_fd(fd):
    if hasattr(os, 'fchmod'):
        os.fchmod(fd, 0o600)


def browser_candidates(platform=None, environ=None):
    """按 Edge 优先、Chrome 回退的顺序查找各安装位置。"""
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    if platform == 'darwin':
        return [Path(root) / name / 'Contents/MacOS' / exe
                for name, exe in [('Microsoft Edge.app', 'Microsoft Edge'),
                                  ('Google Chrome.app', 'Google Chrome')]
                for root in ('/Applications', str(Path.home() / 'Applications'))]
    if platform == 'win32':
        return [Path(env[root]) / suffix
                for suffix in ('Microsoft/Edge/Application/msedge.exe',
                               'Google/Chrome/Application/chrome.exe')
                for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                if env.get(root)]
    return []


def find_browser():
    for path in browser_candidates():
        if path.is_file():
            return path
    raise ValueError('未找到 Chrome 或 Edge，请先安装其中一种浏览器。')
