#!/bin/bash

# 清理旧的构建产物，防止重复上传引发 PyPI 400 报错
rm -rf dist yyds-fswatch.egg-info

python -m build

python -m twine upload dist/*