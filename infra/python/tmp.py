from __future__ import print_function
import sys
import os

print("PATH IS: " + str(sys.path))
try:
  print("PYTHONPATH  " + os.environ.get("PYTHONPATH"))
except:
  pass

# Ensure the ssl modules are loaded correctly.
import ssl
from httplib import HTTPConnection
from urllib2 import HTTPSHandler


print("SUCCESSFULLY LOADED HTTPSHandler")
