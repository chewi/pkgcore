# Copyright: 2008-2011 Brian Harring <ferringb@gmail.com>
# License: BSD/GPL2

"""
A collection of distutils extensions adding things like automatic 2to3
translation, a test runner, and working around broken stdlib extensions CFLAG
passing in distutils.

Generally speaking, you should flip through this modules src.
"""

import errno
import inspect
import io
import math
import os
import re
import sys
import subprocess
import textwrap

os.environ["SNAKEOIL_DEMANDLOAD_PROTECTION"] = 'n'
os.environ["SNAKEOIL_DEMANDLOAD_WARN"] = 'n'

from distutils import log
from distutils.core import Command, Extension
from distutils.errors import DistutilsExecError
from distutils.command import (
    sdist as dst_sdist, build_ext as dst_build_ext, build_py as dst_build_py,
    build as dst_build, build_scripts as dst_build_scripts)


def find_project(repo_file):
    toplevel = os.path.dirname(os.path.realpath(repo_file))
    toplevel_depth = len(toplevel.split('/'))

    # look for a top-level module
    for root, dirs, files in os.walk(toplevel):
        if len(root.split('/')) > toplevel_depth + 1:
            continue
        if '__init__.py' in files:
            return os.path.basename(root)

    raise ValueError('No project module found')


# determine the project we're being imported into
PROJECT = find_project(inspect.stack(0)[1][1])
# top level repo/tarball directory
TOPDIR = os.path.abspath(os.path.dirname(inspect.stack(0)[1][1]))


def version(project=PROJECT):
    """Determine a project's version.

    Based on the assumption that a project defines __version__ in it's main
    module.
    """
    version = None
    try:
        with io.open(os.path.join(project, '__init__.py'), encoding='utf-8') as f:
            version = re.search(
                r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]',
                f.read(), re.MULTILINE).group(1)
    except IOError as e:
        if e.errno == errno.ENOENT:
            pass
        else:
            raise

    if version is None:
        raise RuntimeError('Cannot find version for project: %s' % (project,))

    return version


