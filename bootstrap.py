"""Load the portable dependencies without relying on user Python packages."""
# 依赖入口：把项目随附的 vendor 放到模块搜索路径最前面。
# 其他模块必须先导入 bootstrap，再导入 requests / yaml 等随附依赖；
# 这样 Docker 运行时不依赖宿主机的 Python 包，也不需要在启动时安装包。
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
# 使用文件自身的位置而非当前工作目录，避免从不同目录启动时找错依赖。
# insert(0) 确保随附版本优先于环境中碰巧安装的同名包。
sys.path.insert(0, str(ROOT / "vendor"))
