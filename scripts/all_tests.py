#!/usr/bin/env python
import os
from sys import exit
from subprocess import Popen, PIPE



if __name__ == '__main__':
    test_dirs = []
    for item in os.walk('src').next()[1]:
        if os.path.isfile('src/%s/setup.py' % item):
            test_dirs.append(item)

    #test_dirs.remove('voteit.core')
    success = []
    failed = []
    for item in test_dirs:
        print "%s started" % item
        proc = Popen(['bin/nosetests', 'src/%s' % item, '-qx', '--nologcapture'], stdout=PIPE, stderr=PIPE)
        res = proc.wait()
       # print proc.communicate()
        if res:
            failed.append(item)
            print "- FAILED: %s" % item
        else:
            success.append(item)
    if failed:
        exit(1)
