"""
工具包，对应 TS 源码的 claude-code-main/src/tools/ 目录。

TS 里每个工具是一个独立子目录（FileReadTool/、FileEditTool/、BashTool/……），
各自带 schema、prompt、权限、call 实现。我们用一个 Python 子包对应它，
每个工具一个 .py 文件。第一个落地的是 file_read.py（对应 FileReadTool/）。
"""

from .file_read import FileReadTool  # noqa: F401
from .file_edit import FileEditTool  # noqa: F401
from .bash import BashTool  # noqa: F401
from .glob import GlobTool  # noqa: F401
from .grep import GrepTool  # noqa: F401

__all__ = ["FileReadTool", "FileEditTool", "BashTool", "GlobTool", "GrepTool"]
