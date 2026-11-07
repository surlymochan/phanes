import os, sys
print("sys.path[0] =", repr(sys.path[0]))
print("__file__ =", repr(__file__))
print("abspath(path0) =", os.path.abspath(sys.path[0] or "."))
print("dirname(abspath(__file__)) =", os.path.dirname(os.path.abspath(__file__)))
