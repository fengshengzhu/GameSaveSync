# -*- coding: utf-8 -*-
"""程序入口：python run.py 直接运行，或由 build.bat 打包为单文件 exe。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.app import main

if __name__ == "__main__":
    main()
