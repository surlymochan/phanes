import os, sys
print("sys.path[0] =", repr(sys.path[0]))
print("__file__ =", repr(__file__))
print("guard would fire:", os.path.abspath(sys.path[0] or ".") == os.path.dirname(os.path.abspath(__file__)))
