"""pi 的静态声明。

执行走原生 provider(runtime/pi.py 的 PiRuntimeProvider,effort 档位也在那里
声明);pi 优先用平台 vendored 安装(MC_HOME/pi/vendor),没有时回退到 PATH
上的系统级 pi(如 `brew install pi-coding-agent`)。检测、模型清单与升级
都不走通用路径,由下方钩子声明。无论哪种安装,pi 的主目录都重定向到
MC_HOME/pi,不读写 ~/.pi。
"""
import shutil
from pathlib import Path

from .spec import CliSpec

_NPM_PACKAGE = "@mariozechner/pi-coding-agent"
_BREW_FORMULA = "pi-coding-agent"


def locate_binary() -> str:
    """vendored 安装优先;缺失时回退到 PATH 上的系统级 pi(Homebrew 等)。

    每次启动都重新定位,库里不固化路径:数据目录搬迁或改用 brew 后自动生效。"""
    from ...core.config import pi_vendor_bin
    vendored = pi_vendor_bin()
    if vendored.is_file():
        return str(vendored)
    return shutil.which("pi") or ""


def _brew_managed(binary_path: str) -> bool:
    """二进制解析后落在 Homebrew Cellar 里才视为 brew 托管。"""
    try:
        return "/Cellar/" in str(Path(binary_path).resolve())
    except OSError:
        return False


def configured_models() -> list[str]:
    """pi 的执行单元与平台自有 models.json 同源(provider/model)。"""
    from ..pi import read_pi_models
    return read_pi_models()


def update_plan():
    """按实际安装来源升级,绝不 -g 污染全局:

    - vendored 安装(或尚未安装):只写平台自有 vendor 目录;
    - brew 托管的系统级 pi:`brew upgrade`;
    - 其他来源的系统级 pi(npm -g、手工安装等):不代管,返回 None。
    """
    from ...core.config import pi_vendor_bin, pi_vendor_prefix
    located = locate_binary()
    if not located or located == str(pi_vendor_bin()):
        return ("npm", ["npm", "install", "--prefix", str(pi_vendor_prefix()),
                        "--no-fund", "--no-audit", f"{_NPM_PACKAGE}@latest"])
    if _brew_managed(located):
        brew = shutil.which("brew") or "brew"
        return ("brew", [brew, "upgrade", _BREW_FORMULA])
    return None


def private_dirs() -> list[str]:
    """pi 的平台自有主目录(agent 配置/会话/vendored 安装均重定向到此)。"""
    from ...core.config import pi_home
    return [str(pi_home())]


SPEC = CliSpec(
    adapter="pi",
    binary="pi",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    # npm 包名用于 registry 版本比对;实际更新命令见 update_plan 钩子
    update={"npm": _NPM_PACKAGE},
    locate_binary=locate_binary,
    configured_models=configured_models,
    update_plan=update_plan,
    private_dirs=private_dirs,
)
