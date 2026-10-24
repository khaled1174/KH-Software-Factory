"""Keep parent and child Python test processes from caching into the distribution."""
import os
import sys

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