def get_file_paths(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            yield os.path.join(root, f)[len(path):].lstrip('/')


def data_mapping(host_prefix, path, skip=None):
    """Map repo paths to host paths for installed data files."""
    skip = list(skip) if skip is not None else []
    for root, dirs, files in os.walk(path):
        host_path = os.path.join(host_prefix, root.partition(path)[2].lstrip('/'))
        repo_path = os.path.join(path, root.partition(path)[2].lstrip('/'))
        if repo_path not in skip:
            yield (host_path, [os.path.join(root, x) for x in files
                               if os.path.join(root, x) not in skip])


class OptionalExtension(Extension):
    """python extension that is optional to build.

    If it's not required to have the exception built, just preferable,
    use this class instead of :py:class:`Extension` since the machinery
    in this module relies on isinstance to identify what absolutely must
    be built vs what would be nice to have built.
    """
    pass


class sdist(dst_sdist.sdist):
    """sdist command wrapper to bundle generated files for release."""

    package_namespace = PROJECT

    def initialize_options(self):
        dst_sdist.sdist.initialize_options(self)

    def generate_verinfo(self, base_dir):
        """Generate project version module.

        This is used by the --version option in interactive programs among
        other things.
        """
        from snakeoil.version import get_git_version
        log.info('generating _verinfo')
        data = get_git_version(base_dir)
        if not data:
            return
        path = os.path.join(base_dir, self.package_namespace, '_verinfo.py')
        with open(path, 'w') as f:
            f.write('version_info=%r' % (data,))

    def make_release_tree(self, base_dir, files):
        """Create and populate the directory tree that is put in source tars.

        This copies or hardlinks "normal" source files that should go
        into the release and adds generated files that should not
        exist in a working tree.
        """

        if 'build_man' in self.distribution.cmdclass:
            self.run_command('build_man')
            import shutil
            shutil.copytree(os.path.join(os.getcwd(), "build/sphinx/man"),
                            os.path.join(base_dir, "man"))

        dst_sdist.sdist.make_release_tree(self, base_dir, files)
        self.generate_verinfo(base_dir)


class build_py(dst_build_py.build_py):

    user_options = dst_build_py.build_py.user_options + [("inplace", "i", "do any source conversions in place")]

    package_namespace = PROJECT
    generate_verinfo = True

    def initialize_options(self):
        dst_build_py.build_py.initialize_options(self)
        self.inplace = False

    def finalize_options(self):
        self.inplace = bool(self.inplace)
        if self.inplace:
            self.build_lib = '.'
        dst_build_py.build_py.finalize_options(self)

    def _compute_rebuilds(self, force=False):
        for base, mod_name, path in self.find_all_modules():
            try:
                new_mtime = math.floor(os.lstat(path).st_mtime)
            except EnvironmentError:
                # ok... wtf distutils?
                continue
            trg_path = os.path.join(self.build_lib, path)
            if force:
                yield trg_path, new_mtime
                continue
            try:
                old_mtime = math.floor(os.lstat(trg_path).st_mtime)
            except EnvironmentError:
                yield trg_path, new_mtime
                continue
            if old_mtime != new_mtime:
                yield trg_path, new_mtime

    def _inner_run(self, rebuilds):
        pass

    def _run_generate_verinfo(self, rebuilds):
        from snakeoil.version import get_git_version
        ver_path = self.get_module_outfile(
            self.build_lib, (self.package_namespace,), '_verinfo')
        # this should check mtime...
        if not os.path.exists(ver_path):
            log.info('generating _verinfo')
            with open(ver_path, 'w') as f:
                f.write("version_info=%r" % (get_git_version('.'),))
            self.byte_compile([ver_path])
            rebuilds.append((ver_path, os.lstat(ver_path).st_mtime))

    def get_py2to3_converter(self, options=None, proc_count=0):
        from lib2to3 import refactor as ref_mod
        from snakeoil.dist import caching_2to3

        if ((sys.version_info >= (3, 0) and sys.version_info < (3, 1, 2)) or
                (sys.version_info >= (2, 6) and sys.version_info < (2, 6, 5))):
            if proc_count not in (0, 1):
                log.warn(
                    "disabling parallelization: you're running a python version "
                    "with a broken multiprocessing.queue.JoinableQueue.put "
                    "(python bug 4660).")
            proc_count = 1
        elif proc_count == 0:
            import multiprocessing
            proc_count = multiprocessing.cpu_count()

        assert proc_count >= 1

        if proc_count > 1 and not caching_2to3.multiprocessing_available:
            proc_count = 1

        refactor_kls = caching_2to3.MultiprocessRefactoringTool

        fixer_names = ref_mod.get_fixers_from_package('lib2to3.fixes')
        f = refactor_kls(fixer_names, options=options).refactor

        def f2(*args, **kwds):
            if caching_2to3.multiprocessing_available:
                kwds['num_processes'] = proc_count
            return f(*args, **kwds)

        return f2

    def run(self):
        py3k_rebuilds = []
        if not self.inplace:
            if is_py3k:
                py3k_rebuilds = list(self._compute_rebuilds(
                    self.force))
            dst_build_py.build_py.run(self)

        if self.generate_verinfo:
            self._run_generate_verinfo(py3k_rebuilds)

        self._inner_run(py3k_rebuilds)

        if not is_py3k:
            return

        converter = self.get_py2to3_converter()
        log.info("starting 2to3 conversion; this may take a while...")
        converter([x[0] for x in py3k_rebuilds], write=True)
        for path, mtime in py3k_rebuilds:
            os.utime(path, (-1, mtime))
        log.info("completed py3k conversions")


class build_py3(build_py):

    """build command wrapper for running 3to2 for py2 targets"""

    def run(self):
        py2k_rebuilds = []
        if not self.inplace:
            if not is_py3k:
                py2k_rebuilds = list(self._compute_rebuilds(self.force))
            dst_build_py.build_py.run(self)

        if self.generate_verinfo:
            self._run_generate_verinfo(py2k_rebuilds)

        self._inner_run(py2k_rebuilds)

        if is_py3k:
            return

        from lib3to2.build import run_3to2
        from lib2to3 import refactor

        # assume a few fixes are already handled in the code or aren't needed
        # for py27
        skip_list = (
            'lib3to2.fixes.fix_str', 'lib3to2.fixes.fix_printfunction',
            'lib3to2.fixes.fix_except', 'lib3to2.fixes.fix_with',
        )
        fixer_names = [x for x in refactor.get_fixers_from_package('lib3to2.fixes')
                       if x not in skip_list]

        log.info("starting 3to2 conversion; this may take a while...")
        run_3to2([x[0] for x in py2k_rebuilds], fixer_names=fixer_names)
        for path, mtime in py2k_rebuilds:
            os.utime(path, (-1, mtime))
        log.info("completed py2k conversions")


class build_man(Command):
    """Override the module search path before running sphinx.

    Fixes generating man pages for scripts that need to import modules
    generated via 2to3 or other conversions instead of straight from the build
    directory.
    """

    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        # Use a built version for the man page generation process that imports
        # script modules.
        build_py = self.distribution.get_command_obj('build_py')
        self.run_command('build_py')
        syspath = sys.path[:]
        sys.path.insert(0, os.path.abspath(build_py.build_lib))

        # generate man page content for scripts we create
        if 'build_scripts' in self.distribution.cmdclass:
            from snakeoil.dist.generate_docs import generate_man
            generate_man(PROJECT, TOPDIR)

        # generate man pages
        build_sphinx = self.distribution.get_command_obj('build_sphinx')
        build_sphinx.builder = 'man'
        self.run_command('build_sphinx')
        sys.path = syspath


class build_ext(dst_build_ext.build_ext):

    user_options = dst_build_ext.build_ext.user_options + [
        ("build-optional=", "o", "build optional C modules"),
        ("disable-distutils-flag-fixing", None,
         "disable fixing of issue 969718 in python, adding missing -fno-strict-aliasing"),
    ]

    boolean_options = dst_build.build.boolean_options + ["build-optional"]

    def initialize_options(self):
        dst_build_ext.build_ext.initialize_options(self)
        self.build_optional = None
        self.disable_distutils_flag_fixing = False
        self.default_header_install_dir = None

    def finalize_options(self):
        dst_build_ext.build_ext.finalize_options(self)
        if self.build_optional is None:
            self.build_optional = True
        if not self.build_optional:
            self.extensions = [ext for ext in self.extensions if not isinstance(ext, OptionalExtension)] or None

        # add header install dir to the search path
        # (fixes virtualenv builds for consumer extensions)
        self.set_undefined_options('install', ('install_headers', 'default_header_install_dir'))
        if self.default_header_install_dir:
            self.default_header_install_dir = os.path.dirname(self.default_header_install_dir)
            for e in self.extensions:
                # include_dirs may actually be shared between multiple extensions
                if self.default_header_install_dir not in e.include_dirs:
                    e.include_dirs.append(self.default_header_install_dir)

    def build_extensions(self):
        if self.debug:
            # say it with me kids... distutils sucks!
            for x in ("compiler_so", "compiler", "compiler_cxx"):
                l = [y for y in getattr(self.compiler, x) if y != '-DNDEBUG']
                l.append('-Wall')
                setattr(self.compiler, x, l)
        if not self.disable_distutils_flag_fixing:
            for x in ("compiler_so", "compiler", "compiler_cxx"):
                val = getattr(self.compiler, x)
                if "-fno-strict-aliasing" not in val:
                    val.append("-fno-strict-aliasing")
        return dst_build_ext.build_ext.build_extensions(self)


class build_scripts(dst_build_scripts.build_scripts):

    """Create and build (copy and modify #! line) the wrapper scripts."""

    def finalize_options(self):
        dst_build_scripts.build_scripts.finalize_options(self)
        script_dir = os.path.join(
            os.path.dirname(self.build_dir), '.generated_scripts')
        self.mkpath(script_dir)
        self.scripts = [os.path.join(script_dir, x) for x in os.listdir('bin')]

    def run(self):
        for script in self.scripts:
            with open(script, 'w') as f:
                f.write(textwrap.dedent("""\
                    #!%s
                    from os.path import basename
                    from %s import scripts
                    scripts.main(basename(__file__))
                """ % (sys.executable, PROJECT)))
        self.copy_scripts()


class install_docs(Command):

    """Install html documentation"""

    content_search_path = ('build/sphinx/html', 'html')
    user_options = [
        ('path=', None, "final path to install to; else it's calculated"),
        ('build-dir=', None, "build directory"),
    ]
    build_command = 'build_docs'

    def initialize_options(self):
        self.root = None
        self.prefix = None
        self.path = None
        self.build_dir = None
        self.content = []
        self.source_path = None

    def finalize_options(self):
        self.set_undefined_options(
            'install',
            ('root', 'root'),
            ('install_base', 'prefix'),
        )
        if not self.root:
            self.root = '/'
        if self.path is None:
            self.path = os.path.join(
                self.root, self.calculate_install_path().lstrip(os.path.sep))

    def calculate_install_path(self):
        return os.path.join(
            os.path.abspath(self.prefix), 'share', 'doc', PROJECT + '-%s' % version(), 'html')

    def find_content(self):
        for possible_path in self.content_search_path:
            if self.build_dir is not None:
                possible_path = os.path.join(self.build_dir, possible_path)
            possible_path = os.path.join(TOPDIR, possible_path)
            if os.path.isdir(possible_path):
                return possible_path
        else:
            return None

    def _map_paths(self, content):
        return {x: x for x in content}

    def scan_content(self):
        self.content = self._map_paths(get_file_paths(self.source_path))
        return self.content

    def run(self, firstrun=True):
        self.source_path = self.find_content()
        if self.source_path is None:
            if not firstrun:
                raise DistutilsExecError(
                    "no pregenerated sphinx content, and sphinx isn't available "
                    "to generate it; bailing")
            cwd = os.getcwd()
            if subprocess.call([sys.executable, 'setup.py', self.build_command], cwd=cwd):
                raise DistutilsExecError("%s failed" % self.build_command)
            return self.run(False)

        content = self.scan_content()

        content = self.content
        directories = set(map(os.path.dirname, content.values()))
        directories.discard('')
        for x in sorted(directories):
            self.mkpath(os.path.join(self.path, x))

        for src, dst in sorted(content.items()):
            self.copy_file(
                os.path.join(self.source_path, src),
                os.path.join(self.path, dst))

    def get_inputs(self):
        # Py3k compatibility- force list so behaviour is the same.
        return list(self.content)

    def get_outputs(self):
        # Py3k compatibility- force list so behaviour is the same.
        return list(self.content.values())


class install_man(install_docs):

    """Install man pages"""

    content_search_path = ('build/sphinx/man', 'man')
    build_command = 'build_man'

    def calculate_install_path(self):
        return os.path.join(self.prefix, 'share', 'man')

    def _map_paths(self, content):
        d = {}
        for x in content:
            if len(x) >= 3 and x[-2] == '.' and x[-1].isdigit():
                # Only consider extensions .1, .2, .3, etc, and files that
                # have at least a single char beyond the extension (thus ignore
                # .1, but allow a.1).
                d[x] = 'man%s/%s' % (x[-1], os.path.basename(x))
        return d


class test(Command):

    """Run our unit tests in a built copy.

    Based on code from setuptools.
    """

    blacklist = frozenset()

    user_options = [
        ("inplace", "i", "do building/testing in place"),
        ("skip-rebuilding", "s", "skip rebuilds. primarily for development"),
        ("disable-fork", None, "disable forking of the testloader; primarily for debugging.  "
                               "Automatically set in jython, disabled for cpython/unladen-swallow."),
        ("namespaces=", "t", "run only tests matching these namespaces.  "
                             "comma delimited"),
        ("pure-python", None, "disable building of extensions.  Enabled for jython, disabled elsewhere"),
        ("force", "f", "force build_py/build_ext as needed"),
        ("include-dirs=", "I", "include dirs for build_ext if needed"),
    ]

    default_test_namespace = '%s.test' % PROJECT

    def initialize_options(self):
        self.inplace = False
        self.disable_fork = is_jython
        self.namespaces = ''
        self.pure_python = is_jython
        self.force = False
        self.include_dirs = None

    def finalize_options(self):
        self.inplace = bool(self.inplace)
        self.disable_fork = bool(self.disable_fork)
        self.pure_python = bool(self.pure_python)
        self.force = bool(self.force)
        if isinstance(self.include_dirs, str):
            self.include_dirs = self.include_dirs.split(os.pathsep)
        if self.namespaces:
            self.namespaces = tuple(set(self.namespaces.split(',')))
        else:
            self.namespaces = ()

    def run(self):
        from snakeoil.dist import unittest_extensions

        build_ext = self.reinitialize_command('build_ext')
        build_py = self.reinitialize_command('build_py')
        build_ext.inplace = build_py.inplace = self.inplace
        build_ext.force = build_py.force = self.force

        if self.include_dirs:
            build_ext.include_dirs = self.include_dirs

        if not self.pure_python:
            self.run_command('build_ext')
        if not self.inplace:
            self.run_command('build_py')

        syspath = sys.path[:]
        mods_to_wipe = ()
        if not self.inplace:
            cwd = os.getcwd()
            syspath = [x for x in sys.path if x != cwd]
            test_path = os.path.abspath(build_py.build_lib)
            syspath.insert(0, test_path)
            mods = build_py.find_all_modules()
            mods_to_wipe = set(x[0] for x in mods)
            mods_to_wipe.update('.'.join(x[:2]) for x in mods)

        namespaces = self.namespaces
        if not self.namespaces:
            namespaces = [self.default_test_namespace]

        retval = unittest_extensions.run_tests(
            namespaces, disable_fork=self.disable_fork,
            blacklist=self.blacklist, pythonpath=syspath,
            modules_to_wipe=mods_to_wipe)

        # remove temporary plugincache so it isn't installed
        plugincache = os.path.join(
            os.path.abspath(build_py.build_lib), build_py.package_namespace,
            'plugins/plugincache')
        if os.path.exists(plugincache):
            os.remove(plugincache)

        if retval:
            raise DistutilsExecError("tests failed; return %i" % (retval,))


# yes these are in snakeoil.compatibility; we can't rely on that module however
# since snakeoil source is in 2k form, but this module is 2k/3k compatible.
# in other words, it could be invoked by py3k to translate snakeoil to py3k
is_py3k = sys.version_info >= (3, 0)
is_jython = 'java' in getattr(sys, 'getPlatform', lambda: '')().lower()
