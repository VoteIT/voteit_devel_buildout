Buildout for VoteIT core development
====================================

Development instructions for VoteIT. You need to have:

* a POSIX-compatible operating system. As far as we know, all Linux / UNIX
  version work, including Mac Os X. We haven't tested with Windows.
* Git installed. (See http://git-scm.com)
* Python installed. We're currently ONLY support Python 2.7.
* Setuptools and pip
  (See http://packages.python.org/an_example_pypi_project/setuptools.html)
* Virtualenv for Python. (Recommended but optional)
* C-compiler and python-dev package.


VoteIT manual: http://manual.voteit.se


Getting the code & building the server
--------------------------------------

As a normal user, type the following to fetch the code from or repository:

To clone the current repository

  git clone git://github.com/VoteIT/voteit_devel_buildout.git

Go into the directory

  cd voteit_devel_buildout

If you don't have commit rights to the VoteIT repositories,
you might need to change the [sources] urls in buildout.cfg
  
Install a copy of Python so we don't mess with the system Python. Also upgrade the local pip and setuptools

  virtualenv . -ppython2.7
  source bin/activate
  pip install --upgrade setuptools pip
  
Install buildout and run bootstrap process. See http://buildout.org for more info.

  pip install zc.buildout
  buildout

Have a cup of [tea|coffee]...
As buildout runs, it will fetch the voteit.core package and put it in the src directory

Running the development server
------------------------------

To start the server.

  pserve etc/development.ini

After a few seconds, it should display something like:

  serving on 0.0.0.0:6543 view at http://127.0.0.1:6543

Have fun!

...and remember, never ever use the development version for something serious!
