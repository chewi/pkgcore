# Copyright: 2005 Marien Zwart <marienz@gentoo.org>
# License: GPL2


import os
import shutil
import tempfile


class TempDirMixin(object):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        # change permissions back or rmtree can't kill it
        for root, dirs, files in os.walk(self.dir):
            for directory in dirs:
                os.chmod(os.path.join(root, directory), 0777)
        shutil.rmtree(self.dir)
