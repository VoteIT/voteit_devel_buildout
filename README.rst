Buildout for VoteIT core development
====================================

Development instructions for VoteIT. You need to have:

* a POSIX-compatible operating system. As far as we know, all Linux / UNIX
  version work, including Mac Os X. Windows is not supported.
* Git installed. (See http://git-scm.com)
* Python installed. We're currently developing on Python 2.6. Any lower version
  is not recommended. Also, it's very unlikely that Python 3.0 will work.
* Setuptools for Python installed. If you can type "easy_install" in a
  terminal, you have it.
  (See http://packages.python.org/an_example_pypi_project/setuptools.html)
* Virtualenv for Python. (Installed with "easy_install virtualenv" as root.
  See http://www.virtualenv.org for more information)

See `appendix`_ for more info

Getting the code & building the server
--------------------------------------

As a normal user, type the following to fetch the code from or repository:

To clone the current repository

  git clone git://github.com/VoteIT/voteit_devel_buildout.git

Go into the directory

  cd voteit_devel_buildout

If you don't have commit rights to the VoteIT repositories,
you might need to change the [sources] urls in buildout.cfg
  
Install a copy of Python so we don't mess with the system Python.

  virtualenv-2.6 . --no-site-packages
  
Run bootstrap process. See http://buildout.org for more info.

  bin/python2.6 bootstrap.py

A buildout file should now have been created - We'll run it to build the server

  bin/buildout

Have a cup of tea...
As buildout runs, it will fetch the voteit.core package and put it in the src directory

Running the development server
------------------------------

To start the server.

  bin/paster serve etc/development.ini

After a few seconds, it should display something like:

  serving on 0.0.0.0:6543 view at http://127.0.0.1:6543

With your favourite standards-compliant browser (yes that excludes Internet
Explorer), go to http://127.0.0.1:6543

Have fun!

...and remember, never ever use the development version for something serious!

.. _appendix:

Installing required packages on Debian / Ubuntu
-----------------------------------------------

First, you need to become root or use sudo, then type:

  apt-get install python2.6 python2.6-dev git-core

Install setuptools (You must have wget installed for this command to work)

  wget http://peak.telecommunity.com/dist/ez_setup.py
  python2.6 ez_setup.py

Install virtualenv

  easy_install-2.6 virtualenv

