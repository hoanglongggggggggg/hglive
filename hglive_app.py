# -*- coding: utf-8 -*-
"""Diem vao danh rieng cho ban dong goi .exe cua HGLIVE.

Ten khac `hglive.py` la CO Y: PyInstaller lay ten script lam ten module, trung ten
voi goi `hglive` se khien goi bi che va ban build hong.
"""
import sys

from hglive.app import main

if __name__ == "__main__":
    sys.exit(main())
