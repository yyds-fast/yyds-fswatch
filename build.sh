#!/bin/bash
set -euo pipefail

# 发布门禁：测试和静态检查任一失败都不会构建或上传。
python -m pytest -q
python -m ruff check yyds_fswatch tests examples setup.py

# 清理旧的构建产物，防止重复上传引发 PyPI 400 报错。
rm -rf dist build yyds_fswatch.egg-info

python -m build
python -m twine check dist/*

python -m twine upload dist/*
